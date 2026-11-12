"""进程时区固定为北京时间（dj 2026-11-01：系统时区不能改，Windows 现在是 +01:00）。"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

CODE = ("import os; os.environ['TZ'] = {tz!r}\n"
        "import jevq\n"
        "from datetime import datetime, date, timezone, timedelta\n"
        "bj = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)\n"
        "print(abs((datetime.now() - bj).total_seconds()) < 5, date.today() == bj.date())\n")


def _run(tz):
    env = {k: v for k, v in os.environ.items() if k != "JEVQ_KEEP_LOCAL_TZ"}
    # stdin 显式给 DEVNULL：上线闸门跑在 pythonw 下没有标准输入句柄，再起子进程可能 WinError 6（10-09 出过一次）。
    # 11-10 闸门在这条上挂过一次、报错没留下来，所以失败时把返回码和 stderr 一起带出来。
    out = subprocess.run([sys.executable, "-c", CODE.format(tz=tz)], capture_output=True, text=True, env=env,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), timeout=180,
                         stdin=subprocess.DEVNULL)
    if out.stdout.strip() != "True True":
        return f"{out.stdout.strip()!r} rc={out.returncode} stderr={out.stderr[-800:]!r}"
    return "True True"


def test_import_jevq_pins_beijing_time_whatever_the_machine_timezone_is():
    for tz in ("CET-1CEST", "EST5EDT", "UTC0", "PST8PDT"):
        assert _run(tz) == "True True", tz


def test_force_beijing_is_idempotent_in_this_process():
    from jevq import tz
    tz.force_beijing()
    tz.force_beijing()
    bj = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    assert abs((datetime.now() - bj).total_seconds()) < 5


def test_legacy_rules_are_off_by_default_in_config():
    import importlib
    import jevq.config as cfg
    src = open(cfg.__file__, encoding="utf-8").read()
    assert "LEGACY_BUY_ENABLED = False" in src                       # dj 2026-11-01 关掉老规则
