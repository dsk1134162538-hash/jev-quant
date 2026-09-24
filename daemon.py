import os
import subprocess
import sys
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent

import run  # noqa: E402
from jevq.config import DB_PATH, SYNC_ENABLED, DEPLOY_ENABLED  # noqa: E402
from jevq.notify import notifier_from_env  # noqa: E402
from jevq.runlock import DAEMON_LOCK_NAME, HELD_MSG, hold_run_lock  # noqa: E402
from jevq.sync import export as sync_export  # noqa: E402
from jevq.sync.deploy import sync_and_deploy as real_sync_and_deploy  # noqa: E402

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


def _real_git_rev_parse(repo: Path) -> str | None:
    """记录守护进程启动时运行代码的提交，供收盘后判断是否需要上线。读不到就是 None——
    调用方视为「总是需要检查」，不是致命错误。"""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _real_restart(repo: Path, python_exe: Path) -> None:
    """自我重启：起一个独立的新后台进程跑新代码，当前进程随后正常退出、释放 daemon.lock。
    新进程在拿到锁之前会自己重试几秒（main() 里 hold_run_lock 抛 RuntimeError 才会立刻放弃，
    这里给它留出重试空间由 daemon 自身的运行方式保证：新进程照常调用一次 main()）。"""
    pythonw = python_exe.parent / "pythonw.exe"
    exe = pythonw if pythonw.exists() else python_exe
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe), str(repo / "daemon.py")], cwd=repo, close_fds=True, **kwargs)


def _default_after_close(conn, today) -> None:
    if not SYNC_ENABLED:
        return
    try:
        written = sync_export.export_day(conn, today)
        file_log(f"每日导出 {len(written)} 个文件")
    except Exception as e:
        file_log(f"每日导出失败：{e!r}")


def _default_runner(stop, log):
    return run.run_day(stop=stop, log=log, after_close=_default_after_close)


def _after_run(report_path, today: date, repo: Path, python_exe: Path, notifier,
              running_commit: str | None, sync_and_deploy, restart, log) -> tuple[str | None, bool]:
    """报告写完（report_path 不是 None，说明今天是交易日）之后触发同步/上线判断。

    返回 (新的 running_commit, 是否应该退出主循环去重启)。任何异常都只记日志，绝不让
    同步/上线的问题打断明天的交易；上线成功会触发自我重启，随后主循环应该退出，让当前
    进程正常释放 daemon.lock。
    """
    if report_path is None or not SYNC_ENABLED:
        return running_commit, False
    try:
        result = sync_and_deploy(repo, today, log, notifier, python_exe, running_commit)
    except Exception as e:
        log(f"同步/上线异常：{e!r}")
        return running_commit, False
    if not (result.get("deploy") and DEPLOY_ENABLED):
        return running_commit, False
    commit = result.get("commit")
    log(f"已自动上线新代码 {commit}")
    try:
        notifier.send("模拟盘更新", f"已自动上线新代码 {commit[:7] if commit else ''}")
    except Exception as e:
        log(f"上线提醒推送失败：{e!r}")
    try:
        restart(repo, python_exe)
    except Exception as e:
        log(f"自我重启失败：{e!r}")
        return running_commit, False
    return commit, True


def main(runner=_default_runner, clock=datetime.now, stop=None, *,
        repo: Path = ROOT, python_exe: Path | None = None, notifier=None,
        git_rev_parse=None, sync_and_deploy=None, restart=None) -> int:
    stop = stop or threading.Event()
    python_exe = python_exe or Path(sys.executable)
    notifier = notifier if notifier is not None else notifier_from_env()
    git_rev_parse = git_rev_parse or _real_git_rev_parse
    sync_and_deploy = sync_and_deploy or real_sync_and_deploy
    restart = restart or _real_restart
    running_commit = git_rev_parse(repo)
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
                report_path = _run_with_retry(runner, stop, clock)
                today = clock().date()
                last_run = today
                running_commit, should_exit = _after_run(
                    report_path, today, repo, python_exe, notifier, running_commit,
                    sync_and_deploy, restart, file_log)
                if should_exit:
                    return 0
            return 0
    except RuntimeError:
        file_log("已有后台程序在运行，退出")
        return 0


def _run_with_retry(runner, stop: threading.Event, clock):
    """跑一轮 runner；若因交易锁被占用（面板或 run.bat 抢了锁，runner 抛 HELD_MSG）失败，
    记日志后每 60 秒重试一次，直到成功、遇到其他异常，或到了当天 REPORT_AT（15:05）为止。
    08:50 后台被面板短暂抢锁不该导致后台一整天放弃：之前的做法是记日志后直接把 last_run
    设为今天，等于当天彻底不再补跑。

    成功时把 runner 的返回值（run_day 返回报告路径 Path，非交易日/未生成报告返回 None）
    透传出去，供调用方判断今天是不是交易日、要不要触发同步/上线。
    """
    while True:
        try:
            return runner(stop=stop, log=file_log)
        except RuntimeError as e:
            if str(e) != HELD_MSG:
                file_log(f"运行出错：{e!r}")
                return None
            file_log(f"交易锁被占用，60 秒后重试：{e!r}")
        except Exception as e:
            file_log(f"运行出错：{e!r}")
            return None
        if clock().time() >= run.REPORT_AT:
            file_log("已过今日收盘时间，放弃重试")
            return None
        if stop.wait(60):
            return None


if __name__ == "__main__":
    bootstrap()
    sys.exit(main())
