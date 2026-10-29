import base64
import hashlib
import json
from datetime import date

from jevq import db
from jevq.sync import vibe


def _conn():
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO live_intents(id, ts, source, side, code, name, sim_price, sim_shares, reason, status) "
                 "VALUES (1, '2026-10-08T10:29:00', 'sim', 'buy', '002658', '雪迪龙', 7.17, 500, '超跌买入', 'done')")
    conn.execute("INSERT INTO live_orders(id, intent_id, ts, side, code, price, shares, filled, avg_price, status) "
                 "VALUES (1, 1, '2026-10-08T10:29:05', 'buy', '002658', 7.17, 500, 500, 7.14, 'filled')")
    conn.execute("INSERT INTO live_orders(id, intent_id, ts, side, code, price, shares, filled, avg_price, status) "
                 "VALUES (2, NULL, '2026-10-09T10:14:09', 'sell', '002658', 6.91, 3100, 3100, 6.98, 'filled')")
    conn.execute("INSERT INTO live_orders(id, intent_id, ts, side, code, price, shares, filled, avg_price, status) "
                 "VALUES (3, NULL, '2026-10-09T13:41:17', 'buy', '600653', 1.67, 13100, 0, 0.0, 'failed')")
    conn.execute("INSERT INTO live_orders(id, intent_id, ts, side, code, price, shares, filled, avg_price, status) "
                 "VALUES (4, NULL, '2026-10-09T14:00:00', 'buy', '600000', 9.0, 1000, 300, 8.95, 'partial')")
    conn.commit()
    return conn


def test_live_trades_only_filled_and_adds_opening_for_preexisting_shares():
    trades = vibe.live_trades(_conn())
    assert [(t["type"], t["symbol"], t["shares"]) for t in trades] == [   # 期初持仓紧挨在那笔卖出前面
        ("buy", "002658", 500), ("opening", "002658", 2600), ("sell", "002658", 3100), ("buy", "600000", 300)]
    opening = trades[1]
    assert opening["date"] == "2026-10-09" and opening["price"] == 7.14     # 成本按此前最近一笔买入估
    assert vibe.trade_key(opening) == "jev:opening:002658" and opening["name"] == "雪迪龙"
    assert trades[0]["note"].startswith("jev:live:1") and trades[0]["name"] == "雪迪龙"
    assert trades[2]["price"] == 6.98 and trades[2]["date"] == "2026-10-09"
    assert all(t["account"] == "实盘" for t in trades)


def test_opening_without_any_buy_uses_sell_price():
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO live_orders(id, ts, side, code, price, shares, filled, avg_price, status) "
                 "VALUES (1, '2026-10-09T10:00:00', 'sell', '000001', 10, 200, 200, 10.5, 'filled')")
    trades = vibe.live_trades(conn)
    assert trades[0]["type"] == "opening" and trades[0]["shares"] == 200 and trades[0]["price"] == 10.5


def test_trade_key():
    assert vibe.trade_key({"note": "jev:live:7｜理由"}) == "jev:live:7"
    assert vibe.trade_key({"note": "手动记的"}) is None
    assert vibe.trade_key({}) is None


def test_knowledge_docs_one_markdown_per_topic():
    kb = {"source": {"name": "频道"}, "articles": [{"id": 1, "title": "首板战法", "date": "2025-09-20"}],
          "topics": [{"id": "board", "name": "打板", "intro": "简介",
                      "sections": [{"title": "选股", "points": [{"text": "只做主线", "refs": [1]}]}]}]}
    docs = vibe.knowledge_docs(kb)
    assert list(docs) == ["Jev知识库_打板.md"]
    text = docs["Jev知识库_打板.md"].decode("utf-8")
    assert "# 打板" in text and "## 选股" in text and "- 只做主线" in text and "首板战法" in text


def test_collect_docs_reports_and_articles(tmp_path):
    (tmp_path / "2026-10-09.md").write_text("# 复盘", encoding="utf-8")
    (tmp_path / "README.md").write_text("说明", encoding="utf-8")
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO day_cache(date, kind, key, value) VALUES ('2026-10-09', 'hi189', 'L', ?)",
                 (json.dumps({"title": "10月9日(晚)/复盘", "link": "L", "published": "2026-10-09T20:00", "text": "正文"},
                             ensure_ascii=False),))
    docs = vibe.collect_docs(conn, tmp_path, kb=None)
    assert docs["Jev复盘_2026-10-09.md"] == "# 复盘".encode("utf-8")
    name = next(n for n in docs if n.startswith("hi189_"))
    assert name == "hi189_2026-10-09_10月9日(晚)_复盘.md"           # 斜杠不能进文件名
    assert "正文" in docs[name].decode("utf-8") and "L" in docs[name].decode("utf-8")
    assert not any("README" in n for n in docs)


