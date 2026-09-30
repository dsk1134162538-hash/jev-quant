"""live.py 入口测试：make_broker 按配置选券商适配器；锁被占用时不建 Executor 直接退出；
主循环调用 executor.step()，step 抛异常要自动暂停；页面每个请求自己的连接 + Host 白名单；
15:10–17:00 之间每 5 分钟查一次代码有没有更新，有更新才自我重启。全程注入假对象，不碰真实数据库/网络，
不 import easytrader（用假类替换）。
"""
import sqlite3
from datetime import datetime, time, timedelta

import live
from jevq import config, db
from jevq.live import store as live_store
from jevq.live.executor import Executor
from jevq.runlock import hold_run_lock
from live_fakes import FakeBroker, FakeClock, FakeNotifier


class FakeGatewayBroker:
    def __init__(self, base_url, token=None):
        self.base_url = base_url
        self.token = token


class FakeEasytraderBroker:
    def __init__(self, exe_path):
        self.exe_path = exe_path


class FakeExecutor:
    def __init__(self):
        self.started = False
        self.steps = 0

    def startup(self):
        self.started = True

    def step(self):
        self.steps += 1


# ---- make_broker ----

def test_make_broker_returns_gateway_by_default(monkeypatch):
    monkeypatch.setattr(live, "LIVE_BROKER", "gateway")
    monkeypatch.setattr(live, "GatewayBroker", FakeGatewayBroker)
    monkeypatch.setattr(live, "EasytraderBroker", FakeEasytraderBroker)
    monkeypatch.setattr(live, "_gateway_token", lambda: "tok")
    broker = live.make_broker()
    assert isinstance(broker, FakeGatewayBroker)
    assert broker.token == "tok"


def test_make_broker_returns_easytrader_when_configured(monkeypatch):
    monkeypatch.setattr(live, "LIVE_BROKER", "easytrader")
    monkeypatch.setattr(live, "GatewayBroker", FakeGatewayBroker)
    monkeypatch.setattr(live, "EasytraderBroker", FakeEasytraderBroker)
    broker = live.make_broker()
    assert isinstance(broker, FakeEasytraderBroker)


def test_gateway_token_reads_env_var(monkeypatch):
    monkeypatch.setenv("XIADAN_GATEWAY_TOKEN", "secret")
    assert live._gateway_token() == "secret"


def test_gateway_token_falls_back_to_registry_when_env_missing(monkeypatch):
    monkeypatch.delenv("XIADAN_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(live, "_windows_env_fallback",
                        lambda name: "reg-secret" if name == "XIADAN_GATEWAY_TOKEN" else None)
    assert live._gateway_token() == "reg-secret"


# ---- 锁 ----

