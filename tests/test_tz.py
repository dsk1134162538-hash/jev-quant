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
    out = subprocess.run([sys.executable, "-c", CODE.format(tz=tz)], capture_output=True, text=True, env=env,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), timeout=60)
    return out.stdout.strip()


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
