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
    # 09:26 竞价窗口；开盘后 09:31 起，扫板窗口（到 10:30）每 2 分钟一轮，10:00 是结束时刻
    expected = ["09:26", "09:28"] + [f"09:{m:02d}" for m in range(31, 60, 2)]
    assert [l["ts"][11:16] for l in logs if l.get("called")] == expected


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
    assert sum(1 for l in logs if l.get("error")) <= 8               # 退避后不会每 2 分钟继续硬撞（不退避是 28 次）


def test_repeated_failures_alert_once_per_day():
    # 2026-11-02 13:17 起提示词超长、整个下午失败，没人知道：连续失败到退避线就推一次提醒，当天不重复
    alerts = []

    def boom():
        raise RuntimeError("prompt is too long")
    _run_loop(at(3, 13, 1), at(3, 14, 50), boom, alert=alerts.append)
    assert len(alerts) == 1 and "连续失败 3 次" in alerts[0] and "prompt is too long" in alerts[0]


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


def test_scan_window_runs_every_two_minutes_then_five():
    assert ai_loop.interval_for(datetime(2026, 11, 3, 9, 40)) == 120
    assert ai_loop.interval_for(datetime(2026, 11, 3, 10, 30)) == 300
    assert ai_loop.interval_for(datetime(2026, 11, 3, 13, 5)) == 300


def test_daily_job_runs_once_after_close_on_weekdays(tmp_path):
    state = tmp_path / "daily.json"
    assert not ai_loop.daily_job_due(datetime(2026, 11, 3, 15, 0), state)
    assert not ai_loop.daily_job_due(datetime(2026, 11, 8, 16, 0), state)          # 周日
    now = datetime(2026, 11, 3, 15, 40)
    assert ai_loop.daily_job_due(now, state)
    rec = ai_loop.run_daily_job(now, lambda: {"newDays": ["2026-11-02"], "lessons": 5}, state)
    assert rec["ok"] and rec["lessons"] == 5
    assert not ai_loop.daily_job_due(datetime(2026, 11, 3, 18, 0), state)
    assert ai_loop.daily_job_due(datetime(2026, 11, 4, 15, 40), state)


def test_daily_job_failure_is_logged_not_raised(tmp_path):
    def boom():
        raise RuntimeError("x")
    rec = ai_loop.run_daily_job(datetime(2026, 11, 3, 15, 40), boom, tmp_path / "d.json")
    assert rec["ok"] is False and "RuntimeError" in rec["error"]


def test_flex_hook_runs_every_trader_and_one_traders_failure_does_not_skip_the_other(monkeypatch, tmp_path):
    # 二号交易员（dj 2026-11-05）：两位各自按自己的时点记录跑，一位 DeepSeek 失败不影响另一位
    from dashboard import flex_trader
    ran = []

    def fake_run_cycle(now, slot, *, trader, **kw):
        ran.append(trader.sid)
        if trader.sid == "aiflex":
            raise TimeoutError("WorkBuddy 超时")
        return {"slot": slot, "orders": 0, "stage": "分歧"}
    monkeypatch.setattr(flex_trader, "run_cycle", fake_run_cycle)
    monkeypatch.setattr(flex_trader, "due_slot", lambda now, state_path: "10:00")
    monkeypatch.setattr("jevq.notify.notifier_from_env", lambda: object())
    out = ai_loop._flex(at(2, 10, 1))
    assert ran == ["aiflex", "aiflex2"]
    assert "TimeoutError" in out["aiflex"]["error"] and out["aiflex2"]["slot"] == "10:00"
    monkeypatch.setattr(flex_trader, "due_slot", lambda now, state_path: None)
    assert ai_loop._flex(at(2, 10, 1)) is None


def test_flex_hook_runs_the_manager_first_so_traders_see_todays_directive(monkeypatch):
    # 交易台经理（dj 2026-11-09）：经理到点先跑，交易员同一轮读到新指令；经理失败不挡交易员
    from dashboard import flex_trader, manager
    ran = []

    def fake_manager(now, slot, **kw):
        ran.append("manager")
        raise TimeoutError("经理超时")
    monkeypatch.setattr(manager, "run_manager", fake_manager)
    monkeypatch.setattr(manager, "due_slot", lambda now, state_path=None: "09:26")
    monkeypatch.setattr(flex_trader, "run_cycle", lambda now, slot, *, trader, **kw: ran.append(trader.sid) or {"slot": slot})
    monkeypatch.setattr(flex_trader, "due_slot", lambda now, state_path: "09:26")
    monkeypatch.setattr("jevq.notify.notifier_from_env", lambda: object())
    out = ai_loop._flex(at(2, 9, 27))
    assert ran == ["manager", "aiflex", "aiflex2"]
    assert "TimeoutError" in out["manager"]["error"] and out["aiflex"]["slot"] == "09:26"


def test_flex_runs_at_key_times_and_its_failures_never_count_as_main_loop_failures():
    # AI 灵活交易（dj 2026-11-03）：每次循环都问一下「到点了吗」；它自己的失败只记日志，不触发主循环的退避和告警
    seen = []

    def flex(now):
        seen.append(now)
        if len(seen) == 2:
            raise RuntimeError("WorkBuddy 超时")
        return {"slot": "10:00", "orders": 1} if len(seen) == 1 else None
    alerts = []
    logs = _run_loop(at(2, 9, 35), at(2, 9, 45), lambda: {"ok": True, "model": "m"}, flex=flex, alert=alerts.append)
    assert len(seen) >= 3 and alerts == []
    assert any(l.get("flex") == {"slot": "10:00", "orders": 1} for l in logs)
    assert any("RuntimeError" in str(l.get("flexError")) and l["ok"] is False for l in logs)
