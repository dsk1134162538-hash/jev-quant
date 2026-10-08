import json
import threading
from datetime import date as _date
from datetime import datetime
from pathlib import Path

import pytest

from factories import quote
from jevq import db
from jevq.app.api import Api
from jevq.data import custom_store
from jevq.ledger import Ledger
from jevq.runlock import DAEMON_LOCK_NAME, hold_run_lock

T = datetime(2026, 9, 23, 14, 50)


@pytest.fixture
def paths(tmp_path):
    return str(tmp_path / "t.sqlite"), tmp_path / "reports"


def make_api(paths, runner=None, fetch=None):
    return Api(db_path=paths[0], report_dir=str(paths[1]),
               runner=runner or (lambda **kw: None), fetch_quotes=fetch or (lambda codes: {}))


def test_status_marks_positions_to_fetched_prices(paths):
    conn = db.connect(paths[0])
    Ledger(conn).buy("rebound_main", quote("600001", pct=-1.0), T, "x")
    conn.close()
    api = make_api(paths, fetch=lambda codes: {"600001": quote("600001", pct=1.0)})
    s = api.status()
    board_main, board_shadow, main, shadow = s["accounts"]
    assert main["id"] == "rebound_main" and main["positions"][0]["price"] == 10.1
    assert main["positions"][0]["pnl_pct"] == round((10.1 / 9.9 - 1) * 100, 2)
    assert main["strategy"] == "rebound" and main["strategy_name"] == "超跌反弹"
    assert shadow["id"] == "rebound_shadow" and shadow["equity"] == 30000 and shadow["positions"] == []
    assert board_main["id"] == "board_main" and board_main["strategy"] == "board"
    assert board_main["strategy_name"] == "扫首板"
    assert board_shadow["id"] == "board_shadow" and board_shadow["strategy"] == "board"
    assert s["running"] is False and s["phase"] == "未运行"
    assert s["warning"] is None


def test_retired_lowbuy_accounts_stay_off_panel_but_old_fills_keep_their_name(paths):
    """低吸下线：库里 main/shadow 的历史（现金、持仓、净值、成交）不删，但面板账户和净值曲线不再出现；
    查到历史成交时显示「已下线」的名字。"""
    conn = db.connect(paths[0])
    conn.execute("INSERT INTO cash VALUES ('main', 31000.0), ('shadow', 30500.0)")
    conn.execute("INSERT INTO positions VALUES ('main', '600001', 100, 1005.1, 10.0, '2026-09-21')")
    conn.execute("INSERT INTO equity VALUES ('2026-09-21', 'main', 31000.0), ('2026-09-21', 'shadow', 30500.0), "
                 "('2026-09-21', 'rebound_main', 30100.0)")
    conn.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
                 "VALUES ('main', '600001', 'buy', 10.0, 100, 5.1, '2026-09-21T10:00:00', '低吸-1.0%')")
    conn.commit()
    conn.close()
    api = make_api(paths)
    assert [a["id"] for a in api.status()["accounts"]] == ["board_main", "board_shadow", "rebound_main",
                                                          "rebound_shadow"]
    curve = api.equity_curve()
    assert "main" not in curve and "shadow" not in curve
    assert curve["rebound_main"] == [["2026-09-21", 30100.0]]
    [f] = api.fills("2026-09-21")
    assert f["account_id"] == "main" and f["account"] == "低吸·主(已下线)"


def test_status_surfaces_warning_from_state(paths):
    api = make_api(paths)
    api._state["warning"] = "5 日涨幅数据不足，今日只卖不买"
    assert api.status()["warning"] == "5 日涨幅数据不足，今日只卖不买"


def test_status_defaults_board_watch_to_empty_list(paths):
    api = make_api(paths)
    assert api.status()["board_watch"] == []


def test_status_surfaces_board_watch_from_state(paths):
    api = make_api(paths)
    api._state["board_watch"] = [{"code": "600001", "name": "股票甲"}]
    assert api.status()["board_watch"] == [{"code": "600001", "name": "股票甲"}]


