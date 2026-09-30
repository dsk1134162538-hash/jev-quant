"""live.py：唯一操作真实账户的进程。开机自启，约每秒扫一次意图和状态；下单、对账、暂停都在这里；
同时提供手机页面（jevq.live.web）。照 daemon.py 的 bootstrap()/_real_restart() 写法。
"""
import os
import subprocess
import sys
import time as time_mod
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

from jevq import db  # noqa: E402
from jevq.config import DB_PATH, LIVE_BROKER, LIVE_EASYTRADER_EXE, LIVE_GATEWAY_URL  # noqa: E402
from jevq.config import LIVE_HTTP_PORT, LIVE_LOCK_NAME, LIVE_RESTART_CHECK  # noqa: E402
from jevq.data import tencent  # noqa: E402
from jevq.live.easytrader_broker import EasytraderBroker  # noqa: E402
from jevq.live.executor import Executor  # noqa: E402
from jevq.live.gateway import GatewayBroker  # noqa: E402
from jevq.live.web import make_handler, serve  # noqa: E402
from jevq.notify import _windows_env_fallback, notifier_from_env  # noqa: E402
from jevq.runlock import hold_run_lock, is_held  # noqa: E402

AFTER_DEPLOY_ARG = "--after-deploy"
AFTER_DEPLOY_LOCK_WAIT = 60      # 秒：自动上线重启后，等旧进程释放 live.lock 的上限
LOG_DIR = Path("data")


def bootstrap() -> None:
    """仅在作为脚本运行时执行的有副作用的初始化：切换 cwd 到仓库根目录，
    创建 data/ 目录，并把 pythonw 下为 None 的 stdout/stderr 重定向到日志文件。"""
    os.chdir(ROOT)
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    if sys.stdout is None:
        sys.stdout = open(ROOT / "data" / "live.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(ROOT / "data" / "live.log", "a", encoding="utf-8", buffering=1)


def file_log(msg: str) -> None:
    path = LOG_DIR / "live.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def _gateway_token(env=os.environ) -> str | None:
    return env.get("XIADAN_GATEWAY_TOKEN") or _windows_env_fallback("XIADAN_GATEWAY_TOKEN")


def _live_host(env=os.environ) -> str | None:
    return env.get("JEVQ_LIVE_HOST") or _windows_env_fallback("JEVQ_LIVE_HOST")


def make_broker():
    """按 config.LIVE_BROKER 返回下单层实现；token/exe 路径不打印、不入库（凭证只经环境变量/注册表）。"""
    if LIVE_BROKER == "easytrader":
        return EasytraderBroker(LIVE_EASYTRADER_EXE)
    return GatewayBroker(LIVE_GATEWAY_URL, token=_gateway_token())


def _real_git_rev_parse(repo: Path) -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _real_restart(repo: Path, python_exe: Path) -> None:
    """自我重启：起一个独立的新后台进程跑新代码，当前进程随后返回、释放 live.lock。
    新进程带 AFTER_DEPLOY_ARG 启动：main() 会最多等 AFTER_DEPLOY_LOCK_WAIT 秒让旧进程释放锁。"""
    pythonw = python_exe.parent / "pythonw.exe"
    exe = pythonw if pythonw.exists() else python_exe
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe), str(repo / "live.py"), AFTER_DEPLOY_ARG], cwd=repo, close_fds=True, **kwargs)


def main(*, repo: Path = ROOT, python_exe: Path | None = None, conn=None, broker=None, notifier=None,
        clock=datetime.now, sleep=time_mod.sleep, quotes=None, git_rev_parse=None, restart=None,
        lock_wait: float = 0, log=file_log, executor_factory=Executor, web_serve=serve,
        make_handler_fn=make_handler, max_steps: int | None = None) -> int:
    """max_steps：仅测试用，限制主循环步数（生产不传，跑到自我重启或进程被杀为止）。"""
    python_exe = python_exe or Path(sys.executable)
    git_rev_parse = git_rev_parse or _real_git_rev_parse
    restart = restart or _real_restart
    quotes_fn = quotes or tencent.fetch_quotes

    lock_path = Path(DB_PATH).parent / LIVE_LOCK_NAME
    waited = 0.0
    while lock_wait > 0 and waited < lock_wait and is_held(lock_path):
        sleep(1)
        waited += 1

    try:
        with hold_run_lock(lock_path):
            conn = conn or db.connect(DB_PATH)
            broker = broker or make_broker()
            notifier = notifier if notifier is not None else notifier_from_env()
            executor = executor_factory(conn, broker, notifier, clock=clock, sleep=sleep, quotes=quotes_fn)
            executor.startup()

            hosts = ["127.0.0.1"]
            host = _live_host()
            if host:
                hosts.append(host)
            handler_cls = make_handler_fn(lambda: conn, clock=clock, quotes=quotes_fn)
            web_serve(hosts, LIVE_HTTP_PORT, handler_cls, log=log)

            running_commit = git_rev_parse(repo)
            restart_checked_on = None
            steps = 0
            while max_steps is None or steps < max_steps:
                try:
                    executor.step()
                except Exception as e:
                    log(f"执行异常：{e!r}")
                steps += 1

                now = clock()
                if now.time() >= LIVE_RESTART_CHECK and restart_checked_on != now.date():
                    restart_checked_on = now.date()
                    current = git_rev_parse(repo)
                    if current and running_commit and current != running_commit:
                        log(f"检测到新代码 {current}，自我重启")
                        try:
                            restart(repo, python_exe)
                        except Exception as e:
                            log(f"自我重启失败：{e!r}")
                        else:
                            return 0
                sleep(1)
            return 0
    except RuntimeError:
        log("已有另一个实盘进程在运行，退出")
        return 0


if __name__ == "__main__":
    bootstrap()
    sys.exit(main(lock_wait=AFTER_DEPLOY_LOCK_WAIT if AFTER_DEPLOY_ARG in sys.argv else 0))
