"""AI 盘中循环（dj 2026-11-01 同意）：只在北京盘中跑、不叠加、失败退避、每日上限，没有任何券商调用。"""
from datetime import datetime, timedelta

from dashboard import ai_loop


def at(d, h, m, s=0):
    return datetime(2026, 11, d, h, m, s)


def test_sessions_are_auction_morning_and_afternoon_on_weekdays():
    assert ai_loop.in_session(at(2, 9, 26)) and not ai_loop.in_session(at(2, 9, 30))   # 09:26 竞价一轮，09:30 不跑
    assert ai_loop.in_session(at(2, 9, 31)) and ai_loop.in_session(at(2, 11, 24))
    assert not ai_loop.in_session(at(2, 11, 25)) and not ai_loop.in_session(at(2, 12, 0))
    assert ai_loop.in_session(at(2, 13, 1)) and not ai_loop.in_session(at(2, 14, 50))
    assert not ai_loop.in_session(at(1, 10, 0))                       # 周日
    assert ai_loop.next_session_start(at(1, 10, 0)) == at(2, 9, 26)   # 周日 → 周一竞价
    assert ai_loop.next_session_start(at(2, 9, 27)) == at(2, 9, 31)
    assert ai_loop.next_session_start(at(2, 12, 0)) == at(2, 13, 1)
    assert ai_loop.next_session_start(at(6, 15, 0)) == at(9, 9, 26)   # 周五收盘后 → 下周一


def test_run_once_logs_skip_without_counting_a_model_call():
    logs = []
    rec = ai_loop.run_once(cycle=lambda: {"eligible": False, "reason": "非盘中"}, clock=lambda: at(2, 9, 40), log=logs.append)
    assert rec["ok"] and not rec["called"] and rec["skipped"] == "非盘中" and logs == [rec]


def test_run_once_logs_a_real_call_with_duration_and_never_raises():
    logs = []
    rec = ai_loop.run_once(cycle=lambda: {"ok": True, "model": "workbuddy:deepseek-v4.1-flash", "decisionId": "AI-1",
                                          "execution": {"status": "observe"}, "reconciled": True},
                           clock=lambda: at(2, 9, 40), log=logs.append)
    assert rec["called"] and rec["model"].startswith("workbuddy:") and "seconds" in rec

    def boom():
        raise TimeoutError("WorkBuddy 判断超时（90 秒）")
    bad = ai_loop.run_once(cycle=boom, clock=lambda: at(2, 9, 45), log=logs.append)
    assert not bad["ok"] and not bad["called"] and "TimeoutError" in bad["error"]


class Clock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


def _run_loop(start, until, cycle, **kw):
    clock, logs = Clock(start), []
    ai_loop.loop(cycle=cycle, clock=clock.now, sleep=clock.sleep, log=logs.append, stop=lambda: clock.t >= until, **kw)
    return logs


def test_loop_runs_every_five_minutes_inside_the_session_only():
    calls = []
    logs = _run_loop(at(2, 9, 20), at(2, 10, 0), lambda: calls.append(1) or {"ok": True, "model": "m"})
    # 09:26 竞价一轮；开盘后 09:31 起每 5 分钟，10:00 是结束时刻
    assert [l["ts"][11:16] for l in logs if l.get("called")] == ["09:26", "09:31", "09:36", "09:41", "09:46", "09:51", "09:56"]


def test_loop_stops_calling_at_daily_cap_and_backs_off_after_failures():
    n = {"c": 0}

    def ok():
        n["c"] += 1
        return {"ok": True, "model": "m"}
    logs = _run_loop(at(2, 9, 35), at(2, 11, 0), ok, max_calls=3)
    assert n["c"] == 3 and any("到上限" in str(l.get("skipped")) for l in logs)

    def boom():
        raise RuntimeError("x")
    logs = _run_loop(at(3, 9, 35), at(3, 10, 30), boom)
    assert any("连续失败" in str(l.get("skipped")) for l in logs)
    assert sum(1 for l in logs if l.get("error")) <= 5               # 退避后不会每 5 分钟继续硬撞


def test_module_has_no_broker_or_live_switch():
    import pathlib
    text = pathlib.Path(ai_loop.__file__).read_text(encoding="utf-8")
    for bad in ("GatewayBroker", "make_broker", "set_mode", "live_intents", "/orders", "XIADAN"):
        assert bad not in text


def test_rate_limit_waits_until_reset_time():
    from dashboard.workbuddy_bridge import WorkBuddyRateLimited
    calls = []

    def limited():
        calls.append(1)
        if len(calls) == 1:
            raise WorkBuddyRateLimited("429", datetime(2026, 11, 2, 10, 30))
        return {"ok": True, "model": "m"}
    logs = _run_loop(at(2, 9, 35), at(2, 10, 40), limited)
    called = [l["ts"][11:16] for l in logs if l.get("called")]
    assert called and called[0] >= "10:30" and any("额度限流" in str(l.get("skipped")) for l in logs)


def test_pythonw_without_console_gets_stdio_files(monkeypatch, tmp_path):
    # pythonw 下 sys.stdout/stderr 是 None：tqdm 进度条写不出去，快照第一轮判「行情不完整」、第二轮卡死（2026-11-02 实盘）
    monkeypatch.setattr(ai_loop.sys, "stdout", None)
    monkeypatch.setattr(ai_loop.sys, "stderr", None)
    ai_loop._ensure_stdio(tmp_path)
    print("hello")
    ai_loop.sys.stderr.write("oops\n")
    ai_loop.sys.stdout.flush(); ai_loop.sys.stderr.flush()
    assert "hello" in (tmp_path / "ai_loop.out").read_text(encoding="utf-8")
    assert "oops" in (tmp_path / "ai_loop.err").read_text(encoding="utf-8")


def test_ensure_stdio_keeps_a_real_console(monkeypatch, tmp_path):
    out = ai_loop.sys.stdout
    ai_loop._ensure_stdio(tmp_path)
    assert ai_loop.sys.stdout is out
    assert not (tmp_path / "ai_loop.out").exists()
