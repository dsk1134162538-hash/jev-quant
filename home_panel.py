"""家里电脑的只读面板入口（桌面快捷方式「Jev模拟盘（家里只读）」指向这里）。

打开时：先尝试拉一次 GitHub（只在 master 分支、没有未提交改动时 fast-forward，失败就用本地已有的数据），
再用 exports/ 重建 data/home_view.sqlite，然后打开和公司一样的面板窗口。开始运行、保存规则一律拦截。
实盘只在公司电脑跑，这里绝不启动交易。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def bootstrap() -> None:
    os.chdir(ROOT)
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                          creationflags=flags)


def pull_latest(git=_git) -> str:
    """能安全拉就拉：当前在 master、没有未提交的改动，才 fast-forward。返回给面板顶部看的一句说明（成功返回空串）。"""
    try:
        branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != "master":
            return f"当前在 {branch} 分支，没有拉取最新数据"
        if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
            return "本地有未提交的改动，没有拉取最新数据"
        r = git("pull", "--ff-only", "origin", "master", timeout=60)
        if r.returncode != 0:
            return "拉取 GitHub 失败，显示的是本地已有数据"
        return ""
    except Exception:
        return "拉取 GitHub 失败，显示的是本地已有数据"


def main():
    import webview

    from jevq.app.home_view import HOME_DB, ReadOnlyApi, build_db

    note = pull_latest()
    try:
        as_of = build_db(ROOT / "exports", ROOT / HOME_DB)
    except Exception as e:
        as_of, note = None, f"导出数据读取失败：{e!r}"
    api = ReadOnlyApi(db_path=str(ROOT / HOME_DB), as_of=as_of, note=note)
    webview.create_window("Jev 模拟盘（家里只读）", str(ROOT / "jevq" / "app" / "ui.html"),
                          js_api=api, width=1280, height=820, min_size=(960, 640))
    webview.start()


if __name__ == "__main__":
    bootstrap()
    main()