def test_board_stats_returns_summary(paths):
    conn = db.connect(paths[0])
    from datetime import date as d
    from jevq.data.board_events import fill_next_day, record_day
    record_day(conn, d(2026, 9, 22), [
        {"代码": "600001", "名称": "甲", "首次封板时间": "093000", "连板数": 1}], {})
    fill_next_day(conn, d(2026, 9, 22), {"600001": quote("600001", pct=2.0, open_pct=1.0, pre_close=10.0,
                                                          ts=datetime(2026, 9, 23, 15, 0))}, d(2026, 9, 23))
    conn.commit()
    conn.close()
    api = make_api(paths)
    stats = api.board_stats()
    assert stats == [{"bucket": "早盘", "count": 1, "avg_next_open": 1.0, "avg_next_close": 2.0, "board_rate": 0.0}]


def test_fills_decisions_curve(paths):
    conn = db.connect(paths[0])
    Ledger(conn).buy("rebound_main", quote("600001", pct=-1.0), T, "超跌")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) "
                 "VALUES ('2026-09-23T10:00:00', '600002', 'reject', '新增减持', 'rebound')")
    conn.execute("INSERT INTO equity VALUES ('2026-09-23', 'rebound_main', 30100)")
    conn.commit()
    conn.close()
    api = make_api(paths)
    assert api.fills("2026-09-23")[0]["side"] == "买"
    decisions = api.decisions("2026-09-23")
    assert len(decisions) == 1
    d = decisions[0]
    assert d["id"] and d["has_trace"] is False
    assert {k: v for k, v in d.items() if k not in ("id", "has_trace")} == {
        "time": "10:00", "code": "600002", "name": "", "verdict": "reject", "detail": "新增减持"}
    assert api.equity_curve() == {"board_main": [], "board_shadow": [],
                                  "rebound_main": [["2026-09-23", 30100.0]], "rebound_shadow": []}


def test_decisions_filtered_by_strategy(paths):
    conn = db.connect(paths[0])
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES "
                 "('2026-09-23T10:00:00', '600002', 'reject', '新增减持', 'rebound')")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES "
                 "('2026-09-23T10:01:00', '300001', 'pass', '首板', 'board')")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail) VALUES "
                 "('2026-09-23T10:02:00', '600003', 'reject', '旧数据无 strategy')")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES "
                 "('2026-09-23T10:03:00', '', 'sentiment', '{}', 'market')")
    conn.commit()
    conn.close()
    api = make_api(paths)
    assert [d["code"] for d in api.decisions("2026-09-23", "board")] == ["300001"]
    assert [d["code"] for d in api.decisions("2026-09-23", "rebound")] == ["600002"]
    # 低吸下线：没有 strategy 的老记录（当年的低吸）不再默认归到低吸，只在「全部」里
    assert api.decisions("2026-09-23", "lowbuy") == []
    assert [d["code"] for d in api.decisions("2026-09-23")] == ["市场", "600003", "300001", "600002"]


def test_decision_detail(paths):
    conn = db.connect(paths[0])
    trace = {"candidate": {"name": "股票甲", "rank": 1, "of": 3}, "notices": [], "news": None,
             "market": {"mode": "tail"}, "outcome": {"bought": False, "shares": None, "price": None}}
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, trace) VALUES (?, ?, ?, ?, ?)",
                 ("2026-09-23T10:00:00", "600001", "pass", "消息面中性", json.dumps(trace, ensure_ascii=False)))
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, trace) VALUES (?, ?, ?, ?, ?)",
                 ("2026-09-23T10:01:00", "600002", "reject", "x", "{broken"))
    conn.commit()
    conn.close()
    api = make_api(paths)
    rows = api.decisions("2026-09-23")
    assert [r["has_trace"] for r in rows] == [True, True]
    d = api.decision(rows[1]["id"])
    assert d["code"] == "600001" and d["trace"]["candidate"]["rank"] == 1 and d["time"] == "10:00"
    assert api.decision(rows[0]["id"])["trace"] is None      # 损坏的 JSON
    assert api.decision(999) is None
    assert api.decision("x") is None
    assert api.decision(None) is None


def test_reports_and_path_traversal(paths):
    rep = paths[1]
    rep.mkdir()
    (rep / "2026-09-23.md").write_text("# 复盘", encoding="utf-8")
    (rep / "notes.txt").write_text("x", encoding="utf-8")
    api = make_api(paths)
    assert api.reports() == ["2026-09-23.md"]
    assert api.report("2026-09-23.md") == "# 复盘"
    with pytest.raises(ValueError):
        api.report("../jevq/config.py")


