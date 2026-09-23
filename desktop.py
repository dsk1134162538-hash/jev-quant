import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
# pythonw 没有控制台，sys.stdout/stderr 为 None；akshare 的 tqdm 进度条一写就崩
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import webview  # noqa: E402

from jevq.app.api import Api  # noqa: E402


def main():
    api = Api()
    window = webview.create_window("Jev 低吸模拟盘", str(ROOT / "jevq" / "app" / "ui.html"),
                                   js_api=api, width=1280, height=820, min_size=(960, 640))

    def on_closing(*_):
        api.stop()

    window.events.closing += on_closing
    webview.start()


if __name__ == "__main__":
    main()
