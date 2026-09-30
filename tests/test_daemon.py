import importlib
import os
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

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


def test_main_starts_watchdog_thread_once_lock_is_held(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    calls = []

    def watchdog_start(stop, notifier, repo, python_exe):
        calls.append((stop, notifier, repo, python_exe))

    def runner(stop, log):
        stop.set()

    stop = FakeStop()
    fake_notifier = object()
    rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=stop,
                     git_rev_parse=_no_git_rev_parse, sync_and_deploy=_no_sync,
                     restart=_boom_restart, notifier=fake_notifier,
                     repo=tmp_path, python_exe=tmp_path / "python.exe",
                     watchdog_start=watchdog_start)
    assert rc == 0
    assert calls == [(stop, fake_notifier, tmp_path, tmp_path / "python.exe")]


def test_main_does_not_start_watchdog_when_not_injected(monkeypatch, tmp_path):
    """不传 watchdog_start 时（现有测试的情况）不该有任何看门狗线程被起来。"""
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    before = threading.active_count()

    def runner(stop, log):
        stop.set()

    stop = FakeStop()
    rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=stop,
                     git_rev_parse=_no_git_rev_parse, sync_and_deploy=_no_sync,
                     restart=_boom_restart)
    assert rc == 0
    assert threading.active_count() == before


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


def test_main_after_deploy_waits_for_old_daemon_to_release_lock(monkeypatch, tmp_path):
    """自动上线后新进程起来时旧进程可能还没退出、还拿着 daemon.lock。lock_wait>0 时要等它释放，
    不能像正常开机启动那样「已有后台在运行」直接退出——否则上线后后台就没了，第二天不交易。"""
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    monkeypatch.setattr(daemon, "file_log", lambda msg: None)
    old = hold_run_lock(tmp_path / "daemon.lock")
    old.__enter__()
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            old.__exit__(None, None, None)

    calls = []

    def runner(stop, log):
        calls.append(1)
        stop.set()

    rc = daemon.main(runner=runner, clock=lambda: WED_1000, stop=FakeStop(),
                     git_rev_parse=_no_git_rev_parse, sync_and_deploy=_no_sync,
                     restart=_boom_restart, lock_wait=30, sleep=fake_sleep)
    assert rc == 0
    assert calls == [1]
    assert len(sleeps) == 3


def test_real_restart_marks_new_process_as_after_deploy(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda args, **kw: seen.setdefault("args", args))
    daemon._real_restart(tmp_path, tmp_path / "python.exe")
    assert seen["args"][-1] == "--after-deploy"


# ---- 看门狗接线：PID 复用 + 线程不因连库失败而死 ----

def test_live_alive_requires_process_alive_and_live_lock_still_held(monkeypatch, tmp_path):
    """live.py 退出后 Windows 很快会把它的 PID 分给别的进程：只有进程还占着 live.lock 才算「live.py 还活着」，才杀。"""
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "data" / "jevq.sqlite"))
    lock_checks = []
    state = {"pid_alive": True, "held": True}
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: state["pid_alive"])
    monkeypatch.setattr(daemon, "is_held", lambda path: lock_checks.append(path) or state["held"])

    assert daemon._live_alive(1234) is True
    assert lock_checks == [tmp_path / "data" / daemon.LIVE_LOCK_NAME]

    state["held"] = False                  # 进程在（PID 被别人用了），但已经没人占 live.lock
    assert daemon._live_alive(1234) is False

    state["pid_alive"], state["held"] = False, True
    assert daemon._live_alive(1234) is False


