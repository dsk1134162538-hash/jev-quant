import time
from datetime import date

import pytest

from jevq.notify import Notifier, notifier_from_env


class FakeResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data if data is not None else {"code": 200}

    def json(self):
        return self._data


def make_post(calls, resp=None, exc=None, sleep=0.0):
    resp = resp or FakeResp()

    def post(url, json, timeout):
        calls.append((url, json, timeout))
        if sleep:
            time.sleep(sleep)
        if exc:
            raise exc
        return resp
    return post


# ---- enabled / disabled ----

def test_no_token_is_disabled_and_never_posts():
    calls = []
    n = Notifier(None, post=make_post(calls))
    assert n.enabled is False
    n.send("t", "c")
    n.once("k", "t", "c")
    n.flush()
    assert calls == []


def test_token_enables_notifier():
    n = Notifier("tok", post=make_post([]))
    assert n.enabled is True


# ---- send: happy path ----

def test_send_posts_correct_json():
    calls = []
    n = Notifier("tok123", post=make_post(calls))
    n.send("标题", "正文")
    n.flush()
    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "https://www.pushplus.plus/send"
    assert payload == {"token": "tok123", "title": "标题", "content": "正文", "template": "txt"}
    assert timeout == 5


# ---- send: failures never raise, only log ----

def test_send_post_exception_is_logged_not_raised():
    logs = []
    n = Notifier("tok", post=make_post([], exc=RuntimeError("network down")), log=logs.append)
    n.send("t", "c")
    n.flush()
    assert any("network down" in m for m in logs)


def test_send_non_200_is_logged():
    logs = []
    n = Notifier("tok", post=make_post([], resp=FakeResp(status_code=500)), log=logs.append)
    n.send("t", "c")
    n.flush()
    assert any("500" in m for m in logs)


def test_send_bad_code_in_body_is_logged():
    logs = []
    n = Notifier("tok", post=make_post([], resp=FakeResp(data={"code": 400, "msg": "token错误"})),
                 log=logs.append)
    n.send("t", "c")
    n.flush()
    assert any("400" in m for m in logs)


# ---- daily cap ----

def test_max_per_day_caps_sends():
    calls = []
    n = Notifier("tok", post=make_post(calls), max_per_day=2)
    for _ in range(5):
        n.send("t", "c")
    n.flush()
    assert len(calls) == 2


def test_max_per_day_resets_next_day():
    calls = []
    day = {"d": date(2026, 9, 24)}
    n = Notifier("tok", post=make_post(calls), max_per_day=1, today=lambda: day["d"])
    n.send("t", "c")
    n.send("t", "c")
    day["d"] = date(2026, 9, 25)
    n.send("t", "c")
    n.flush()
    assert len(calls) == 2


# ---- once() ----

def test_once_same_key_only_sends_once_per_day():
    calls = []
    n = Notifier("tok", post=make_post(calls))
    n.once("halted", "t", "c")
    n.once("halted", "t", "c2")
    n.flush()
    assert len(calls) == 1


def test_once_same_key_can_send_again_next_day():
    calls = []
    day = {"d": date(2026, 9, 24)}
    n = Notifier("tok", post=make_post(calls), today=lambda: day["d"])
    n.once("halted", "t", "c")
    day["d"] = date(2026, 9, 25)
    n.once("halted", "t", "c")
    n.flush()
    assert len(calls) == 2


def test_once_different_keys_both_send():
    calls = []
    n = Notifier("tok", post=make_post(calls))
    n.once("a", "t", "c")
    n.once("b", "t", "c")
    n.flush()
    assert len(calls) == 2


def test_once_not_marked_used_when_dropped_by_daily_cap():
    calls = []
    n = Notifier("tok", post=make_post(calls), max_per_day=1)
    n.once("a", "t", "c1")       # 用掉当天唯一的配额
    n.once("b", "t", "c2")       # 被上限挡住：不该发，也不该把 'b' 记成「已发过」
    n._max_per_day = 5           # 模拟配额恢复（同一天）
    n.once("b", "t", "c2-retry")  # 'b' 没被占用过，配额够了应该真的发出去
    n.flush()
    assert len(calls) == 2


# ---- non-blocking ----

def test_send_is_non_blocking():
    n = Notifier("tok", post=make_post([], sleep=1.0))
    t0 = time.monotonic()
    n.send("t", "c")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2
    n.flush()


# ---- notifier_from_env ----

def test_notifier_from_env_no_token_is_disabled(monkeypatch):
    monkeypatch.setattr("jevq.notify._windows_env_fallback", lambda name: None)
    n = notifier_from_env(env={})
    assert n.enabled is False


def test_notifier_from_env_reads_token(monkeypatch):
    monkeypatch.setattr("jevq.notify._windows_env_fallback", lambda name: None)
    n = notifier_from_env(env={"PUSHPLUS_TOKEN": "abc"}, post=lambda *a, **kw: FakeResp())
    assert n.enabled is True
    assert n.token == "abc"


def test_notifier_from_env_falls_back_to_windows_registry(monkeypatch):
    monkeypatch.setattr("jevq.notify._windows_env_fallback", lambda name: "regtoken")
    n = notifier_from_env(env={}, post=lambda *a, **kw: FakeResp())
    assert n.enabled is True
    assert n.token == "regtoken"


# ---- token 绝不进日志 ----

def test_token_is_redacted_when_response_echoes_it_back():
    logs = []
    real_token = "sup3rsecret-token-value"
    n = Notifier(real_token, post=make_post([], resp=FakeResp(
        data={"code": 400, "msg": "token验证错误", "token": real_token})), log=logs.append)
    n.send("t", "c")
    n.flush()
    text = "\n".join(logs)
    assert real_token not in text
    assert "***" in text


def test_notifier_from_env_is_disabled_in_test_suite_even_with_real_registry_token():
    """全局隔离生效：即便这台机器的 HKCU 注册表里真的有 PUSHPLUS_TOKEN
    （守护进程在用），测试进程里 notifier_from_env() 也必须拿到禁用的 Notifier。"""
    n = notifier_from_env()
    assert n.enabled is False