class FakeVibe:
    def __init__(self, trades=(), reports=()):
        self.trades = list(trades)
        self.reports = list(reports)
        self.deleted = []
        self.added_trades = []
        self.n = 0

    def list_trades(self):
        return list(self.trades)

    def add_trade(self, rec):
        self.added_trades.append(rec)
        self.trades.append({**rec, "id": f"t{len(self.trades)}"})

    def list_reports(self):
        return list(self.reports)

    def add_report(self, name, content: bytes):
        sha = hashlib.sha256(content).hexdigest()
        for r in self.reports:
            if r["_sha"] == sha:
                return r
        self.n += 1
        r = {"id": f"r{self.n}", "name": name, "_sha": sha}
        self.reports.append(r)
        return r

    def delete_report(self, rid):
        self.deleted.append(rid)
        self.reports = [r for r in self.reports if r["id"] != rid]


def test_push_trades_skips_already_imported_and_keeps_order():
    trades = vibe.live_trades(_conn())
    fake = FakeVibe(trades=[{**trades[0], "id": "x"}, {"symbol": "600519", "note": "dj 手动记的"}])
    state = {}
    stats = vibe.push(fake, trades, {}, state)
    assert [vibe.trade_key(t) for t in fake.added_trades] == [vibe.trade_key(t) for t in trades[1:]]
    assert stats["trades"] == 3
    assert vibe.push(fake, trades, {}, state)["trades"] == 0          # 再跑一遍不重复


def test_push_docs_uploads_changed_and_removes_old_version():
    fake = FakeVibe()
    state = {}
    assert vibe.push(fake, [], {"a.md": b"v1"}, state)["docs"] == 1
    assert vibe.push(fake, [], {"a.md": b"v1"}, state)["docs"] == 0          # 没变不传
    assert vibe.push(fake, [], {"a.md": b"v2"}, state)["docs"] == 1          # 变了：传新版、删旧版
    assert [r["name"] for r in fake.reports] == ["a.md"] and fake.deleted == ["r1"]
    assert state["docs"]["a.md"]["sha"] == hashlib.sha256(b"v2").hexdigest()


def test_push_docs_reuploads_when_deleted_in_vibe():
    fake = FakeVibe()
    state = {}
    vibe.push(fake, [], {"a.md": b"v1"}, state)
    fake.reports = []                                                     # dj 在 Vibe 里删了
    assert vibe.push(fake, [], {"a.md": b"v1"}, state)["docs"] == 1


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, headers, json))
        if url.endswith("/ledger/trade") and method == "GET":
            return FakeResp({"trade": [{"id": "1", "note": "jev:live:1"}]})
        if url.endswith("/reports") and method == "GET":
            return FakeResp([{"id": "r", "name": "a.md"}])
        return FakeResp({"id": "new"})


def test_client_requests():
    s = FakeSession()
    c = vibe.VibeClient("http://127.0.0.1:8765", "t" * 20, session=s)
    assert c.list_trades() == [{"id": "1", "note": "jev:live:1"}]
    c.add_trade({"symbol": "002658"})
    c.add_report("a.md", "中".encode("utf-8"))
    c.delete_report("r")
    method, url, headers, body = s.calls[1]
    assert (method, url, body) == ("POST", "http://127.0.0.1:8765/ledger/trade", {"record": {"symbol": "002658"}})
    assert headers["Authorization"] == "Bearer " + "t" * 20 and headers["Content-Type"] == "application/json"
    assert s.calls[2][3] == {"name": "a.md", "content": base64.b64encode("中".encode("utf-8")).decode()}
    assert s.calls[3][1].endswith("/reports/r/delete")


def test_write_local_files(tmp_path):
    trades = vibe.live_trades(_conn())
    paths = vibe.write_local(tmp_path, trades, {"Jev复盘_2026-10-09.md": b"x"})
    csv_text = (tmp_path / "trades.csv").read_text(encoding="utf-8-sig")
    assert csv_text.splitlines()[0] == "date,symbol,name,type,shares,price,account,note"
    assert "002658" in csv_text and (tmp_path / "docs" / "Jev复盘_2026-10-09.md").read_bytes() == b"x"
    assert len(paths) == 2


def test_sync_day_skips_push_when_vibe_not_installed(tmp_path):
    out = tmp_path / "out"
    stats = vibe.sync_day(_conn(), date(2026, 10, 9), out_dir=out, report_dir=tmp_path,
                          vibe_dir=tmp_path / "nope", kb=None)
    assert stats["pushed"] is False and (out / "trades.csv").exists()


