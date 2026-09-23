import os
from contextlib import contextmanager
from pathlib import Path

try:
    import msvcrt
except ImportError:          # 非 Windows
    msvcrt = None
    import fcntl

HELD_MSG = "已有另一个交易进程在运行（桌面面板或 run.bat）"
DAEMON_LOCK_NAME = "daemon.lock"


def _lock(f):
    f.seek(0)
    if msvcrt:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(f):
    f.seek(0)
    if msvcrt:
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def is_held(path) -> bool:
    """锁文件当前是否被其他进程（或本进程的另一个句柄）占用。不存在的文件视为未占用。"""
    path = Path(path)
    if not path.exists():
        return False
    f = open(path, "a+b")
    try:
        try:
            _lock(f)
        except OSError:
            return True
        _unlock(f)
        return False
    finally:
        f.close()


@contextmanager
def hold_run_lock(path):
    """整个交易日独占一个锁文件；已被占用则抛 RuntimeError。进程退出时系统自动释放。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    try:
        _lock(f)
    except OSError:
        f.close()
        raise RuntimeError(HELD_MSG) from None
    try:
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()).encode())
        f.flush()
        yield
    finally:
        try:
            _unlock(f)
        finally:
            f.close()