def test_start_stop_runs_runner_in_thread(paths):
    started = threading.Event()

    def runner(stop, log, state):
        state["phase"] = "盘中"
        log("hello")
        started.set()
        stop.wait(5)

    api = make_api(paths, runner=runner)
    assert api.start()["ok"]
    assert started.wait(2)
    assert api.is_running() and api.status()["phase"] == "盘中"
    assert api.start()["ok"] is False
    api.stop()
    api._thread.join(2)
    assert not api.is_running()
    logs = api.logs()
    assert any("hello" in line for line in logs) and any("运行结束" in line for line in logs)


def test_runner_error_is_logged(paths):
    def runner(stop, log, state):
        raise RuntimeError("缺少环境变量 TYPESAFE_API_KEY")

    api = make_api(paths, runner=runner)
    api.start()
    api._thread.join(2)
    assert any("TYPESAFE_API_KEY" in line for line in api.logs())


def test_names_from_state_then_quotes_cache(paths):
    conn = db.connect(paths[0])
    ledger = Ledger(conn)
    ledger.buy("board_main", quote("600001", pct=-1.0), T, "x")
    ledger.buy("board_shadow", quote("600002", pct=-1.0), T, "y")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail) VALUES "
                 "('2026-09-23T10:00:00', '600001', 'pass', 'ok'), ('2026-09-23T10:00:00', '', 'sentiment', '{}')")
    conn.commit()
    conn.close()
    fetched = []

    def fetch(codes):
        fetched.append(sorted(codes))
        return {c: quote(c, pct=0.0, name="行情名" + c[-1]) for c in codes}

    api = make_api(paths, fetch=fetch)
    api._state["names"] = {"600001": "股票甲"}
    s = api.status()
    assert s["accounts"][0]["positions"][0]["name"] == "股票甲"
    assert s["accounts"][1]["positions"][0]["name"] == "行情名2"
    assert {f["code"]: f["name"] for f in api.fills("2026-09-23")} == {"600001": "股票甲", "600002": "行情名2"}
    assert [d["name"] for d in api.decisions("2026-09-23")] == ["", "股票甲"]
    assert fetched == [["600001", "600002"]]          # status 一次请求同时拿价格和名称；之后命中缓存


def test_query_methods_never_open_write_connection(paths, monkeypatch):
    conn = db.connect(paths[0])
    Ledger(conn).buy("board_main", quote("600001", pct=-1.0), T, "x")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail) "
                 "VALUES ('2026-09-23T10:00:00', '600002', 'reject', 'x')")
    conn.commit()
    conn.close()
    api = make_api(paths)

    def boom(*a, **kw):
        raise AssertionError("query method opened a write connection")

    monkeypatch.setattr(db, "connect", boom)
    assert api.status()["accounts"][0]["positions"][0]["code"] == "600001"
    assert len(api.fills("2026-09-23")) == 1
    rows = api.decisions("2026-09-23")
    assert api.decision(rows[0]["id"])["code"] == "600002"
    assert api.equity_curve() == {"board_main": [], "board_shadow": [], "rebound_main": [], "rebound_shadow": []}


def test_api_init_creates_fresh_db(paths):
    api = make_api(paths)
    s = api.status()
    assert [a["equity"] for a in s["accounts"]] == [30000] * 4


