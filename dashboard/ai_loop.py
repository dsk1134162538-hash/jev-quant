"""AI 盘中循环（dj 2026-11-01 同意）：北京时间盘中每 5 分钟跑一次「快照 → DeepSeek（WorkBuddy）→ 校验 → 隔离模拟」。

- 只做 dashboard.workbuddy_bridge.run_cycle，没有任何券商调用；真实委托仍未启用。
- 只在北京时间 09:35–11:25、13:05–14:50 的工作日运行；非交易日快照自己返回 eligible=false，不调模型、不扣积分。
- 同一时间只允许一个循环（锁文件）；上一次没跑完不会叠加；连续失败后退避；每日模型调用有上限（AI_LOOP_MAX_CALLS）。
- 每次都在 dashboard/runtime/ai_loop.log 记一行 JSON：时间、耗时、结果或错误，用来量单次耗时和积分消耗。
- 不会自己启动：`python -m dashboard.ai_loop` 手动起；`--once` 只跑一轮（计划任务用）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import jevq  # noqa: F401  —— 先 import：把进程时区固定成北京时间
from jevq.runlock import hold_run_lock

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "dashboard" / "runtime"
LOG_PATH = RUNTIME / "ai_loop.log"
LOCK_PATH = RUNTIME / "ai_loop.lock"

INTERVAL_SECONDS = 300
FAST_INTERVAL_SECONDS = 120          # 扫板窗口（到 10:30）每 2 分钟一轮（dj 2026-11-02：WorkBuddy 额度用不完）
FAST_UNTIL = dtime(10, 30)
DAILY_JOB_AFTER = dtime(15, 35)      # 收盘后记分、赛马排行、复盘写经验条
DAILY_STATE = RUNTIME / "ai_daily_job.json"
# 09:26 竞价一轮（开盘前定开不开闸）；09:31 起每 5 分钟（之前 09:35 起，开盘头几分钟的板打不到）
SESSIONS = ((dtime(9, 26), dtime(9, 29)), (dtime(9, 31), dtime(11, 25)), (dtime(13, 1), dtime(14, 50)))
MAX_CALLS_PER_DAY = int(os.environ.get("AI_LOOP_MAX_CALLS", "150"))  # 2 分钟一轮的扫板窗口约 32 次 + 5 分钟一轮约 40 次
FAIL_BACKOFF_AFTER = 3
FAIL_BACKOFF_SECONDS = 600


def in_session(now: datetime) -> bool:
    return now.weekday() < 5 and any(a <= now.time() < b for a, b in SESSIONS)


def interval_for(now: datetime) -> int:
    return FAST_INTERVAL_SECONDS if now.time() < FAST_UNTIL else INTERVAL_SECONDS


def daily_job_due(now: datetime, state: Path = DAILY_STATE) -> bool:
    if now.weekday() >= 5 or now.time() < DAILY_JOB_AFTER:
        return False
    try:
        return json.loads(state.read_text(encoding="utf-8")).get("date") != now.date().isoformat()
    except (OSError, ValueError):
        return True


def run_daily_job(now: datetime, job, state: Path = DAILY_STATE) -> dict:
    """收盘后一次：任何异常只记日志，不让循环挂掉；跑过就记日期，当天不重跑。"""
    record = {"ts": now.isoformat(timespec="seconds"), "dailyJob": True}
    try:
        result = job()
        record.update(ok=True, newDays=result.get("newDays"), lessons=result.get("lessons"),
                      lessonError=result.get("lessonError"))
    except Exception as e:                                   # noqa: BLE001
        record.update(ok=False, error=f"{type(e).__name__}: {str(e)[:300]}")
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"date": now.date().isoformat(), **record}, ensure_ascii=False), encoding="utf-8")
    return record


def next_session_start(now: datetime) -> datetime:
    """下一个运行窗口的开始时间（工作日；不认节假日——节假日快照自己返回 eligible=false）。"""
    day = now.date()
    for _ in range(8):
        if day.weekday() < 5:
            for a, _b in SESSIONS:
                start = datetime.combine(day, a)
                if start > now:
                    return start
        day += timedelta(days=1)
    raise RuntimeError("找不到下一个运行窗口")


def _log(record: dict, path: Path = LOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _ensure_node() -> None:
    """计划任务/后台进程的 PATH 可能没有 node：解析一次绝对路径给桥接用。"""
    if not os.environ.get("WORKBUDDY_NODE"):
        found = shutil.which("node")
        if found:
            os.environ["WORKBUDDY_NODE"] = found


def _ensure_stdio(folder: Path = RUNTIME) -> None:
    """pythonw 下 sys.stdout/stderr 是 None：行情抓取里的 tqdm 进度条写不出去，
    第一轮快照判成「行情不完整」、第二轮直接卡死（2026-11-02 实盘）。没控制台就接到文件上。"""
    folder.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None:
        sys.stdout = open(folder / "ai_loop.out", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(folder / "ai_loop.err", "a", encoding="utf-8", buffering=1)


def run_once(cycle=None, clock=datetime.now, log=_log, calls_today: int = 0) -> dict:
    """跑一轮并记日志；返回 {"ok": bool, "called": bool（是否真调了模型）, ...}。任何异常都不外抛。"""
    if cycle is None:
        _ensure_node()
        from dashboard.workbuddy_bridge import run_cycle
        cycle = run_cycle
    started = clock()
    t0 = time.monotonic()
    record = {"ts": started.isoformat(timespec="seconds")}
    try:
        result = cycle()
    except Exception as e:                                   # noqa: BLE001
        record.update(ok=False, called=False, seconds=round(time.monotonic() - t0, 1), error=f"{type(e).__name__}: {str(e)[:300]}")
        reset = getattr(e, "reset_at", None)
        if reset is not None:
            record["retryAt"] = reset.isoformat(timespec="seconds")
        log(record)
        return record
    if isinstance(result, dict) and result.get("eligible") is False:
        record.update(ok=True, called=False, seconds=round(time.monotonic() - t0, 1), skipped=str(result.get("reason", ""))[:120])
    else:
        record.update(ok=True, called=True, seconds=round(time.monotonic() - t0, 1),
                      model=result.get("model"), decisionId=result.get("decisionId"),
                      execution=result.get("execution"), reconciled=result.get("reconciled"),
                      candidateId=result.get("candidateId"))
    log(record)
    return record


def loop(cycle=None, clock=datetime.now, sleep=time.sleep, log=_log, max_calls: int = MAX_CALLS_PER_DAY,
         stop=lambda: False, daily=None) -> None:
    """daily：收盘后任务（main 里传真的 ai_score.daily_job；测试不传就不跑）。"""
    calls, fails, today = 0, 0, None
    while not stop():
        now = clock()
        if today != now.date():
            today, calls, fails = now.date(), 0, 0
        if not in_session(now):
            if daily is not None and daily_job_due(now):
                log(run_daily_job(now, daily))
                continue
            wait = (next_session_start(now) - now).total_seconds()
            sleep(max(1.0, min(wait, 600.0)))
            continue
        if calls >= max_calls:
            log({"ts": now.isoformat(timespec="seconds"), "ok": True, "called": False, "skipped": f"今日已调 {calls} 次，到上限"})
            sleep(max(1.0, min((next_session_start(now) - now).total_seconds(), 1800.0)))
            continue
        rec = run_once(cycle, clock, log)
        calls += 1 if rec.get("called") else 0
        fails = 0 if rec["ok"] else fails + 1
        if rec.get("retryAt"):                               # WorkBuddy 额度限流：等到它说的恢复时间，不硬撞
            wait_until = datetime.fromisoformat(rec["retryAt"])
            log({"ts": clock().isoformat(timespec="seconds"), "ok": False, "called": False,
                 "skipped": f"WorkBuddy 额度限流，{wait_until:%H:%M} 恢复"})
            sleep(max(1.0, min((wait_until - clock()).total_seconds() + 5, 6 * 3600)))
            continue
        # 下一轮对齐到 5 分钟间隔：从这一轮开始算，不是从结束算
        elapsed = (clock() - now).total_seconds()
        wait = max(1.0, interval_for(now) - elapsed)
        if fails >= FAIL_BACKOFF_AFTER:
            wait += FAIL_BACKOFF_SECONDS
            log({"ts": clock().isoformat(timespec="seconds"), "ok": False, "called": False,
                 "skipped": f"连续失败 {fails} 次，多等 {FAIL_BACKOFF_SECONDS} 秒"})
        sleep(wait)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="只跑一轮（不看时间窗口）后退出")
    args = parser.parse_args(argv)
    _ensure_stdio()
    try:
        with hold_run_lock(LOCK_PATH):
            if args.once:
                rec = run_once()
                print(json.dumps(rec, ensure_ascii=False))
                return 0 if rec["ok"] else 1
            from dashboard.ai_score import daily_job
            from jevq.notify import notifier_from_env
            loop(daily=lambda: daily_job(notifier=notifier_from_env()))
    except RuntimeError as e:                                # 锁被占用
        print(str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
