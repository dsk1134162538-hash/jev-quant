import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Jev低吸模拟盘"
HOME_NAME = "Jev模拟盘（家里只读）"


def main(home: bool = False):
    """默认建公司用的面板快捷方式；--home 建家里只读面板的快捷方式（不能开跑实盘）。"""
    target = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    script = ROOT / ("home_panel.py" if home else "desktop.py")
    name = HOME_NAME if home else NAME
    ps = ("$ws = New-Object -ComObject WScript.Shell;"
          "$d = [Environment]::GetFolderPath('Desktop');"
          f"$l = $ws.CreateShortcut(\"$d\\{name}.lnk\");"
          f"$l.TargetPath = '{target}';"
          f"$l.Arguments = '\"{script}\"';"
          f"$l.WorkingDirectory = '{ROOT}';"
          "$l.Save(); Write-Output \"$d\\" + name + ".lnk\"")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         check=True, capture_output=True, text=True)
    print("已创建快捷方式：", out.stdout.strip())


if __name__ == "__main__":
    import sys
    main(home="--home" in sys.argv)
