import os
import sys
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent

import run  # noqa: E402
from jevq.config import DB_PATH  # noqa: E402
from jevq.runlock import DAEMON_LOCK_NAME, HELD_MSG, hold_run_lock  # noqa: E402

START = time(8, 50)
LOG_DIR = Path("data")


def bootstrap() -> None:
    """仅在作为脚本运行时执行的有副作用的初始化：切换 cwd 到仓库根目录，
    创建 data/ 目录，并把 pythonw 下为 None 的 stdout/stderr 重定向到日志文件。"""
    os.chdir(ROOT)
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    if sys.stdout is None:
        sys.stdout = open(ROOT / "data" / "daemon.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(ROOT / "data" / "daemon.log", "a", encoding="utf-8", buffering=1)


def plan_next_run(now: datetime, last_run: date | None) -> datetime:
    """返回下一次应调用 run_day 的时刻。

    - 今天是周一到周五且 last_run != 今天：now >= 08:50 → 立即补跑（返回 now；run_day 自己
      处理盘中晚启动/收盘后补报告/非交易日）；now < 08:50 → 今天 08:50。
    - 否则 → 下一个周一到周五的 08:50。
    """
    today = now.date()
    if now.weekday() < 5 and last_run != today:
        if now.time() >= START:
            return now
        return datetime.combine(today, START)
    d = today + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime.combine(d, START)


def file_log(msg: str) -> None:
    path = LOG_DIR / f"run-{date.today():%Y%m%d}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%H:%M:%S} {msg}\n")


def main(runner=run.run_day, clock=datetime.now, stop=None) -> int:
    stop = stop or threading.Event()
    lock_path = Path(DB_PATH).parent / DAEMON_LOCK_NAME
    try:
        with hold_run_lock(lock_path):
            last_run = None
            while not stop.is_set():
                target = plan_next_run(clock(), last_run)
                while clock() < target and not stop.is_set():
                    remaining = (target - clock()).total_seconds()
                    stop.wait(min(60, max(0, remaining)))
                if stop.is_set():
                    break
                _run_with_retry(runner, stop, clock)
                last_run = clock().date()
            return 0
    except RuntimeError:
        file_log("已有后台程序在运行，退出")
        return 0


def _run_with_retry(runner, stop: threading.Event, clock) -> None:
    """跑一轮 runner；若因交易锁被占用（面板或 run.bat 抢了锁，runner 抛 HELD_MSG）失败，
    记日志后每 60 秒重试一次，直到成功、遇到其他异常，或到了当天 REPORT_AT（15:05）为止。
    08:50 后台被面板短暂抢锁不该导致后台一整天放弃：之前的做法是记日志后直接把 last_run
    设为今天，等于当天彻底不再补跑。"""
    while True:
        try:
            runner(stop=stop, log=file_log)
            return
        except RuntimeError as e:
            if str(e) != HELD_MSG:
                file_log(f"运行出错：{e!r}")
                return
            file_log(f"交易锁被占用，60 秒后重试：{e!r}")
        except Exception as e:
            file_log(f"运行出错：{e!r}")
            return
        if clock().time() >= run.REPORT_AT:
            file_log("已过今日收盘时间，放弃重试")
            return
        if stop.wait(60):
            return


if __name__ == "__main__":
    bootstrap()
    sys.exit(main())
