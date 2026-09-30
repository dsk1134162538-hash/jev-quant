from datetime import datetime, timedelta

from jevq import config, db
from jevq.live import store as live_store
from jevq.live.watchdog import Watchdog
from live_fakes import FakeClock, FakeNotifier


def conn():
    return db.connect(":memory:")


class FakeProcess:
    """记录 spawn/kill/alive 调用，模拟一个可控的「进程是否存活」状态。"""

    def __init__(self):
        self.spawned = 0
        self.killed: list[int] = []
        self.alive_pids: set[int] = set()

    def spawn(self) -> None:
        self.spawned += 1

    def kill(self, pid: int) -> None:
        self.killed.append(pid)

    def alive(self, pid: int) -> bool:
        return pid in self.alive_pids


def make_watchdog(c, notifier, proc, clock):
    return Watchdog(c, notifier, proc.spawn, proc.kill, proc.alive, clock=clock.now)


def test_off_mode_does_nothing():
    c = conn()
    notifier = FakeNotifier()
    proc = FakeProcess()
    wd = make_watchdog(c, notifier, proc, FakeClock(datetime(2026, 9, 29, 10, 0)))
    wd.check()
    assert proc.spawned == 0
    assert proc.killed == []
    assert notifier.sent == []


def test_fresh_heartbeat_does_nothing():
    c = conn()
    live_store.set_mode(c, "live")
    clock = FakeClock(datetime(2026, 9, 29, 10, 0))
    live_store.set_state(c, "heartbeat", (clock.now() - timedelta(seconds=10)).isoformat())
    notifier = FakeNotifier()
    proc = FakeProcess()
    wd = make_watchdog(c, notifier, proc, clock)
    wd.check()
    assert proc.spawned == 0
    assert notifier.sent == []


def test_stale_heartbeat_and_alive_pid_kills_then_spawns_and_notifies():
    c = conn()
    live_store.set_mode(c, "live")
    clock = FakeClock(datetime(2026, 9, 29, 10, 0))
    live_store.set_state(c, "heartbeat", (clock.now() - timedelta(seconds=200)).isoformat())
    live_store.set_state(c, "pid", "4321")
    notifier = FakeNotifier()
    proc = FakeProcess()
    proc.alive_pids.add(4321)
    wd = make_watchdog(c, notifier, proc, clock)
    wd.check()
    assert proc.killed == [4321]
    assert proc.spawned == 1
    assert notifier.sent == [("💰 实盘 执行进程重启", "心跳 200 秒没更新")]


def test_missing_heartbeat_counts_as_stale_and_spawns_without_kill():
    c = conn()
    live_store.set_mode(c, "dryrun")
    clock = FakeClock(datetime(2026, 9, 29, 10, 0))
    notifier = FakeNotifier()
    proc = FakeProcess()
    wd = make_watchdog(c, notifier, proc, clock)
    wd.check()
    assert proc.killed == []
    assert proc.spawned == 1
    assert notifier.sent == [("💰 实盘 执行进程重启", "心跳缺失")]


def test_stale_heartbeat_with_dead_pid_spawns_without_kill():
    c = conn()
    live_store.set_mode(c, "live")
    clock = FakeClock(datetime(2026, 9, 29, 10, 0))
    live_store.set_state(c, "heartbeat", (clock.now() - timedelta(seconds=200)).isoformat())
    live_store.set_state(c, "pid", "999")
    notifier = FakeNotifier()
    proc = FakeProcess()  # alive_pids 为空，999 不算活着
    wd = make_watchdog(c, notifier, proc, clock)
    wd.check()
    assert proc.killed == []
    assert proc.spawned == 1


def test_stops_spawning_after_max_restarts_and_notifies_once_only():
    c = conn()
    live_store.set_mode(c, "live")
    clock = FakeClock(datetime(2026, 9, 29, 10, 0))
    live_store.set_state(c, "heartbeat", (clock.now() - timedelta(seconds=200)).isoformat())
    live_store.set_state(c, "pid", "4321")
    notifier = FakeNotifier()
    proc = FakeProcess()
    proc.alive_pids.add(4321)
    wd = make_watchdog(c, notifier, proc, clock)
    for _ in range(config.LIVE_WATCHDOG_MAX_RESTARTS):
        wd.check()
    assert proc.spawned == config.LIVE_WATCHDOG_MAX_RESTARTS

    wd.check()
    wd.check()
    assert proc.spawned == config.LIVE_WATCHDOG_MAX_RESTARTS  # 不再涨

    dead_msgs = [m for m in notifier.sent if m[0] == "💰 实盘 执行进程停了"]
    assert len(dead_msgs) == 1  # once：只报一次
    assert dead_msgs[0][1] == f"已重启 {config.LIVE_WATCHDOG_MAX_RESTARTS} 次，不再自动重启，请远程检查"


def test_restart_counter_resets_on_new_calendar_day():
    c = conn()
    live_store.set_mode(c, "live")
    live_store.set_state(c, "pid", "4321")
    clock = FakeClock(datetime(2026, 9, 29, 10, 0))
    notifier = FakeNotifier()
    proc = FakeProcess()
    proc.alive_pids.add(4321)
    wd = make_watchdog(c, notifier, proc, clock)

    for _ in range(config.LIVE_WATCHDOG_MAX_RESTARTS):
        live_store.set_state(c, "heartbeat", (clock.now() - timedelta(seconds=200)).isoformat())
        wd.check()
    assert proc.spawned == config.LIVE_WATCHDOG_MAX_RESTARTS

    live_store.set_state(c, "heartbeat", (clock.now() - timedelta(seconds=200)).isoformat())
    wd.check()
    assert proc.spawned == config.LIVE_WATCHDOG_MAX_RESTARTS  # 当天用完了

    clock.t = datetime(2026, 9, 30, 10, 0)  # 跨天
    live_store.set_state(c, "heartbeat", (clock.now() - timedelta(seconds=200)).isoformat())
    wd.check()
    assert proc.spawned == config.LIVE_WATCHDOG_MAX_RESTARTS + 1  # 次数清零后又能重启