def test_sync_day_pushes_through_given_client(tmp_path):
    fake = FakeLedger()
    (tmp_path / "2026-10-09.md").write_text("# 复盘", encoding="utf-8")
    stats = vibe.sync_day(_held_conn(), date(2026, 10, 9), out_dir=tmp_path / "out", report_dir=tmp_path,
                          kb=None, client_factory=lambda: _ctx(fake), calendar=CAL)
    assert stats["pushed"] is True and stats["trades"] == 4 and stats["docs"] == 1 and stats["opened"] == 1
    state = json.loads((tmp_path / "out" / "state.json").read_text(encoding="utf-8"))
    assert "Jev复盘_2026-10-09.md" in state["docs"]


class _ctx:
    def __init__(self, c):
        self.c = c

    def __enter__(self):
        return self.c

    def __exit__(self, *a):
        return False


# ---- 实盘持仓 → 论点 + 判据 ----

CAL = [date(2026, 10, d) for d in (8, 9, 12, 13, 14, 15)]


def _held_conn(source="rebound"):
    conn = _conn()
    conn.execute("INSERT INTO live_positions(code, shares, cost, buy_price, buy_date, sim_buy_price) "
                 "VALUES ('002658', 500, 3570.0, 7.14, '2026-10-08', 7.17)")
    if source:
        conn.execute("INSERT INTO regime_sources VALUES ('002658', ?, '2026-10-08T10:29:00')", (source,))
    conn.commit()
    return conn


def test_live_holdings_reads_position_buy_reason_and_source():
    [h] = vibe.live_holdings(_held_conn("lowbuy"))
    assert h == {"key": "002658:2026-10-08", "code": "002658", "name": "雪迪龙", "buy_date": "2026-10-08",
                 "buy_price": 7.14, "shares": 500, "reason": "超跌买入", "source": "lowbuy"}


def test_live_holdings_defaults_to_rebound_without_source():
    assert vibe.live_holdings(_held_conn(None))[0]["source"] == "rebound"


def test_nth_trading_day():
    assert vibe.nth_trading_day(date(2026, 10, 9), 1, CAL) == date(2026, 10, 12)
    assert vibe.nth_trading_day(date(2026, 10, 8), 2, CAL) == date(2026, 10, 12)
    assert vibe.nth_trading_day(date(2026, 10, 9), 1, None) == date(2026, 10, 12)     # 没日历按工作日


def test_plan_records_per_source():
    h = vibe.live_holdings(_held_conn())[0]
    thesis, stop, due = vibe.plan_records(h, CAL)
    assert thesis["symbol"] == "002658" and thesis["status"] == "active" and thesis["review_by"] == "2026-10-13"
    assert "超跌反弹" in thesis["title"] and "超跌买入" in thesis["statement"] and "7.14" in thesis["statement"]
    assert thesis["note"].startswith("jev:thesis:002658:2026-10-08")
    assert stop["type"] == "falsifier" and "-3%" in stop["statement"] and "-3.5" in stop["statement"]   # 实盘跟主账户：-3%
    assert due["type"] == "decision_point" and due["due"] == "2026-10-13" and "14:55" in due["statement"]   # 主账户拿 3 天
    board = vibe.plan_records({**h, "source": "board"}, CAL)
    assert board[2]["due"] == "2026-10-09" and "竞价不及预期" in board[1]["statement"]
    lowbuy = vibe.plan_records({**h, "source": "lowbuy"}, CAL)
    assert lowbuy[2]["due"] == "2026-10-12" and "-3" in lowbuy[1]["statement"]       # 低吸主账户拿 2 天


class FakeLedger(FakeVibe):
    def __init__(self):
        super().__init__()
        self.kinds = {"thesis": [], "criterion": []}

    def list_ledger(self, kind):
        return [dict(r) for r in self.kinds[kind]]

    def save(self, kind, rec):
        rows = self.kinds[kind]
        if "id" in rec:
            i = next(i for i, r in enumerate(rows) if r["id"] == rec["id"])
            rows[i] = dict(rec)
            return dict(rec)
        new = {**rec, "id": f"{kind}{len(rows)}"}
        rows.append(new)
        return dict(new)


