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
    assert api.decisions("2026-09-23") == [
        {"time": "10:00", "code": "600002", "verdict": "reject", "detail": "新增减持"}]
    assert api.equity_curve() == {"main": [["2026-09-23", 30100.0]], "shadow": []}


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
