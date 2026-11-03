import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta

import pytest

from dashboard.decision_journal import DecisionJournal
from dashboard.subscription_bridge import create_snapshot, submit_answers
from jevq.config import AI_ACCOUNT
from jevq.models import Quote

NOW = datetime(2026, 10, 28, 10, 30)


def _quote():
    return Quote(code="600001", name="测试公司", price=9.0, pre_close=10.0, open=9.8,
                 high=10.2, low=8.8, volume=10000, amount=1_000_000,
                 limit_up=11.0, limit_down=9.0, ts=NOW)


def _context():
    return {"snapshot": {"phase": "盘中"},
            "marketBreadth": {"complete": True, "stale": False, "up": 3000, "down": 2000,
                              "upRatio": 60.0, "total": 5000, "universe": 5000,
                              "limitUp": 50, "limitDown": 5},
            "sectorBreadth": {"complete": True, "stale": False, "up": 30, "down": 19,
                              "leaders": [{"leaderCode": "sh600001", "name": "机器人", "change": 2.3}]},
            "observations": []}


def _files(tmp_path):
    quote = _quote()
    raw_quote = asdict(quote)
    raw_quote["ts"] = quote.ts.isoformat()
    snapshot = {"snapshotId": NOW.isoformat(), "createdAt": NOW.isoformat(),
                "context": _context(), "quotes": {"600001": raw_quote}}
    answers = {"snapshotId": NOW.isoformat(),
               "regime": {"choice": "修复观察", "confidence": 0.8},
               "action": {"choice": "允许模拟小仓评估", "confidence": 0.8},
               "portfolio": {"posture": "平衡", "positionPct": 0.2,
                             "orders": {"600001": {"action": "模拟买入", "confidence": 0.9,
                                                  "reason": "订阅集成测试"}}}}
    snapshot_path = tmp_path / "snapshot.json"
    answers_path = tmp_path / "answers.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    answers_path.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
    return snapshot_path, answers_path


class FakeNotifier:
    enabled = True

    def __init__(self, fail=False):
        self.messages = []
        self.flushed = False
        self.fail = fail

    def send(self, title, content):
        if self.fail:
            raise RuntimeError("push unavailable")
        self.messages.append((title, content))

    def flush(self, timeout):
        self.flushed = True


def test_subscription_answers_reuse_local_paper_without_api(tmp_path):
    snapshot, answers = _files(tmp_path)
    notifier = FakeNotifier()
    result = submit_answers(snapshot, answers, tmp_path / "decisions.jsonl", tmp_path / "paper.sqlite",
                            clock=lambda: NOW, notifier=notifier)
    assert result["ok"] is True and result["reconciled"] is True
    assert result["model"] == "kimi-work-subscription" and result["liveTrading"] is False
    assert len(result["execution"]["fills"]) == 1
    assert notifier.flushed is True and result["notificationEnabled"] is True
    assert len(notifier.messages) == 2
    assert "📊" in notifier.messages[0][0] and "订阅集成测试" in notifier.messages[0][1]
    assert "模拟买入" in notifier.messages[1][0] and "200 股 @9.00" in notifier.messages[1][1]
    conn = sqlite3.connect(tmp_path / "paper.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM fills WHERE account=?", (AI_ACCOUNT,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0] == 0
    conn.close()


def test_notification_failure_does_not_roll_back_paper_fill(tmp_path):
    snapshot, answers = _files(tmp_path)
    result = submit_answers(snapshot, answers, tmp_path / "decisions.jsonl", tmp_path / "paper.sqlite",
                            clock=lambda: NOW, notifier=FakeNotifier(fail=True))
    assert result["ok"] is True and result["notificationError"] == "push unavailable"
    assert len(result["execution"]["fills"]) == 1


def test_subscription_snapshot_mismatch_and_staleness_are_rejected(tmp_path):
    snapshot, answers = _files(tmp_path)
    value = json.loads(answers.read_text(encoding="utf-8"))
    value["snapshotId"] = "wrong"
    answers.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="编号不一致"):
        submit_answers(snapshot, answers, tmp_path / "decisions.jsonl", tmp_path / "paper.sqlite",
                       clock=lambda: NOW)

    _files(tmp_path)
    with pytest.raises(ValueError, match="有效期"):
        submit_answers(snapshot, answers, tmp_path / "decisions.jsonl", tmp_path / "paper.sqlite",
                       clock=lambda: NOW + timedelta(seconds=181))