def test_status_includes_active_custom_strategy_accounts(paths):
    conn = db.connect(paths[0])
    conn.close()
    conn = db.connect(paths[0])
    today = _date.today()
    yesterday = today.fromordinal(today.toordinal() - 1)
    spec = {"name": "尾盘弱转强", "capital": 20000,
            "select": {"conditions": [{"field": "pct", "op": "between", "value": [-2, 0]}]},
            "entry": {"conditions": []},
            "exits": [{"when": {"conditions": [{"field": "pct", "op": "<=", "value": -3}]}, "reason": "止损"}],
            "jev": {"notices": True, "news": True}}
    sid, version = custom_store.save(conn, spec, yesterday, [yesterday, today])
    assert sid == "c1"
    Ledger(conn, init=False).ensure_account("c1_main", 20000)
    Ledger(conn, init=False).ensure_account("c1_shadow", 20000)
    conn.close()
    api = make_api(paths)
    s = api.status()
    ids = [a["id"] for a in s["accounts"]]
    assert "c1_main" in ids and "c1_shadow" in ids
    main = next(a for a in s["accounts"] if a["id"] == "c1_main")
    assert main["name"] == "尾盘弱转强·主(Jev)" and main["strategy"] == "c1"
    assert main["strategy_name"] == "尾盘弱转强" and main["equity"] == 20000
    # 净值基准是这个策略自己冻结的资金（20000），不是内置策略的 INITIAL_CASH（30000）；
    # 空仓时净值等于本金，收益率必须是 0，不能因为拿错基准变成 (20000/30000-1) 这种负数。
    assert main["return_pct"] == 0.0


def test_status_background_true_when_another_handle_holds_runner_lock(paths):
    api = make_api(paths)
    lock_path = Path(paths[0]).parent / "runner.lock"
    with hold_run_lock(lock_path):
        s = api.status()
    assert s["background"] is True and s["running"] is False
    assert api.status()["background"] is False


def test_status_daemon_true_when_daemon_lock_held(paths):
    api = make_api(paths)
    lock_path = Path(paths[0]).parent / DAEMON_LOCK_NAME
    with hold_run_lock(lock_path):
        s = api.status()
    assert s["daemon"] is True
    assert api.status()["daemon"] is False


def test_start_defers_to_daemon_when_daemon_lock_held(paths):
    started = threading.Event()

    def runner(stop, log, state):
        started.set()

    api = make_api(paths, runner=runner)
    lock_path = Path(paths[0]).parent / DAEMON_LOCK_NAME
    with hold_run_lock(lock_path):
        result = api.start()
    assert result == {"ok": False, "msg": "后台程序已接管今日交易，面板只需查看"}
    assert not api.is_running()
    assert not started.wait(0.2)


def test_status_accounts_carry_capital_baseline(paths):
    """面板净值曲线按各账户自己的本金折算收益率，status 要把基准资金带出来。"""
    conn = db.connect(paths[0])
    today = _date.today()
    yesterday = today.fromordinal(today.toordinal() - 1)
    spec = {"name": "大资金", "capital": 100000,
            "select": {"conditions": [{"field": "pct", "op": "between", "value": [-2, 0]}]},
            "exits": [{"when": {"conditions": [{"field": "pct", "op": "<=", "value": -3}]}, "reason": "止损"}]}
    custom_store.save(conn, spec, yesterday, [yesterday, today])
    Ledger(conn, init=False).ensure_account("c1_main", 100000)
    conn.close()
    s = make_api(paths).status()
    by_id = {a["id"]: a for a in s["accounts"]}
    assert by_id["rebound_main"]["baseline"] == 30000
    assert by_id["c1_main"]["baseline"] == 100000
    # c1_shadow 还没开户（交易进程尚未构造 CustomStrategy）：按本金空仓显示，status 不能抛异常
    shadow = by_id["c1_shadow"]
    assert shadow["equity"] == 100000 and shadow["return_pct"] == 0.0 and shadow["positions"] == []


def test_fills_carry_account_id(paths):
    """成交表按账户上色，需要账户 id（显示名可能重名/改名）。"""
    conn = db.connect(paths[0])
    Ledger(conn).buy("board_main", quote("600001", pct=-1.0), T, "首板")
    conn.close()
    f = make_api(paths).fills("2026-09-23")[0]
    assert f["account_id"] == "board_main" and f["account"] == "首板·主(Jev)"


