"""live.py 入口测试：make_broker 按配置选券商适配器；锁被占用时不建 Executor 直接退出；
主循环调用 executor.step()；15:10 后代码有更新才自我重启。全程注入假对象，不碰真实数据库/网络，
不 import easytrader（用假类替换）。
"""
from datetime import datetime

import live
from jevq.runlock import hold_run_lock


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