def test_journal_reloads_external_subscription_records(tmp_path):
    path = tmp_path / "decisions.jsonl"
    journal = DecisionJournal(path, external=True)
    path.write_text('{"id":"AI-SUB-1","ts":"2026-10-28T10:30:00"}\n', encoding="utf-8")
    assert [row["id"] for row in journal.records()] == ["AI-SUB-1"]
    assert journal.status()["enabled"] is True
    assert journal.status()["mode"] == "subscription"


def test_snapshot_waits_for_market_breadth_before_eligibility(monkeypatch, tmp_path):
    class Market:
        def __init__(self):
            self.ready = False
            self.blocking_calls = 0

        def payload(self, blocking=False):
            if blocking:
                self.blocking_calls += 1
                self.ready = True
            return {"marketBreadth": {"complete": self.ready, "stale": False},
                    "sectorBreadth": {"complete": self.ready, "stale": False}}

    class Connection:
        def close(self):
            pass

    class Paper:
        def _connect(self):
            return Connection()

        def _reconcile(self, conn, now):
            pass

        def _candidate_codes(self, conn, context):
            return []

    class Service:
        def __init__(self):
            self.market = Market()
            self.paper = Paper()

        def payload(self):
            return {"snapshot": {"phase": "盘中"}, **self.market.payload()}

        def _paper_quotes(self, codes):
            return {}

    service = Service()
    monkeypatch.setattr("dashboard.subscription_bridge.DashboardService", lambda: service)
    result = create_snapshot(tmp_path / "snapshot.json", clock=lambda: NOW)
    assert service.market.blocking_calls == 1
    assert result["eligible"] is False
    assert result["reason"].startswith("候选代码为空")


def test_analysis_push_is_detailed_with_names_sectors_and_next_steps():
    from dashboard.subscription_bridge import notify_result
    record = {"id": "AI-1", "ts": "2026-11-02T10:58:38", "regime": "修复观察", "action": "空仓等待",
              "confidence": 0.58, "market": "全市场上涨 2879 家", "sector": "行业上涨 36 个",
              "analysis": {"market": "修复延续、没有主线", "sectors": "传媒娱乐有量", "picks": "一只都不碰",
                           "forecast": "涨停 50-65 震荡", "review": "上一轮方向对", "plan": "盯涨停 70 家"}}
    latest = {"portfolio": {"posture": "防守", "positionPct": 0,
                            "orders": {"002292": {"action": "观察", "confidence": 0.72, "reason": "已封板",
                                                  "sector": "传媒娱乐", "next": "明天竞价高开 3% 以内且放量再评估"}}},
              "execution": {"fills": [], "skipped": []}}
    quotes = {"002292": Quote(code="002292", name="奥飞娱乐", price=8.71, pre_close=7.92, open=7.95,
                              high=8.71, low=7.9, volume=1, amount=6.59e8, limit_up=8.71, limit_down=7.13, ts=NOW)}
    notifier = FakeNotifier()
    notify_result(notifier, record, latest, True, quotes=quotes)
    title, body = notifier.messages[0]
    assert "空仓等待" in title
    for text in ("奥飞娱乐", "传媒娱乐", "8.71", "+9.97%", "已封板", "明天竞价高开 3% 以内且放量再评估",
                 "修复延续、没有主线", "传媒娱乐有量", "涨停 50-65 震荡", "盯涨停 70 家", "上一轮方向对",
                 "闸门由规则定", "次日一开盘就卖", "09:30-10:30"):
        assert text in body, text