def test_sync_plans_opens_once_and_closes_after_sell():
    fake = FakeLedger()
    conn = _held_conn()
    assert vibe.sync_plans(fake, conn, CAL) == {"opened": 1, "closed": 0}
    assert vibe.sync_plans(fake, conn, CAL) == {"opened": 0, "closed": 0}        # 不重复
    [t] = fake.kinds["thesis"]
    assert {c["thesis_id"] for c in fake.kinds["criterion"]} == {t["id"]} and len(fake.kinds["criterion"]) == 2
    # 卖掉了：持仓清空，有一笔止损卖出
    conn.execute("DELETE FROM live_positions")
    conn.execute("INSERT INTO live_intents(id, ts, source, side, code, name, reason, status) "
                 "VALUES (9, '2026-10-09T10:14:00', 'sim', 'sell', '002658', '雪迪龙', '止损-2.1%', 'done')")
    conn.execute("UPDATE live_orders SET intent_id=9 WHERE id=2")
    conn.commit()
    assert vibe.sync_plans(fake, conn, CAL) == {"opened": 0, "closed": 1}
    assert fake.kinds["thesis"][0]["status"] == "closed"
    crit = {c["type"]: c for c in fake.kinds["criterion"]}
    assert crit["falsifier"]["status"] == "met" and crit["falsifier"]["judged_at"] == "2026-10-09"
    assert "止损-2.1%" in crit["falsifier"]["verdict_note"] and "6.98" in crit["falsifier"]["verdict_note"]
    assert crit["decision_point"]["status"] == "met"
    assert vibe.sync_plans(fake, conn, CAL) == {"opened": 0, "closed": 0}


def test_sync_plans_close_without_stop_marks_falsifier_dropped():
    fake = FakeLedger()
    conn = _held_conn()
    vibe.sync_plans(fake, conn, CAL)
    conn.execute("DELETE FROM live_positions")
    conn.commit()
    vibe.sync_plans(fake, conn, CAL)
    crit = {c["type"]: c for c in fake.kinds["criterion"]}
    assert crit["falsifier"]["status"] == "dropped"


def test_sync_plans_leaves_user_theses_alone():
    fake = FakeLedger()
    fake.kinds["thesis"].append({"id": "u1", "title": "我自己的", "status": "active", "note": "dj 写的"})
    vibe.sync_plans(fake, _conn(), CAL)
    assert fake.kinds["thesis"] == [{"id": "u1", "title": "我自己的", "status": "active", "note": "dj 写的"}]


def test_client_ignores_system_proxy_for_localhost():
    """bug D：Windows 系统代理（VPN）例外里没有 127.0.0.1，requests 默认会把本机请求也送进代理。"""
    c = vibe.VibeClient("http://127.0.0.1:8765", "t" * 20)
    assert c.session.trust_env is False


def test_second_opening_for_same_code_gets_its_own_key():
    """bug E：同一只票第二次出现「接管前已有持仓」要单独补一笔，不能被第一笔的去重标记吞掉；
    第一笔的标记保持 jev:opening:<代码>（已经导进 Vibe 的那条不能重复导）。"""
    conn = db.connect(":memory:")
    rows = [(1, "2026-10-08T10:00:00", "buy", 500), (2, "2026-10-09T10:00:00", "sell", 3100),
            (3, "2026-10-20T10:00:00", "sell", 1000)]
    for oid, ts, side, n in rows:
        conn.execute("INSERT INTO live_orders(id, ts, side, code, price, shares, filled, avg_price, status) "
                     "VALUES (?, ?, ?, '002658', 7, ?, ?, 7.0, 'filled')", (oid, ts, side, n, n))
    openings = [t for t in vibe.live_trades(conn) if t["type"] == "opening"]
    assert [(vibe.trade_key(t), t["shares"], t["date"]) for t in openings] == [
        ("jev:opening:002658", 2600, "2026-10-09"), ("jev:opening:002658:3", 1000, "2026-10-20")]


def test_api_client_closes_its_log_file(tmp_path, monkeypatch):
    """F：临时起 Vibe 时打开的日志文件用完要关。"""
    opened = []
    real_open = open

    def spy(*a, **k):
        f = real_open(*a, **k)
        opened.append(f)
        return f
    monkeypatch.setattr("builtins.open", spy)
    monkeypatch.setattr(vibe.config, "VIBE_OUT_DIR", str(tmp_path))
    monkeypatch.setattr(vibe, "_port_open", lambda port: False)

    class Proc:
        returncode = 1

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass
    monkeypatch.setattr(vibe.subprocess, "Popen", lambda *a, **k: Proc())
    try:
        with vibe.api_client(tmp_path, wait=1):
            pass
    except RuntimeError:
        pass
    logs = [f for f in opened if str(getattr(f, "name", "")).endswith("api.log")]
    assert logs and all(f.closed for f in logs)