def test_rules_list_includes_builtin_readonly_summaries(paths):
    result = make_api(paths).rules_list()
    assert result["ok"] and result["rules"] == []
    builtin = {b["id"]: b for b in result["builtin"]}
    assert set(builtin) == {"board", "rebound"}                  # 低吸已下线
    titles = [s["title"] for s in builtin["board"]["sections"]]
    assert titles[0] == "选股" and all(s["lines"] for s in builtin["board"]["sections"])
    board_lines = [line for s in builtin["board"]["sections"] for line in s["lines"]]
    assert any("情绪闸门" in line and "只记录不拦截" in line and "30%" in line and "10 家" in line and "3 板" in line
               for line in board_lines)
    assert any("hi189" in line and "只调顺序" in line for line in board_lines)
    rebound_lines = [line for s in builtin["rebound"]["sections"] for line in s["lines"]]
    assert builtin["rebound"]["accounts"] == ["rebound_main", "rebound_shadow"]
    assert any("hi189" in line for line in rebound_lines)
    assert any("-3%" in line for line in rebound_lines)
    assert any("-20%" in line and "0%" in line for line in rebound_lines)
    assert any("40" in line and "2 连板" in line for line in rebound_lines)
    assert any("从低到高" in line for line in rebound_lines)
    # 成交量条件与卖出后再买的门槛：面板说明要和代码一致
    assert any("连续缩量" in line and "至少 3 天" in line and "09:45" in line for line in rebound_lines)
    assert any("4 天" in line and "优先" in line for line in rebound_lines)
    assert any("当天卖出" in line and "低 1%" in line and "再买" in line for line in rebound_lines)


def test_knowledge_points_all_cite_existing_summarized_articles(paths):
    """知识库：专题手册里每条要点都要指向索引里真实存在、且有摘要的文章。"""
    kb = make_api(paths).knowledge()
    articles = {a["id"]: a for a in kb["articles"]}
    assert len(articles) == len(kb["articles"]) > 300
    topic_ids = [t["id"] for t in kb["topics"]]
    assert len(topic_ids) == len(set(topic_ids)) >= 8
    for t in kb["topics"]:
        assert t["name"] and t["intro"] and t["sections"]
        for s in t["sections"]:
            assert s["title"] and s["points"]
            for p in s["points"]:
                assert p["text"] and p["refs"] and len(set(p["refs"])) == len(p["refs"])
                for ref in p["refs"]:
                    assert articles[ref]["points"] and not articles[ref]["dup"], (p["text"], ref)


def test_knowledge_articles_carry_title_date_link_and_known_topic(paths):
    kb = make_api(paths).knowledge()
    topic_ids = {t["id"] for t in kb["topics"]}
    ids = {a["id"] for a in kb["articles"]}
    for a in kb["articles"]:
        assert a["title"].strip() and "]]" not in a["title"]
        assert len(a["date"]) == 10 and a["url"].startswith("https://")
        assert a["topic"] in topic_ids or a["topic"] == ""          # "" = 没归进专题（非交易内容或原文已删）
        assert a["dup"] is None or (a["dup"] in ids and a["dup"] != a["id"])
        assert a["deleted"] == (not a["points"] and a["dup"] is None)
    assert kb["source"]["url"].startswith("https://t.me/")


def _sentiment_row(paths, ts, label, overseas):
    conn = db.connect(paths[0])
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, '', 'sentiment', ?, NULL, 'market')",
                 (ts, json.dumps({"stats": {}, "label": label, "overseas": overseas}, ensure_ascii=False)))
    conn.commit()
    conn.close()


def test_status_header_reads_todays_latest_sentiment_when_daemon_is_trading(paths):
    """后台 daemon 在交易时面板自己的状态是空的：顶栏的情绪/外围读今天最近一条情绪记录（2026-10-08 顶栏一直是「–」）。"""
    today = _date.today().isoformat()
    _sentiment_row(paths, f"{today}T10:00:00", "正常", "中性")
    _sentiment_row(paths, f"{today}T11:27:02", "偏弱", "偏弱")
    s = make_api(paths).status()
    assert (s["sentiment"], s["overseas"]) == ("偏弱", "偏弱")


def test_status_header_ignores_sentiment_from_previous_days(paths):
    _sentiment_row(paths, "2020-01-02T14:56:00", "火热", "偏强")
    s = make_api(paths).status()
    assert s["sentiment"] is None and s["overseas"] is None


def test_status_header_prefers_panel_state_when_panel_itself_is_running(paths):
    _sentiment_row(paths, f"{_date.today().isoformat()}T11:27:02", "偏弱", "偏弱")
    api = make_api(paths)
    api._state.update({"sentiment": "火热", "overseas": "偏强"})
    s = api.status()
    assert (s["sentiment"], s["overseas"]) == ("火热", "偏强")
