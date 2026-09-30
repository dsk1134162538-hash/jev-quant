"""家里电脑一键安装 / 更新运行目录（只跑不改的那份代码）。

在开发目录（E:\\claude\\jev-quant）里运行：
  .venv\\Scripts\\python scripts\\home_install.py [--run-dir E:\\jev-live] [--db 路径\\jevq.sqlite.gz] [--force]

可以重复运行，做过的步骤会跳过：
1. 运行目录不存在就 git clone（master）；存在就 fetch 后 rebase 到 origin/master（有未提交改动或冲突就停下）。
2. 建 .venv，装 requirements.txt。
3. data/jevq.sqlite 不存在时放入公司电脑迁来的数据库：给了 --db 就用这个文件，否则从 GitHub 的 migration-data 分支取。
   已存在绝不覆盖。
4. 检查凭证环境变量有没有设（只说有没有，不打印值）。TYPESAFE_API_KEY、PUSHPLUS_TOKEN 没设就停下；
   实盘用的 XIADAN_GATEWAY_TOKEN、JEVQ_LIVE_HOST 没设只提示「实盘需要」，不算失败。
5. 建开机启动项，结束旧的后台进程，用运行目录的代码启动 daemon.py（有 live.py 也一起启动）。
   交易时段（周一至周五 09:15–15:30，含 daemon 15:05–15:20 的同步/上线）拒绝重启后台，除非带 --force。
"""
import argparse
import gzip
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path

DEV = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEV))

from jevq.notify import _windows_env_fallback  # noqa: E402

REQUIRED = ("TYPESAFE_API_KEY", "PUSHPLUS_TOKEN")
LIVE_ONLY = ("XIADAN_GATEWAY_TOKEN", "JEVQ_LIVE_HOST")      # 只有实盘（live.py）用得上
PASS_THROUGH = REQUIRED + LIVE_ONLY
SESSION_START = time(9, 15)
SESSION_END = time(15, 30)      # daemon 15:05–15:20 还在同步/上线（git fetch/rebase/push），这段也不许装
DATA_BRANCH = "migration-data"
DATA_FILE = "jevq.sqlite.gz"
SCRIPTS = ("daemon.py", "live.py")


def run(args, cwd=None):
    print("$", " ".join(str(a) for a in args))
    subprocess.run([str(a) for a in args], cwd=cwd, check=True)


def git_out(args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def sync_code(run_dir: Path) -> None:
    if not run_dir.exists():
        run(["git", "clone", "--branch", "master", git_out(["remote", "get-url", "origin"], DEV), run_dir])
        return
    if git_out(["status", "--porcelain"], run_dir):
        sys.exit(f"{run_dir} 有未提交的改动，先人工处理，安装停止")
    run(["git", "fetch", "origin"], cwd=run_dir)
    run(["git", "checkout", "master"], cwd=run_dir)
    try:
        run(["git", "rebase", "origin/master"], cwd=run_dir)
    except subprocess.CalledProcessError:
        subprocess.run(["git", "rebase", "--abort"], cwd=run_dir)
        sys.exit("rebase 冲突，已撤回，运行目录保持原样，安装停止")


def ensure_venv(run_dir: Path) -> None:
    py = run_dir / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        run([sys.executable, "-m", "venv", run_dir / ".venv"])
    run([py, "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=run_dir)


def ensure_db(run_dir: Path, db_gz: Path | None) -> None:
    db = run_dir / "data" / "jevq.sqlite"
    if db.exists():
        print(f"{db} 已存在，不覆盖")
        return
    if db_gz is not None:
        packed = db_gz.read_bytes()
    else:
        run(["git", "fetch", "origin", DATA_BRANCH], cwd=run_dir)
        packed = subprocess.run(["git", "show", f"origin/{DATA_BRANCH}:{DATA_FILE}"], cwd=run_dir,
                                check=True, capture_output=True).stdout
    db.parent.mkdir(exist_ok=True)
    part = db.with_name("jevq.sqlite.part")
    part.write_bytes(gzip.decompress(packed))
    conn = sqlite3.connect(part)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        days, last = conn.execute("SELECT COUNT(DISTINCT date), MAX(date) FROM closes").fetchone()
    except sqlite3.DatabaseError:
        ok = False
    finally:
        conn.close()
    if not ok:
        part.unlink()
        sys.exit("数据库完整性检查没过，安装停止")
    part.replace(db)
    print(f"数据库已就位：{db}（收盘价 {days} 天，最后 {last}）")


def check_env() -> bool:
    ok = True
    for name in REQUIRED:
        present = bool(os.environ.get(name) or _windows_env_fallback(name))
        print(f"{name}：{'已设置' if present else '没有！后台跑不起来，先 setx 设好再重跑'}")
        ok = ok and present
    for name in LIVE_ONLY:
        present = bool(os.environ.get(name) or _windows_env_fallback(name))
        print(f"{name}：{'已设置' if present else '没设置（实盘需要，先 setx 设好；只跑模拟盘可以先不管）'}")
    return ok


def in_session(now: datetime) -> bool:
    return now.weekday() < 5 and SESSION_START <= now.time() <= SESSION_END


def stop_old() -> None:
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and "
          "$_.CommandLine -match '(daemon|live)\\.py' } | ForEach-Object { "
          "Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.split()
    print(f"已结束旧后台进程：{out}" if out else "没有在跑的旧后台进程")


def restart(run_dir: Path) -> None:
    run([run_dir / ".venv" / "Scripts" / "python.exe", "install_autostart.py"], cwd=run_dir)
    stop_old()
    env = dict(os.environ)
    for name in PASS_THROUGH:
        value = env.get(name) or _windows_env_fallback(name)
        if value:
            env[name] = value
    pythonw = run_dir / ".venv" / "Scripts" / "pythonw.exe"
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    for script in SCRIPTS:
        if (run_dir / script).exists():
            subprocess.Popen([str(pythonw), script], cwd=run_dir, env=env, creationflags=flags, close_fds=True)
            print(f"已启动 {script}")


def main() -> None:
    parser = argparse.ArgumentParser(description="家里电脑一键安装 / 更新运行目录")
    parser.add_argument("--run-dir", type=Path, default=Path(r"E:\jev-live"))
    parser.add_argument("--db", type=Path, help="公司电脑迁来的 jevq.sqlite.gz；不给就从 migration-data 分支取")
    parser.add_argument("--force", action="store_true", help="交易时段也重启后台")
    args = parser.parse_args()

    if in_session(datetime.now()) and not args.force:
        sys.exit("交易时段（周一至周五 09:15–15:30，含收盘后的同步/上线）不重启后台（确实要做就加 --force）")
    sync_code(args.run_dir)
    ensure_venv(args.run_dir)
    ensure_db(args.run_dir, args.db)
    if not check_env():
        sys.exit("凭证没设全，后台先不启动")
    restart(args.run_dir)
    print("完成。开机会自动启动后台；节后第一个交易日 08:50 开始跑。")


if __name__ == "__main__":
    main()