def test_main_exits_zero_without_building_executor_when_lock_already_held(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    logged = []

    def boom_executor_factory(*a, **kw):
        raise AssertionError("不该在锁被占用时创建 Executor")

    with hold_run_lock(tmp_path / "live.lock"):
        rc = live.main(executor_factory=boom_executor_factory, log=logged.append)

    assert rc == 0
    assert logged


# ---- 主循环 ----

def test_main_runs_step_each_loop_iteration_and_serves_localhost(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    monkeypatch.setattr(live, "_live_host", lambda: None)
    fake = FakeExecutor()
    served = []

    def fake_serve(hosts, port, handler_cls, log=print):
        served.append((list(hosts), port))
        return []

    rc = live.main(conn=object(), broker=object(), notifier=object(),
                   executor_factory=lambda *a, **kw: fake, web_serve=fake_serve,
                   clock=lambda: datetime(2026, 9, 29, 10, 0), sleep=lambda s: None,
                   max_steps=3, git_rev_parse=lambda repo: "abc")

    assert rc == 0
    assert fake.started is True
    assert fake.steps == 3
    assert served == [(["127.0.0.1"], live.LIVE_HTTP_PORT)]


def test_main_adds_extra_host_from_live_host_env(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    monkeypatch.setattr(live, "_live_host", lambda: "100.64.1.2")
    fake = FakeExecutor()
    served = []

    rc = live.main(conn=object(), broker=object(), notifier=object(),
                   executor_factory=lambda *a, **kw: fake,
                   web_serve=lambda hosts, port, handler_cls, log=print: served.append(list(hosts)) or [],
                   clock=lambda: datetime(2026, 9, 29, 10, 0), sleep=lambda s: None, max_steps=1)

    assert rc == 0
    assert served == [["127.0.0.1", "100.64.1.2"]]


def test_main_logs_step_exception_and_keeps_looping(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "DB_PATH", str(tmp_path / "jevq.sqlite"))

    class BoomExecutor:
        def __init__(self):
            self.calls = 0

        def startup(self):
            pass

        def step(self):
            self.calls += 1
            raise RuntimeError("炸了")

        def _auto_pause(self, reason):
            pass

    fake = BoomExecutor()
    logged = []

    rc = live.main(conn=object(), broker=object(), notifier=object(),
                   executor_factory=lambda *a, **kw: fake, web_serve=lambda *a, **kw: [],
                   clock=lambda: datetime(2026, 9, 29, 10, 0), sleep=lambda s: None,
                   max_steps=2, log=logged.append)

    assert rc == 0
    assert fake.calls == 2
    assert any("执行异常" in m for m in logged)


# ---- 15:10 后自我重启 ----

def test_main_self_restarts_after_15_10_when_commit_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    fake = FakeExecutor()
    restart_calls = []
    commits = iter(["abc", "def"])   # 第一次调用取启动时的提交，第二次是检查时的提交（已不同）

    rc = live.main(conn=object(), broker=object(), notifier=object(),
                   executor_factory=lambda *a, **kw: fake, web_serve=lambda *a, **kw: [],
                   clock=lambda: datetime(2026, 9, 29, 15, 10), sleep=lambda s: None,
                   max_steps=5, git_rev_parse=lambda repo: next(commits),
                   restart=lambda repo, py: restart_calls.append((repo, py)))

    assert rc == 0
    assert len(restart_calls) == 1
    assert fake.steps == 1   # 第一步之后检测到重启，主循环立即退出


def test_main_does_not_restart_when_commit_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    fake = FakeExecutor()
    restart_calls = []

    rc = live.main(conn=object(), broker=object(), notifier=object(),
                   executor_factory=lambda *a, **kw: fake, web_serve=lambda *a, **kw: [],
                   clock=lambda: datetime(2026, 9, 29, 15, 10), sleep=lambda s: None,
                   max_steps=3, git_rev_parse=lambda repo: "abc",
                   restart=lambda repo, py: restart_calls.append((repo, py)))

    assert rc == 0
    assert restart_calls == []
    assert fake.steps == 3


def test_main_does_not_restart_before_15_10(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    fake = FakeExecutor()
    restart_calls = []

    rc = live.main(conn=object(), broker=object(), notifier=object(),
                   executor_factory=lambda *a, **kw: fake, web_serve=lambda *a, **kw: [],
                   clock=lambda: datetime(2026, 9, 29, 15, 9), sleep=lambda s: None,
                   max_steps=3, git_rev_parse=lambda repo: "new-commit-each-time",
                   restart=lambda repo, py: restart_calls.append((repo, py)))

    assert rc == 0
    assert restart_calls == []


# ---- step 抛异常：记日志 + 自动暂停 ----

class BoomStepExecutor(Executor):
    def step(self):
        raise RuntimeError("炸了")


def test_main_step_exception_auto_pauses_and_notifies(monkeypatch, tmp_path):
    """place() 之后写库抛异常这类情况：不能只记日志，要自动暂停并通知，恢复时会走 sending 核对。"""
    monkeypatch.setattr(live, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    c = db.connect(":memory:")
    clock = FakeClock(datetime(2026, 9, 29, 10, 0))
    notifier = FakeNotifier()
    logged = []

    rc = live.main(conn=c, broker=FakeBroker(clock), notifier=notifier,
                   executor_factory=lambda conn, broker, notifier_, **kw: BoomStepExecutor(conn, broker, notifier_, **kw),
                   web_serve=lambda *a, **kw: [], clock=clock.now, sleep=lambda s: None, max_steps=3,
                   log=logged.append)

    assert rc == 0
    p = live_store.paused(c)
    assert p is not None and p["kind"] == "auto" and p["until"] is None
    assert "执行异常" in p["reason"] and "炸了" in p["reason"]
    assert len(notifier.sent) == 1            # 之后每轮再抛异常也不重复推送
    assert "自动暂停" in notifier.sent[0][0] and "执行异常" in notifier.sent[0][1]
    assert sum("执行异常" in m for m in logged) == 3     # 日志照旧每次都记


def test_main_step_exception_when_auto_pause_itself_fails_only_logs_and_keeps_looping(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "DB_PATH", str(tmp_path / "jevq.sqlite"))

    class Boom:
        def __init__(self):
            self.steps = 0
            self.pause_calls = []

        def startup(self):
            pass

        def step(self):
            self.steps += 1
            raise RuntimeError("炸了")

        def _auto_pause(self, reason):
            self.pause_calls.append(reason)
            raise sqlite3.OperationalError("database is locked")

    fake = Boom()
    logged = []
    rc = live.main(conn=object(), broker=object(), notifier=object(),
                   executor_factory=lambda *a, **kw: fake, web_serve=lambda *a, **kw: [],
                   clock=lambda: datetime(2026, 9, 29, 10, 0), sleep=lambda s: None,
                   max_steps=2, log=logged.append)

    assert rc == 0
    assert fake.steps == 2
    assert len(fake.pause_calls) == 2 and "RuntimeError" in fake.pause_calls[0]
    assert any("自动暂停失败" in m and "database is locked" in m for m in logged)


# ---- 页面：每个请求自己的连接 + Host 白名单 ----

def _capture_handler_args(monkeypatch, tmp_path, live_host):
    monkeypatch.setattr(live, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    monkeypatch.setattr(live, "_live_host", lambda: live_host)
    captured = {}

    def fake_make_handler(conn_factory, **kw):
        captured["factory"] = conn_factory
        captured["kw"] = kw
        return object()

    rc = live.main(conn=object(), broker=object(), notifier=object(),
                   executor_factory=lambda *a, **kw: FakeExecutor(), web_serve=lambda *a, **kw: [],
                   make_handler_fn=fake_make_handler,
                   clock=lambda: datetime(2026, 9, 29, 10, 0), sleep=lambda s: None, max_steps=1)
    assert rc == 0
    return captured


def test_main_page_gets_per_request_connections_that_are_closed(monkeypatch, tmp_path):
    captured = _capture_handler_args(monkeypatch, tmp_path, None)
    assert captured["kw"]["close_conn"] is True


def test_page_conn_opens_a_fresh_connection_each_call_with_15s_timeout(monkeypatch, tmp_path):
    path = str(tmp_path / "jevq.sqlite")
    db.connect(path).close()        # 执行器那条连接已经建好表、切成 WAL；页面连接不再跑 SCHEMA
    monkeypatch.setattr(live, "DB_PATH", path)
    captured = _capture_handler_args(monkeypatch, tmp_path, None)
    monkeypatch.setattr(live, "DB_PATH", path)

    c1, c2 = captured["factory"](), captured["factory"]()
    try:
        assert c1 is not c2
        assert c1.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
        assert c1.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert c1.execute("SELECT COUNT(*) FROM live_state").fetchone() == (0,)
    finally:
        c1.close()
        c2.close()


def test_main_page_only_answers_to_loopback_hosts_by_default(monkeypatch, tmp_path):
    captured = _capture_handler_args(monkeypatch, tmp_path, None)
    assert captured["kw"]["allowed_hosts"] == {"127.0.0.1", "localhost"}


def test_main_page_host_whitelist_includes_live_host_env(monkeypatch, tmp_path):
    captured = _capture_handler_args(monkeypatch, tmp_path, "100.64.1.2")
    assert captured["kw"]["allowed_hosts"] == {"127.0.0.1", "localhost", "100.64.1.2"}


# ---- 自我重启：15:10–17:00 每 5 分钟查一次 ----

class TickClock:
    """每次 sleep（主循环每轮一次）把时间往前拨 tick 秒，不真的睡眠。"""

    def __init__(self, start, tick):
        self.t = start
        self.tick = tick

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=self.tick)


def _run_restart_loop(monkeypatch, tmp_path, start, tick, max_steps, rev):
    """rev(n)：第 n 次（从 0 起）git rev-parse 的返回值。返回 (rc, 每次 rev-parse 的时刻, 重启次数)。"""
    monkeypatch.setattr(live, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    clock = TickClock(start, tick)
    calls = []
    restarts = []

    def git_rev_parse(repo):
        calls.append(clock.now())
        return rev(len(calls) - 1)

    rc = live.main(conn=object(), broker=object(), notifier=object(),
                   executor_factory=lambda *a, **kw: FakeExecutor(), web_serve=lambda *a, **kw: [],
                   clock=clock.now, sleep=clock.sleep, max_steps=max_steps, git_rev_parse=git_rev_parse,
                   restart=lambda repo, py: restarts.append(clock.now()))
    return rc, calls[1:], restarts      # calls[0] 是启动时记录运行中的提交


def test_restart_check_repeats_every_five_minutes_between_15_10_and_17_00(monkeypatch, tmp_path):
    assert config.LIVE_RESTART_EVERY == 300
    rc, checks, restarts = _run_restart_loop(monkeypatch, tmp_path, datetime(2026, 9, 29, 15, 5), 100, 20,
                                             lambda n: "abc")
    assert rc == 0 and restarts == []
    d = datetime(2026, 9, 29, 15, 0)
    assert checks == [d.replace(minute=m) for m in (10, 15, 20, 25, 30, 35)]


def test_restart_check_catches_a_commit_that_changes_after_the_first_check(monkeypatch, tmp_path):
    """代码更新晚于 15:10（deploy 要先跑测试，rebase 常常晚）：后面的检查要能发现，不能等到明天。"""
    rc, checks, restarts = _run_restart_loop(monkeypatch, tmp_path, datetime(2026, 9, 29, 15, 5), 100, 40,
                                             lambda n: "abc" if n < 3 else "def")   # 启动、15:10、15:15 都是 abc
    assert rc == 0
    assert checks == [datetime(2026, 9, 29, 15, m) for m in (10, 15, 20)]
    assert restarts == [datetime(2026, 9, 29, 15, 20)]     # 15:20 那次发现变了，立刻重启，主循环退出


def test_restart_check_stops_after_17_00(monkeypatch, tmp_path):
    assert config.LIVE_RESTART_UNTIL == time(17, 0)
    rc, checks, restarts = _run_restart_loop(monkeypatch, tmp_path, datetime(2026, 9, 29, 16, 40), 100, 30,
                                             lambda n: "abc")      # 模拟时间一直跑到 17:30
    assert rc == 0 and restarts == []
    assert len(checks) >= 4 and checks[0] == datetime(2026, 9, 29, 16, 40)
    assert all(t.time() <= config.LIVE_RESTART_UNTIL for t in checks)
    assert checks[-1] >= datetime(2026, 9, 29, 16, 55)


def test_restart_check_starts_again_the_next_day(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "DB_PATH", str(tmp_path / "jevq.sqlite"))
    times = iter([datetime(2026, 9, 29, 15, 10), datetime(2026, 9, 30, 15, 10)])
    calls = []
    rc = live.main(conn=object(), broker=object(), notifier=object(),
                   executor_factory=lambda *a, **kw: FakeExecutor(), web_serve=lambda *a, **kw: [],
                   clock=lambda: next(times), sleep=lambda s: None, max_steps=2,
                   git_rev_parse=lambda repo: calls.append(1) or "abc",
                   restart=lambda repo, py: None)
    assert rc == 0
    assert len(calls) == 3      # 启动 + 两天各一次