def test_portfolio_answer_keeps_sector_and_next_step():
    from dashboard.kimi_judge import _convert_portfolio
    criteria = {"posture": ["防守"], "positionPct": [0], "codes": {"002292": "奥飞娱乐"}, "actions": ["观察"]}
    out = _convert_portfolio(criteria, {"posture": "防守", "positionPct": 0, "orders": {
        "002292": {"action": "观察", "confidence": 0.7, "reason": "封板", "sector": "传媒娱乐", "next": "竞价再看"}}})
    assert out["orders"]["002292"]["sector"] == "传媒娱乐" and out["orders"]["002292"]["next"] == "竞价再看"


def test_only_shanghai_shenzhen_main_board_codes_reach_deepseek():
    # dj 2026-11-02：只看沪深主板，创业板/科创板/北交所买不了
    from dashboard.subscription_bridge import _main_board_only
    assert _main_board_only(["600779", "300085", "688981", "002292", "000559", "301234", "920001", "830799", "603081"]) \
        == ["600779", "002292", "000559", "603081"]


def test_snapshot_codes_drive_the_paper_engine_even_with_chinext_sector_leader(tmp_path):
    # 2026-11-02 13:01：快照已按主板过滤，模拟账户却又从行业龙头取到 300133，查不到行情 KeyError，整轮失败
    snapshot, answers = _files(tmp_path)
    snap = json.loads(snapshot.read_text(encoding="utf-8"))
    snap["context"]["sectorBreadth"]["leaders"] = [{"leaderCode": "sz300133", "name": "传媒", "change": 3.0},
                                                   {"leaderCode": "sh600001", "name": "机器人", "change": 2.3}]
    snapshot.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    result = submit_answers(snapshot, answers, tmp_path / "decisions.jsonl", tmp_path / "paper.sqlite",
                            clock=lambda: NOW, notifier=FakeNotifier())
    assert result["ok"] is True and len(result["execution"]["fills"]) == 1


def test_quote_industries_come_from_jevq_database(tmp_path):
    from dashboard.subscription_bridge import _industries
    db = tmp_path / "jevq.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE industries (code TEXT, industry TEXT, updated TEXT)")
    conn.execute("INSERT INTO industries VALUES ('603081', '传媒娱乐', '')")
    conn.commit()
    conn.close()
    assert _industries(db, ["603081", "600001"]) == {"603081": "传媒娱乐"}
    assert _industries(tmp_path / "missing.sqlite", ["603081"]) == {}


def test_stock_extras_add_seal_position_cap_and_holders(tmp_path):
    from datetime import date as _date
    from dashboard.subscription_bridge import _stock_extras
    db = tmp_path / "jevq.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE closes (date TEXT, code TEXT, close REAL, volume REAL, pre_close REAL)")
    conn.execute("CREATE TABLE holders (code TEXT, holders INT, as_of TEXT, updated TEXT)")
    for i in range(25):
        conn.execute("INSERT INTO closes VALUES (?, '600001', ?, 0, 0)", (f"2026-10-{i + 1:02d}", 10.0 + i * 0.1))
    conn.execute("INSERT INTO closes VALUES ('2026-11-03', '600001', 99, 0, 0)")          # 今天的不算
    conn.execute("INSERT INTO holders VALUES ('600001', 35000, '', '')")
    conn.commit()
    conn.close()
    q = Quote(code="600001", name="甲", price=11.0, pre_close=10.0, open=10.2, high=11.0, low=10.1, volume=1,
              amount=2e8, limit_up=11.0, limit_down=9.0, ts=NOW, float_mcap=55.5, bid_vols=(20000, 1, 1, 1, 1))
    out = _stock_extras(db, {"600001": q}, _date(2026, 11, 3))["600001"]
    assert out["sealAmount亿"] == 0.22 and out["sealRatio"] == 0.11
    assert out["floatMcap亿"] == 55.5 and out["holders万"] == 3.5
    assert out["pos5%"] == round((12.4 / 11.9 - 1) * 100, 1) and out["pos20%"] == round((12.4 / 10.4 - 1) * 100, 1)