def test_live_alive_with_real_lock_file(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    assert daemon._live_alive(1234) is False           # 没有锁文件
    with hold_run_lock(tmp_path / daemon.LIVE_LOCK_NAME):
        assert daemon._live_alive(1234) is True
    assert daemon._live_alive(1234) is False           # 锁已释放（live.py 退出了）


def test_watchdog_does_not_kill_a_reused_pid(monkeypatch, tmp_path):
    from jevq import db as jdb
    from jevq.live import store as live_store
    from jevq.live.watchdog import Watchdog
    from live_fakes import FakeNotifier

    monkeypatch.setattr(daemon, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)       # 这个 PID 现在属于别的进程
    c = jdb.connect(":memory:")
    live_store.set_mode(c, "live")
    live_store.set_state(c, "heartbeat", "2026-09-29T10:00:00")
    live_store.set_state(c, "pid", "4321")
    killed, spawned = [], []
    wd = Watchdog(c, FakeNotifier(), spawn=lambda: spawned.append(1), kill=killed.append,
                  alive=daemon._live_alive, clock=lambda: datetime(2026, 9, 29, 10, 10))
    wd.check()
    assert killed == []          # live.lock 没人占：live.py 早就退出了，不杀
    assert spawned == [1]        # 照旧拉起新的 live.py


class _WatchdogStop:
    """threading.Event 替身：wait() 不真的睡，记下每次等待的秒数，等满 trip_after 次之后 is_set() 为真。"""

    def __init__(self, trip_after):
        self.waits = []
        self.trip_after = trip_after

    def is_set(self):
        return len(self.waits) >= self.trip_after

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set()


def _run_watchdog_thread(monkeypatch, stop, connect):
    made, logged = [], []

    class FakeWatchdog:
        def __init__(self, conn, notifier, spawn, kill, alive, clock=None):
            made.append({"conn": conn, "alive": alive, "kill": kill, "checks": 0})
            self.info = made[-1]

        def check(self):
            self.info["checks"] += 1

    monkeypatch.setattr(daemon, "Watchdog", FakeWatchdog)
    monkeypatch.setattr(daemon.db, "connect", connect)
    monkeypatch.setattr(daemon, "file_log", logged.append)
    t = daemon._real_watchdog_start(stop, object(), Path("repo"), Path("python.exe"))
    t.join(timeout=5)
    assert not t.is_alive()
    return made, logged


def test_watchdog_is_wired_with_the_lock_aware_alive_check(monkeypatch):
    made, logged = _run_watchdog_thread(monkeypatch, _WatchdogStop(1), lambda path: "conn")
    assert made[0]["alive"] is daemon._live_alive
    assert made[0]["conn"] == "conn"
    assert made[0]["checks"] == 1 and logged == []


def test_watchdog_thread_survives_db_connect_failure_and_retries_in_60s(monkeypatch):
    """db.connect 失败（库被占、磁盘暂时不可用）不能让看门狗线程当天静默死掉：记日志，60 秒后重试。"""
    attempts = []

    def flaky_connect(path):
        attempts.append(path)
        if len(attempts) <= 2:
            raise OSError("unable to open database file")
        return "conn"

    stop = _WatchdogStop(3)
    made, logged = _run_watchdog_thread(monkeypatch, stop, flaky_connect)

    assert len(attempts) == 3
    assert stop.waits == [60, 60, 60]
    assert len(logged) == 2 and all("unable to open database file" in m for m in logged)
    assert len(made) == 1 and made[0]["checks"] == 1      # 连上以后照常检查，且只建一个 Watchdog


def test_watchdog_thread_keeps_checking_after_a_check_exception(monkeypatch):
    stop = _WatchdogStop(3)
    made, logged = [], []
    calls = []

    class FlakyWatchdog:
        def __init__(self, *a, **kw):
            made.append(self)

        def check(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("检查炸了")

    monkeypatch.setattr(daemon, "Watchdog", FlakyWatchdog)
    monkeypatch.setattr(daemon.db, "connect", lambda path: "conn")
    monkeypatch.setattr(daemon, "file_log", logged.append)
    t = daemon._real_watchdog_start(stop, object(), Path("repo"), Path("python.exe"))
    t.join(timeout=5)
    assert len(calls) == 3 and len(made) == 1
    assert logged == [f"看门狗检查异常：{RuntimeError('检查炸了')!r}"]
