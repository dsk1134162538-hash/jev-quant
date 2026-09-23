import threading
from datetime import date, datetime, timedelta

import daemon
from jevq.runlock import hold_run_lock

WED_0800 = datetime(2026, 9, 23, 8, 0)      # 2026-09-23 是周三
WED_1000 = datetime(2026, 9, 23, 10, 0)
FRI_1600 = datetime(2026, 9, 25, 16, 0)     # 2026-09-25 是周五
SAT_1200 = datetime(2026, 9, 26, 12, 0)     # 2026-09-26 是周六


def test_plan_next_run_before_start_today():
    assert daemon.plan_next_run(WED_0800, None) == datetime(2026, 9, 23, 8, 50)


def test_plan_next_run_after_start_not_yet_run_today():
    assert daemon.plan_next_run(WED_1000, None) == WED_1000


def test_plan_next_run_already_run_today_goes_to_tomorrow():
    assert daemon.plan_next_run(WED_1000, date(2026, 9, 23)) == datetime(2026, 9, 24, 8, 50)


def test_plan_next_run_friday_already_run_goes_to_monday():
    assert daemon.plan_next_run(FRI_1600, date(2026, 9, 25)) == datetime(2026, 9, 28, 8, 50)


def test_plan_next_run_weekend_goes_to_monday():
    assert daemon.plan_next_run(SAT_1200, None) == datetime(2026, 9, 28, 8, 50)


class FakeStop:
    """threading.Event 的替身：wait() 立即返回，不真的睡眠，测试跑得快。"""

    def __init__(self, trip_after=None):
        self._set = False
        self._calls = 0
        self._trip_after = trip_after

    def wait(self, timeout=None):
        self._calls += 1
        if self._trip_after is not None and self._calls >= self._trip_after:
            self._set = True
        return self._set

    def is_set(self):
        return self._set

    def set(self):
        self._set = True


def test_main_calls_runner_once_then_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    calls = []

    def runner(stop, log):
        calls.append(1)
        log("hello")
        stop.set()

    stop = FakeStop()
    rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=stop)
    assert rc == 0
    assert calls == [1]


def test_main_logs_runner_exception_and_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    logged = []
    monkeypatch.setattr(daemon, "file_log", lambda msg: logged.append(msg))
    calls = []

    def runner(stop, log):
        calls.append(1)
        raise RuntimeError("已有另一个交易进程在运行")

    # 第一次运行后 last_run == today，plan_next_run 会算到明天；FakeStop 在被
    # 等待几次后置位，让主循环在不真的睡眠的情况下退出。
    stop = FakeStop(trip_after=3)
    rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=stop)
    assert rc == 0
    assert calls == [1]
    assert any("已有另一个交易进程在运行" in m for m in logged)


def test_main_exits_immediately_when_daemon_lock_already_held(monkeypatch, tmp_path):
    db_path = tmp_path / "jevq.sqlite"
    monkeypatch.setattr(daemon, "DB_PATH", str(db_path))
    logged = []
    monkeypatch.setattr(daemon, "file_log", lambda msg: logged.append(msg))
    calls = []

    def runner(stop, log):
        calls.append(1)

    with hold_run_lock(tmp_path / "daemon.lock"):
        rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=FakeStop())

    assert rc == 0
    assert calls == []
    assert logged
