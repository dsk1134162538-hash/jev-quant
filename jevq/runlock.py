import os
from contextlib import contextmanager
from pathlib import Path

try:
    import msvcrt
except ImportError:          # 非 Windows
    msvcrt = None
    import fcntl

HELD_MSG = "已有另一个交易进程在运行（桌面面板或 run.bat）"


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
