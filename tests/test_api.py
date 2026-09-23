import json
import threading
from datetime import datetime

import pytest

from factories import quote
from jevq import db
from jevq.app.api import Api
from jevq.ledger import Ledger

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
    main, shadow = s["accounts"]
    assert main["id"] == "main" and main["positions"][0]["price"] == 10.1
    assert main["positions"][0]["pnl_pct"] == round((10.1 / 9.9 - 1) * 100, 2)
    assert shadow["equity"] == 30000 and shadow["positions"] == []
    assert s["running"] is False and s["phase"] == "未运行"


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
    assert api.equity_curve() == {"main": [["2026-09-23", 30100.0]], "shadow": []}


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
    assert api.equity_curve() == {"main": [], "shadow": []}


def test_api_init_creates_fresh_db(paths):
    api = make_api(paths)
    s = api.status()
    assert [a["equity"] for a in s["accounts"]] == [30000, 30000]