def _targets_file(tmp_path, day="2026-11-03", relay=None, wts=None):
    import json
    p = tmp_path / "strategy_targets.json"
    p.write_text(json.dumps({"date": day, "relay": relay or {}, "wts": wts or {}}), encoding="utf-8")
    return p


def _tq(code, name, open_pct, pct, ts=None, pre=10.0):
    ts = ts or datetime(2026, 11, 3, 9, 26)
    price = round(pre * (1 + pct / 100), 2)
    return Quote(code=code, name=name, price=price, pre_close=pre, open=round(pre * (1 + open_pct / 100), 2),
                 high=price, low=price, volume=1, amount=1e8, limit_up=round(pre * 1.1, 2),
                 limit_down=round(pre * 0.9, 2), ts=ts)


def test_strategy_targets_list_other_strategies_with_the_buy_plan(tmp_path):
    """dj 2026-11-03：「出了首板还有其他策略的标的吗」——高标接力、弱转强的标的和今天的买入计划也要在推送里。"""
    from dashboard.subscription_bridge import _strategy_targets
    path = _targets_file(tmp_path, relay={"001219": 6, "002172": 4, "600261": 3}, wts={"600001": 2, "600002": 3})
    qs = {"001219": _tq("001219", "甲", 4.2, 4.5), "002172": _tq("002172", "乙", 7.5, 8.0),
          "600261": _tq("600261", "丙", 1.0, 0.5), "600001": _tq("600001", "丁", -1.0, 1.0),
          "600002": _tq("600002", "戊", 2.0, 3.0)}
    got = _strategy_targets(datetime(2026, 11, 3, 9, 26), lambda codes: qs, path)
    by = {(t["strategy"], t["code"]): t for t in got}
    assert "在 3~6% 区间" in by[("高标接力", "001219")]["plan"] and "6 板" in by[("高标接力", "001219")]["plan"]
    assert "超过 6%" in by[("高标接力", "002172")]["plan"]
    assert "不到 3%" in by[("高标接力", "600261")]["plan"]
    assert "不在 0~6%" in by[("弱转强", "600001")]["plan"]
    assert "在 0~6% 区间" in by[("弱转强", "600002")]["plan"]
    assert _strategy_targets(datetime(2026, 11, 4, 9, 26), lambda codes: qs, path) == []      # 文件是昨天的：不用


def test_strategy_targets_without_auction_result_say_to_wait(tmp_path):
    from dashboard.subscription_bridge import _strategy_targets
    path = _targets_file(tmp_path, relay={"001219": 6})
    got = _strategy_targets(datetime(2026, 11, 3, 9, 10), lambda codes: {}, path)
    assert got and "等 09:25" in got[0]["plan"]


def test_push_has_a_section_for_other_strategy_targets_or_says_there_are_none():
    from dashboard.subscription_bridge import notify_result
    record = {"id": "AI-1", "ts": "2026-11-03T09:26:00", "regime": "修复观察", "action": "空仓等待",
              "confidence": 0.58, "market": "m", "sector": "s", "analysis": {}}
    latest = {"portfolio": {"posture": "防守", "positionPct": 0, "orders": {}}, "execution": {"fills": [], "skipped": []}}
    targets = [{"strategy": "高标接力", "code": "001219", "name": "甲", "plan": "6 板；竞价高开 +4.2%，在 3~6% 区间"}]
    notifier = FakeNotifier()
    notify_result(notifier, record, latest, True, quotes={}, targets=targets)
    body = notifier.messages[0][1]
    assert "【其他策略标的】" in body and "高标接力" in body and "001219" in body and "在 3~6% 区间" in body
    notifier2 = FakeNotifier()
    notify_result(notifier2, record, latest, True, quotes={}, targets=[])
    assert "今天没有高标接力 / 弱转强的标的" in notifier2.messages[0][1]


def _zt(code, name, reason, hd="首板"):
    return {"code": code, "name": name, "reason": reason, "high_days": hd}


