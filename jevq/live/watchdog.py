"""daemon 看门狗：live.py 心跳断了就重启它。规则见
docs/specs/2026-09-29-live-trading-design.md「架构」daemon 一条。
"""
import ctypes
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from jevq import config
from jevq.live import store

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


class Watchdog:
    """check() 由调用方每 60 秒调一次。重启次数记在内存里，按 clock() 的日期分天。"""

    def __init__(self, conn, notifier, spawn, kill, alive, clock=datetime.now):
        self.conn = conn
        self.notifier = notifier
        self.spawn = spawn
        self.kill = kill
        self.alive = alive
        self.clock = clock
        self._day = None
        self._restarts = 0

    def check(self) -> None:
        if store.mode(self.conn) == "off":
            return
        now = self.clock()
        if now.date() != self._day:
            self._day = now.date()
            self._restarts = 0
        raw = store.get_state(self.conn, "heartbeat")
        if raw is None:
            reason = "心跳缺失"
        else:
            elapsed = (now - datetime.fromisoformat(raw)).total_seconds()
            if elapsed < config.LIVE_WATCHDOG_STALE:
                return
            reason = f"心跳 {int(elapsed)} 秒没更新"
        if self._restarts >= config.LIVE_WATCHDOG_MAX_RESTARTS:
            self.notifier.once("live_dead", "💰 实盘 执行进程停了",
                               f"已重启 {config.LIVE_WATCHDOG_MAX_RESTARTS} 次，不再自动重启，请远程检查")
            return
        pid = store.get_state(self.conn, "pid")
        if pid is not None and self.alive(int(pid)):
            self.kill(int(pid))
        self.spawn()
        self._restarts += 1
        self.notifier.send("💰 实盘 执行进程重启", reason)


def pid_alive(pid: int) -> bool:
    """进程是否还活着。不用 os.kill —— Windows 上 os.kill 会直接把进程结束掉。"""
    try:
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def kill_pid(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


def spawn_live(repo: Path, python_exe: Path) -> None:
    """同 daemon._real_restart 的 Popen 写法，启动 live.py。"""
    pythonw = python_exe.parent / "pythonw.exe"
    exe = pythonw if pythonw.exists() else python_exe
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe), str(repo / "live.py")], cwd=repo, close_fds=True, **kwargs)
