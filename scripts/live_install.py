"""家里电脑一键安装实盘下单层：xiadan-gateway + Tailscale 地址。可以重复运行，做过的步骤会跳过。

在开发目录（E:\\claude\\jev-quant）里运行（home_setup.bat 会替你调用）：
  .venv\\Scripts\\python scripts\\live_install.py [--gateway-dir E:\\xiadan-gateway] [--xiadan 路径\\xiadan.exe] [--force]

1. 网关目录不存在就 git clone，并固定到审查过的提交 PIN（docs/research/research-gateway.md）；
   已存在就确认还在 PIN 上，不在就停下（不自动升级没审查过的代码）。
2. 用 uv 装网关依赖（uv 没有就先 pip 装进当前 Python），再补装 numpy（网关漏了这个依赖）。
3. 写网关配置 config/app_config.json：只监听 127.0.0.1:5000、开 token 认证、关告警 webhook、开窗口监控、
   xiadan.exe 路径。其它字段保留。token 优先用已有的 XIADAN_GATEWAY_TOKEN，其次用配置里已有的，都没有就新生成；
   写进网关配置和当前用户环境变量（注册表 HKCU\\Environment），**绝不打印**。
4. Tailscale 装好了就读本机 100.x 地址，设成 JEVQ_LIVE_HOST（手机页面监听这个地址）。
5. 建开机启动项「xiadan网关」（最小化窗口运行），结束旧的网关进程，启动新的，等 /health 回来报告状态。
   交易时段（周一至周五 09:15–15:30）拒绝重启网关，除非带 --force。
**网关仓库 scripts/ 目录下的脚本会真实下单，本脚本绝不运行它们，你也绝不要运行。**
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import time as time_mod
from datetime import datetime, time
from pathlib import Path

DEV = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEV))

from jevq.notify import _windows_env_fallback  # noqa: E402

REPO_URL = "https://github.com/hylyq/xiadan-gateway.git"
PIN = "1662adf5b7d0afc73d1ee2f0f15101104d2c07af"
PATCH_FILE = DEV / "patches" / "xiadan-gateway-ocr-alnum.patch"    # 本地增量，锁在 PIN 之上
PORT = 5000
SHORTCUT = "xiadan网关"
SESSION_START = time(9, 15)
SESSION_END = time(15, 30)
PLACEHOLDER = "<你生成的随机串>"
XIADAN_CANDIDATES = [Path(p) for p in (
    r"C:\同花顺软件\同花顺\xiadan.exe", r"D:\同花顺软件\同花顺\xiadan.exe", r"E:\同花顺软件\同花顺\xiadan.exe",
    r"C:\Program Files (x86)\同花顺\xiadan.exe", r"C:\Program Files\同花顺\xiadan.exe",
    r"D:\Program Files (x86)\同花顺\xiadan.exe", r"D:\Program Files\同花顺\xiadan.exe",
)]


def run(args, cwd=None):
    print("$", " ".join(str(a) for a in args))
    subprocess.run([str(a) for a in args], cwd=cwd, check=True)


def in_session(now: datetime) -> bool:
    return now.weekday() < 5 and SESSION_START <= now.time() <= SESSION_END


# ---- 1. 代码 ----

def sync_gateway(gw: Path) -> None:
    if not gw.exists():
        run(["git", "clone", REPO_URL, gw])
        run(["git", "checkout", "--quiet", PIN], cwd=gw)
        return
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=gw, check=True, capture_output=True,
                          text=True).stdout.strip()
    if head != PIN:
        sys.exit(f"{gw} 不在审查过的提交 {PIN[:7]} 上（现在是 {head[:7]}），先人工确认，安装停止")
    print(f"网关代码已在 {PIN[:7]}")


def apply_patch(gw: Path) -> None:
    """幂等地把本地增量补丁应用到网关目录。PIN 锁上游代码，patch 锁本地增量。
    判定：git apply --check 成功 → 未 apply → 打上；否则 git apply --check -R 成功 → 已 apply → 跳过；
    两者都失败 → 与其它未提交改动冲突 → 停下让人工看。"""
    if not PATCH_FILE.exists():
        print(f"没找到补丁 {PATCH_FILE}，跳过（网关跑的是纯上游代码）")
        return
    forward = subprocess.run(["git", "apply", "--check", str(PATCH_FILE)], cwd=gw, capture_output=True)
    if forward.returncode == 0:
        run(["git", "apply", str(PATCH_FILE)], cwd=gw)
        print(f"补丁已应用：{PATCH_FILE.name}")
        return
    reverse = subprocess.run(["git", "apply", "--check", "-R", str(PATCH_FILE)], cwd=gw, capture_output=True)
    if reverse.returncode == 0:
        print(f"补丁已在网关里：{PATCH_FILE.name}")
        return
    sys.exit(f"补丁 {PATCH_FILE.name} 既不能 apply、也不能反向 apply（网关目录有别的未提交改动？）；"
             f"人工看 `git -C {gw} diff` 后再决定")


# ---- 2. 依赖 ----

def install_deps(gw: Path) -> None:
    if subprocess.run([sys.executable, "-m", "uv", "--version"], capture_output=True).returncode != 0:
        run([sys.executable, "-m", "pip", "install", "-q", "uv"])
    # --extra ocr：装 ddddocr（识别字母数字混合验证码，见 patches/xiadan-gateway-ocr-alnum.patch）
    run([sys.executable, "-m", "uv", "sync", "--extra", "ocr"], cwd=gw)
    # 网关顶层 import numpy 却没写进依赖；用 uv pip 装进它的 .venv，不改网关仓库里的文件
    run([sys.executable, "-m", "uv", "pip", "install", "--python", gw / ".venv" / "Scripts" / "python.exe",
         "numpy"], cwd=gw)


# ---- 3. 配置 + token ----

def find_xiadan(candidates=XIADAN_CANDIDATES) -> Path | None:
    return next((p for p in candidates if p.exists()), None)


def choose_token(env_token: str | None, config: dict) -> str:
    """已有的环境变量优先（live.py 用它），其次沿用配置里的，都没有就新生成。"""
    if env_token:
        return env_token
    existing = ((config.get("auth") or {}).get("token") or "").strip()
    if existing and existing != PLACEHOLDER:
        return existing
    return secrets.token_hex(24)


def build_config(existing: dict, token: str, xiadan: Path) -> dict:
    """在已有配置上强制安全相关字段，其它字段原样保留。"""
    cfg = dict(existing)
    cfg["host"] = "127.0.0.1"           # 只给本机用：手机走 live.py 的页面，不直连网关
    cfg["port"] = PORT
    cfg["auth"] = {**(existing.get("auth") or {}), "enabled": True, "token": token}
    cfg["window_monitor"] = {**(existing.get("window_monitor") or {}), "enabled": True}
    cfg["alerts"] = {**(existing.get("alerts") or {}), "webhook_url": ""}   # 不往外发任何告警
    cfg["trading_app_paths"] = [str(xiadan)]
    return cfg


def set_user_env(name: str, value: str) -> None:
    """写当前用户环境变量（同 setx，但不经过命令行参数），并通知系统，之后新开的程序就能读到。"""
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 0x2, 5000, None)
    os.environ[name] = value


def write_config(gw: Path, xiadan: Path) -> None:
    path = gw / "config" / "app_config.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    env_token = os.environ.get("XIADAN_GATEWAY_TOKEN") or _windows_env_fallback("XIADAN_GATEWAY_TOKEN")
    token = choose_token(env_token, existing)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(build_config(existing, token, xiadan), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    if token != env_token:
        set_user_env("XIADAN_GATEWAY_TOKEN", token)
    print(f"网关配置已写好：{path}（token 已设好，不显示）；xiadan.exe：{xiadan}")


# ---- 4. Tailscale ----

def tailscale_ip() -> str | None:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [s.strip() for s in out.stdout.splitlines() if s.strip()]
    return lines[0] if out.returncode == 0 and lines and lines[0].startswith("100.") else None


def ensure_live_host() -> None:
    ip = tailscale_ip()
    if ip is None:
        print("没找到 Tailscale（或还没登录）：手机暂时连不上页面。装好登录后重跑本脚本即可。")
        return
    current = os.environ.get("JEVQ_LIVE_HOST") or _windows_env_fallback("JEVQ_LIVE_HOST")
    if current != ip:
        set_user_env("JEVQ_LIVE_HOST", ip)
    print(f"手机页面地址：http://{ip}:8766 （手机也要开着 Tailscale）")


# ---- 5. 自启 + 启动 ----

def write_start_bat(gw: Path) -> Path:
    bat = gw / "start_gateway.bat"
    # newline=""：原样写 CRLF（不然 Windows 上 \r\n 会被再转一次）
    bat.write_text(f'@echo off\r\ncd /d "{gw}"\r\n.venv\\Scripts\\python.exe main.py\r\n', encoding="gbk",
                   newline="")
    return bat


def create_shortcut(bat: Path) -> None:
    ps = ("$ws = New-Object -ComObject WScript.Shell;"
          "$d = [Environment]::GetFolderPath('Startup');"
          f"$l = $ws.CreateShortcut(\"$d\\{SHORTCUT}.lnk\");"
          f"$l.TargetPath = '{bat}';"
          f"$l.WorkingDirectory = '{bat.parent}';"
          "$l.WindowStyle = 7;"      # 最小化
          "$l.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
    print(f"已建开机启动项：{SHORTCUT}")


def stop_gateway_script(gw: Path, port: int) -> str:
    """开机自启的网关命令行是相对路径（.venv\\Scripts\\python.exe main.py），光按目录匹配找不到，
    所以再按监听端口找：只杀 python 进程，连同 python 父进程（uv 的启动壳）一起。"""
    return (
        "$ids = New-Object 'System.Collections.Generic.HashSet[int]';"
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -and "
        f"$_.CommandLine.Contains('{gw}') }} | ForEach-Object {{ [void]$ids.Add($_.ProcessId) }};"
        f"Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | ForEach-Object {{ "
        "$p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue;"
        "if ($p -and $p.ProcessName -like 'python*') { [void]$ids.Add($p.Id);"
        "$pp = (Get-CimInstance Win32_Process -Filter \"ProcessId=$($p.Id)\").ParentProcessId;"
        "$q = Get-Process -Id $pp -ErrorAction SilentlyContinue;"
        "if ($q -and $q.ProcessName -like 'python*') { [void]$ids.Add($pp) } } };"
        "foreach ($i in $ids) { Stop-Process -Id $i -Force -ErrorAction SilentlyContinue; $i }"
    )


def stop_old_gateway(gw: Path) -> None:
    ps = stop_gateway_script(gw, PORT)
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.split()
    print(f"已结束旧网关进程：{out}" if out else "没有在跑的旧网关")


def start_gateway(bat: Path) -> None:
    # start 的第一个带引号参数是窗口标题；最小化窗口跑，出错时点开就能看到报错
    subprocess.run(f'start "xiadan-gateway" /min "{bat}"', shell=True, cwd=bat.parent, check=True)


def wait_health(get=None, sleep=time_mod.sleep, tries: int = 20) -> dict | None:
    """等网关起来：/health 免认证，只报是否认到客户端、是否登录，不涉及 token。"""
    if get is None:
        import requests
        get = requests.get
    for _ in range(tries):
        try:
            r = get(f"http://127.0.0.1:{PORT}/health", timeout=5)
            return r.json().get("data") or {}
        except Exception:
            sleep(2)
    return None


def report_health(data: dict | None) -> None:
    if data is None:
        print("网关 40 秒内没起来：打开任务栏里最小化的 xiadan-gateway 窗口看报错，截图发给 Claude")
        return
    print(f"网关已启动：认到下单客户端 {bool(data.get('xiadan_running'))}，已登录 {bool(data.get('logged_in'))}")
    if not data.get("logged_in"):
        print("同花顺独立下单客户端还没打开或没登录：登录后浏览器打开 http://127.0.0.1:5000/health 看 logged_in")


def main() -> None:
    parser = argparse.ArgumentParser(description="家里电脑一键安装实盘下单层（xiadan-gateway + Tailscale 地址）")
    parser.add_argument("--gateway-dir", type=Path, default=Path(r"E:\xiadan-gateway"))
    parser.add_argument("--xiadan", type=Path, help="同花顺独立下单 xiadan.exe 的路径；不给就在常见位置找")
    parser.add_argument("--force", action="store_true", help="交易时段也重启网关")
    args = parser.parse_args()

    if in_session(datetime.now()) and not args.force:
        sys.exit("交易时段（周一至周五 09:15–15:30）不重启网关（确实要做就加 --force）")
    xiadan = args.xiadan or find_xiadan()
    while xiadan is None or not xiadan.exists():
        typed = input("没找到同花顺独立下单的 xiadan.exe。开始菜单里右键它「打开文件所在位置」，"
                      "把完整路径粘贴到这里后回车：").strip().strip('"')
        xiadan = Path(typed) if typed else None
    gw = args.gateway_dir
    sync_gateway(gw)
    apply_patch(gw)
    install_deps(gw)
    write_config(gw, xiadan)
    ensure_live_host()
    bat = write_start_bat(gw)
    create_shortcut(bat)
    stop_old_gateway(gw)
    start_gateway(bat)
    report_health(wait_health())
    print("网关装好了。提醒：网关仓库 scripts/ 目录下的脚本会真实下单，绝不要运行。")


if __name__ == "__main__":
    main()
