"""进程时区固定为北京时间（中国没有夏令时，固定偏移即可）。

A 股的一切调度（08:50 启动、09:15–15:00 盘中、14:55 清仓…）都按北京时间的本地钟点写。dj 2026-11-01 说系统时区
不能改（人在欧洲，Windows 是 W. Europe +01:00），而运行中的 live.py 心跳还是北京时间——进程一重启（重启电脑、
Windows 更新、15:10 自动上线）整套调度会偏 7 小时。所以进程自己固定：设 TZ=CST-8，并让 C 运行库重读。
`import jevq` 时自动执行（jevq/__init__.py），daemon、live、run、面板服务都会 import jevq；子进程继承 TZ 环境变量。
"""
import os
import sys
import time

TZ_VALUE = "CST-8"          # POSIX 写法：名称 CST，比 UTC 早 8 小时


def force_beijing() -> None:
    if os.environ.get("JEVQ_KEEP_LOCAL_TZ") == "1":      # 只给排查问题留的口子
        return
    os.environ["TZ"] = TZ_VALUE
    if sys.platform == "win32":
        import ctypes
        ctypes.CDLL("ucrtbase")._tzset()
    else:
        time.tzset()
