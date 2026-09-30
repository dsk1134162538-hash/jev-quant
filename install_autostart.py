import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Jev模拟盘后台"
LIVE_NAME = "Jev实盘执行"


def _create_shortcut(name: str, script: Path) -> str:
    target = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    ps = ("$ws = New-Object -ComObject WScript.Shell;"
          "$d = [Environment]::GetFolderPath('Startup');"
          f"$l = $ws.CreateShortcut(\"$d\\{name}.lnk\");"
          f"$l.TargetPath = '{target}';"
          f"$l.Arguments = '\"{script}\"';"
          f"$l.WorkingDirectory = '{ROOT}';"
          "$l.Save(); Write-Output \"$d\\" + name + ".lnk\"")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def main():
    path = _create_shortcut(NAME, ROOT / "daemon.py")
    print("已创建启动快捷方式：", path)
    live_path = _create_shortcut(LIVE_NAME, ROOT / "live.py")
    print("已创建启动快捷方式：", live_path)


if __name__ == "__main__":
    main()