def test_theme_ladder_fetches_today_and_previous_day_once_and_labels_the_phase(tmp_path):
    from datetime import date
    from dashboard.subscription_bridge import _theme_ladder
    today_rows = [_zt("600001", "甲", "机器人", "2天2板"), _zt("600002", "乙", "机器人"), _zt("600003", "丙", "机器人")]
    prev_rows = [_zt(f"60001{i}", f"前{i}", "机器人") for i in range(1, 2)]
    calls = []

    def fetch(d):
        calls.append(d)
        return today_rows if d == date(2026, 11, 3) else prev_rows
    path = tmp_path / "theme_cache.json"
    got = _theme_ladder(datetime(2026, 11, 3, 10, 0), fetch, date(2026, 11, 2), path, clock=lambda: 1000.0)
    assert got[0]["theme"] == "机器人" and got[0]["n"] == 3 and got[0]["phase"] == "启动" and got[0]["leaders"] == ["甲"]
    assert sorted(calls) == [date(2026, 11, 2), date(2026, 11, 3)]
    _theme_ladder(datetime(2026, 11, 3, 10, 1), fetch, date(2026, 11, 2), path, clock=lambda: 1060.0)
    assert len(calls) == 2                                              # 120 秒内、同一天：不重复联网
    _theme_ladder(datetime(2026, 11, 3, 10, 5), fetch, date(2026, 11, 2), path, clock=lambda: 1200.0)
    assert len(calls) == 3                                              # 过期只刷新今天，前一天不再取


def test_theme_ladder_failure_returns_nothing_instead_of_breaking_the_snapshot(tmp_path):
    from datetime import date
    from dashboard.subscription_bridge import _theme_ladder

    def boom(d):
        raise TimeoutError("同花顺超时")
    assert _theme_ladder(datetime(2026, 11, 3, 10, 0), boom, date(2026, 11, 2), tmp_path / "c.json") == []


def test_push_shows_the_theme_ladder_section():
    from dashboard.subscription_bridge import notify_result
    record = {"id": "AI-1", "ts": "2026-11-03T10:00:00", "regime": "修复观察", "action": "空仓等待",
              "confidence": 0.58, "market": "m", "sector": "s", "analysis": {}}
    latest = {"portfolio": {"posture": "防守", "positionPct": 0, "orders": {}}, "execution": {"fills": [], "skipped": []}}
    ladder = [{"theme": "机器人", "n": 12, "maxb": 5, "levels": 3, "leaders": ["甲", "乙"], "prev_n": 7, "phase": "发酵"}]
    n = FakeNotifier()
    notify_result(n, record, latest, True, quotes={}, targets=[], ladder=ladder)
    body = n.messages[0][1]
    assert "【题材梯队】" in body and "机器人" in body and "涨停 12 家" in body and "最高 5 板（甲、乙）" in body and "发酵" in body
    n2 = FakeNotifier()
    notify_result(n2, record, latest, True, quotes={}, targets=[], ladder=[])
    assert "今天还没有成型的题材梯队" in n2.messages[0][1]


