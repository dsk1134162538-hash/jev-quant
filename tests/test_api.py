import json
import threading
from datetime import datetime
from pathlib import Path

import pytest

from datetime import date as _date

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
    Ledger(conn).buy("main", quote("600001", pct=-1.0), T, "x")
    conn.close()
    api = make_api(paths, fetch=lambda codes: {"600001": quote("600001", pct=1.0)})
    s = api.status()
    main, shadow, board_main, board_shadow = s["accounts"]
    assert main["id"] == "main" and main["positions"][0]["price"] == 10.1
    assert main["positions"][0]["pnl_pct"] == round((10.1 / 9.9 - 1) * 100, 2)
    assert main["strategy"] == "lowbuy" and main["strategy_name"] == "低吸"
    assert shadow["equity"] == 30000 and shadow["positions"] == []
    assert board_main["id"] == "board_main" and board_main["strategy"] == "board"
    assert board_main["strategy_name"] == "扫首板"
    assert board_shadow["id"] == "board_shadow" and board_shadow["strategy"] == "board"
    assert s["running"] is False and s["phase"] == "未运行"
    assert s["warning"] is None


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
    Ledger(conn).buy("main", quote("600001", pct=-1.0), T, "低吸")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail) "
                 "VALUES ('2026-09-23T10:00:00', '600002', 'reject', '新增减持')")
    conn.execute("INSERT INTO equity VALUES ('2026-09-23', 'main', 30100)")
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
    assert api.equity_curve() == {"main": [["2026-09-23", 30100.0]], "shadow": [],
                                  "board_main": [], "board_shadow": []}


def test_decisions_filtered_by_strategy(paths):
    conn = db.connect(paths[0])
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES "
                 "('2026-09-23T10:00:00', '600002', 'reject', '新增减持', 'lowbuy')")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES "
                 "('2026-09-23T10:01:00', '300001', 'pass', '首板', 'board')")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail) VALUES "
                 "('2026-09-23T10:02:00', '600003', 'reject', '旧数据无 strategy')")
    conn.commit()
    conn.close()
    api = make_api(paths)
    board = api.decisions("2026-09-23", "board")
    assert [d["code"] for d in board] == ["300001"]
    lowbuy = api.decisions("2026-09-23", "lowbuy")
    assert sorted(d["code"] for d in lowbuy) == ["600002", "600003"]     # NULL 视为 lowbuy
    assert len(api.decisions("2026-09-23")) == 3


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
    ledger.buy("main", quote("600001", pct=-1.0), T, "x")
    ledger.buy("shadow", quote("600002", pct=-1.0), T, "y")
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
    Ledger(conn).buy("main", quote("600001", pct=-1.0), T, "x")
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
    assert api.equity_curve() == {"main": [], "shadow": [], "board_main": [], "board_shadow": []}


def test_api_init_creates_fresh_db(paths):
    api = make_api(paths)
    s = api.status()
    assert [a["equity"] for a in s["accounts"]] == [30000, 30000, 30000, 30000]


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
