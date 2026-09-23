import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Jev模拟盘后台"


def main():
    target = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    script = ROOT / "daemon.py"
    ps = ("$ws = New-Object -ComObject WScript.Shell;"
          "$d = [Environment]::GetFolderPath('Startup');"
          f"$l = $ws.CreateShortcut(\"$d\\{NAME}.lnk\");"
          f"$l.TargetPath = '{target}';"
          f"$l.Arguments = '\"{script}\"';"
          f"$l.WorkingDirectory = '{ROOT}';"
          "$l.Save(); Write-Output \"$d\\" + NAME + ".lnk\"")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         check=True, capture_output=True, text=True)
    print("已创建启动快捷方式：", out.stdout.strip())


if __name__ == "__main__":
    main()
