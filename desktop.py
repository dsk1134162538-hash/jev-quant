import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

import webview  # noqa: E402

from jevq.app.api import Api  # noqa: E402


def bootstrap() -> None:
    """仅在作为脚本运行时执行的有副作用的初始化：切换 cwd 到仓库根目录，
    并把 pythonw 下为 None 的 stdout/stderr 重定向到 devnull（akshare 的 tqdm 进度条一写就崩）。"""
    os.chdir(ROOT)
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def main():
    api = Api()
    window = webview.create_window("Jev 模拟盘", str(ROOT / "jevq" / "app" / "ui.html"),
                                   js_api=api, width=1280, height=820, min_size=(960, 640))

    def on_closing(*_):
        api.stop()

    window.events.closing += on_closing
    webview.start()


if __name__ == "__main__":
    bootstrap()
    main()
