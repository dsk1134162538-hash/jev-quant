import importlib
import os
import sys
import threading
from datetime import date, datetime, timedelta

import daemon
import desktop
from jevq.runlock import HELD_MSG, hold_run_lock


def _no_git_rev_parse(repo):
    """测试专用：从不真的调用 git，模拟读不到当前运行提交（视为「总是需要检查」）。"""
    return None


def _no_sync(repo, today, log, notifier, python_exe, running_commit):
    """测试专用：从不真的同步/上线，直接说「不需要上线」。"""
    return {"deploy": False}


def _boom_restart(repo, python_exe):
    raise AssertionError("real restart in tests")


def test_importing_daemon_and_desktop_does_not_change_cwd():
    before = os.getcwd()
    importlib.reload(daemon)
    importlib.reload(desktop)
    assert os.getcwd() == before

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
    rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=stop,
                     git_rev_parse=_no_git_rev_parse, sync_and_deploy=_no_sync,
                     restart=_boom_restart)
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
    rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=stop,
                     git_rev_parse=_no_git_rev_parse, sync_and_deploy=_no_sync,
                     restart=_boom_restart)
    assert rc == 0
    assert calls == [1]
    assert any("已有另一个交易进程在运行" in m for m in logged)


def test_main_retries_runner_on_lock_contention_until_success(monkeypatch, tmp_path):
    """08:50 后台被面板抢锁：runner 前两次抛 HELD_MSG，第三次成功 → runner 被调用 3 次，
    不能在锁竞争那次就把 last_run 设为今天（否则当天彻底放弃补跑）。"""
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    logged = []
    monkeypatch.setattr(daemon, "file_log", lambda msg: logged.append(msg))
    calls = []

    def runner(stop, log):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError(HELD_MSG)

    stop = FakeStop(trip_after=3)
    rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=stop,
                     git_rev_parse=_no_git_rev_parse, sync_and_deploy=_no_sync,
                     restart=_boom_restart)
    assert rc == 0
    assert len(calls) == 3
    assert any("60 秒后重试" in m for m in logged)


def test_main_gives_up_retry_after_report_at(monkeypatch, tmp_path):
    """一直被占用且已过当天 15:05：放弃重试，记日志，不再无限等下去。"""
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    logged = []
    monkeypatch.setattr(daemon, "file_log", lambda msg: logged.append(msg))
    calls = []

    def runner(stop, log):
        calls.append(1)
        raise RuntimeError(HELD_MSG)

    stop = FakeStop(trip_after=3)
    rc = daemon.main(runner=runner, clock=lambda: datetime(2026, 9, 23, 15, 6), stop=stop,
                     git_rev_parse=_no_git_rev_parse, sync_and_deploy=_no_sync,
                     restart=_boom_restart)
    assert rc == 0
    assert len(calls) == 1                          # 已过 15:05，不重试
    assert any("放弃重试" in m for m in logged)


def test_main_exits_immediately_when_daemon_lock_already_held(monkeypatch, tmp_path):
    db_path = tmp_path / "jevq.sqlite"
    monkeypatch.setattr(daemon, "DB_PATH", str(db_path))
    logged = []
    monkeypatch.setattr(daemon, "file_log", lambda msg: logged.append(msg))
    calls = []

    def runner(stop, log):
        calls.append(1)

    with hold_run_lock(tmp_path / "daemon.lock"):
        rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=FakeStop(),
                          git_rev_parse=_no_git_rev_parse, sync_and_deploy=_no_sync,
                          restart=_boom_restart)

    assert rc == 0
    assert calls == []
    assert logged


def test_main_calls_sync_and_deploy_when_runner_returns_a_report_path(monkeypatch, tmp_path):
    """runner 返回报告路径（说明今天是交易日、报告已写完）之后，daemon 应该调用
    sync_and_deploy，并把启动时记下的 running_commit 传进去。"""
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))

    def runner(stop, log):
        stop.set()
        return tmp_path / "reports" / "2026-09-23.md"

    sync_calls = []

    def fake_sync(repo, today, log, notifier, python_exe, running_commit):
        sync_calls.append((today, running_commit))
        return {"deploy": False}

    rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=FakeStop(),
                     git_rev_parse=lambda repo: "abc1234",
                     sync_and_deploy=fake_sync, restart=_boom_restart)
    assert rc == 0
    assert sync_calls == [(date(2026, 9, 23), "abc1234")]


def test_main_skips_sync_when_runner_returns_none(monkeypatch, tmp_path):
    """非交易日/报告没写出来时 runner 返回 None，不应该触发同步/上线。"""
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))

    def runner(stop, log):
        stop.set()
        return None

    def fake_sync(*a, **kw):
        raise AssertionError("不该在非交易日调用同步")

    rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=FakeStop(),
                     git_rev_parse=_no_git_rev_parse, sync_and_deploy=fake_sync,
                     restart=_boom_restart)
    assert rc == 0


def test_main_restarts_and_exits_loop_when_deploy_succeeds(monkeypatch, tmp_path):
    """sync_and_deploy 返回 deploy=True：应该调用注入的自我重启函数，并且主循环随之退出
    （不再等待/调用下一轮 runner）——当前进程该释放 daemon.lock 正常退出了。"""
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    calls = []

    def runner(stop, log):
        calls.append(1)
        return tmp_path / "reports" / "2026-09-23.md"

    def fake_sync(repo, today, log, notifier, python_exe, running_commit):
        return {"deploy": True, "commit": "def4567"}

    restart_calls = []

    def fake_restart(repo, python_exe):
        restart_calls.append((repo, python_exe))

    rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=FakeStop(trip_after=3),
                     repo=tmp_path, git_rev_parse=lambda repo: "abc1234",
                     sync_and_deploy=fake_sync, restart=fake_restart)
    assert rc == 0
    assert len(calls) == 1                 # 上线后立即退出，没有进入下一轮
    assert restart_calls == [(tmp_path, daemon.Path(sys.executable))]


def test_main_sync_exception_does_not_crash_and_next_day_still_runs(monkeypatch, tmp_path):
    """同步/上线过程本身抛异常：只记日志，不能打断守护进程——main() 正常继续它的主循环
    （沿用其他测试同样的断言方式：不崩、rc==0、异常被记进日志、runner 正常跑过一次）。"""
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    logged = []
    monkeypatch.setattr(daemon, "file_log", lambda msg: logged.append(msg))
    calls = []

    def runner(stop, log):
        calls.append(1)
        return tmp_path / "reports" / "2026-09-23.md"

    def fake_sync(*a, **kw):
        raise RuntimeError("网络炸了")

    stop = FakeStop(trip_after=3)
    rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=stop,
                     git_rev_parse=lambda repo: "abc1234",
                     sync_and_deploy=fake_sync, restart=_boom_restart)
    assert rc == 0
    assert calls == [1]
    assert any("同步/上线异常" in m for m in logged)
