import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Jev低吸模拟盘"


def main():
    target = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    script = ROOT / "desktop.py"
    ps = ("$ws = New-Object -ComObject WScript.Shell;"
          "$d = [Environment]::GetFolderPath('Desktop');"
          f"$l = $ws.CreateShortcut(\"$d\\{NAME}.lnk\");"
          f"$l.TargetPath = '{target}';"
          f"$l.Arguments = '\"{script}\"';"
          f"$l.WorkingDirectory = '{ROOT}';"
          "$l.Save(); Write-Output \"$d\\" + NAME + ".lnk\"")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         check=True, capture_output=True, text=True)
    print("已创建快捷方式：", out.stdout.strip())


if __name__ == "__main__":
    main()