def test_stale_answer_is_still_pushed_as_a_delayed_analysis_but_never_executed(tmp_path):
    """2026-11-03：机器被占满，一轮要 6~8 分钟，答案回来行情已过 180 秒被拒，整个下午一条推送都没有。
    过期的答案不能按旧价让模拟账户成交，但分析要照样推给 dj，标题写明延迟。"""
    snapshot, answers = _files(tmp_path)
    notifier = FakeNotifier()
    with pytest.raises(ValueError, match="有效期"):
        submit_answers(snapshot, answers, tmp_path / "decisions.jsonl", tmp_path / "paper.sqlite",
                       clock=lambda: NOW + timedelta(seconds=400), notifier=notifier)
    assert len(notifier.messages) == 1 and notifier.flushed is True
    title, body = notifier.messages[0]
    assert "延迟" in title and "修复观察" in title
    assert "已过期" in body and "订阅集成测试" in body and "没有处理模拟账户" in body
    assert not (tmp_path / "paper.sqlite").exists() or sqlite3.connect(tmp_path / "paper.sqlite").execute(
        "SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_a_push_failure_on_a_stale_answer_does_not_hide_the_staleness_error(tmp_path):
    snapshot, answers = _files(tmp_path)
    with pytest.raises(ValueError, match="有效期"):
        submit_answers(snapshot, answers, tmp_path / "decisions.jsonl", tmp_path / "paper.sqlite",
                       clock=lambda: NOW + timedelta(seconds=400), notifier=FakeNotifier(fail=True))


def _snapshot_service(codes, quotes, held=()):
    class Market:
        def payload(self, blocking=False):
            return {"marketBreadth": {"complete": True, "stale": False}, "sectorBreadth": {"complete": True, "stale": False}}

    class Connection:
        def close(self):
            pass

    class Paper:
        def _connect(self):
            return Connection()

        def _reconcile(self, conn, now):
            pass

        def _candidate_codes(self, conn, context):
            return list(held) if not context.get("sectorBreadth") else list(codes)

        def _account_snapshot(self, conn, quotes):
            return {"cash": 1000.0, "equity": 1000.0, "marketValue": 0.0, "positions": []}

        @staticmethod
        def _quote_payload(quotes):
            return {c: {"name": q.name, "price": q.price, "pct": q.pct} for c, q in quotes.items()}

    class Service:
        market = Market()
        paper = Paper()

        def payload(self):
            return {"snapshot": {"phase": "盘中"}, **Market().payload(), "marketBreadth": {"complete": True, "stale": False},
                    "sectorBreadth": {"complete": True, "stale": False, "leaders": [{}]}}

        def _paper_quotes(self, cs):
            return {c: quotes[c] for c in cs if c in quotes}
    return Service()


def test_snapshot_drops_a_stale_candidate_instead_of_skipping_the_whole_round(monkeypatch, tmp_path):
    """2026-11-03：冷门票/临停票的行情时间戳可能比 180 秒旧，原来只要有一只候选这样，整轮（含所有推送）都被跳过。"""
    import dashboard.subscription_bridge as sb
    stale = Quote(code="600002", name="冷门", price=10.0, pre_close=10.0, open=10.0, high=10.0, low=10.0, volume=1, amount=1,
                  limit_up=11.0, limit_down=9.0, ts=NOW - timedelta(minutes=10))
    fresh = Quote(code="600001", name="活跃", price=10.5, pre_close=10.0, open=10.0, high=10.6, low=10.0, volume=1, amount=1,
                  limit_up=11.0, limit_down=9.0, ts=NOW)
    service = _snapshot_service(["600001", "600002"], {"600001": fresh, "600002": stale})
    monkeypatch.setattr(sb, "DashboardService", lambda: service)
    monkeypatch.setattr(sb, "_theme_ladder", lambda *a, **k: [])
    monkeypatch.setattr(sb, "_strategy_targets", lambda *a, **k: [])
    monkeypatch.setattr(sb, "_board_context", lambda *a, **k: {"watch": [], "account": None, "fills": [], "sentiment": None})
    result = create_snapshot(tmp_path / "snapshot.json", clock=lambda: NOW, registry=tmp_path / "no-registry")
    assert result["eligible"] is True and result["codes"] == ["600001"]


def test_snapshot_still_skips_when_a_held_position_has_no_valid_quote(monkeypatch, tmp_path):
    import dashboard.subscription_bridge as sb
    stale = Quote(code="600002", name="持仓", price=10.0, pre_close=10.0, open=10.0, high=10.0, low=10.0, volume=1, amount=1,
                  limit_up=11.0, limit_down=9.0, ts=NOW - timedelta(minutes=10))
    service = _snapshot_service(["600002"], {"600002": stale}, held=["600002"])
    monkeypatch.setattr(sb, "DashboardService", lambda: service)
    monkeypatch.setattr(sb, "_board_context", lambda *a, **k: {"watch": [], "account": None, "fills": [], "sentiment": None})
    result = create_snapshot(tmp_path / "snapshot.json", clock=lambda: NOW, registry=tmp_path / "no-registry")
    assert result["eligible"] is False and "持仓" in result["reason"]
