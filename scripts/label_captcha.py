"""手工标注同花顺验证码存档：一张张看图、键盘输真值，复制成 `真值_原ID.png`。

    .venv/Scripts/python scripts/label_captcha.py [--src DIR] [--dst DIR]

只读源目录（不改不删），标注结果放到 dst（默认 E:/xiadan-gateway/assets/captcha_labeled）。
输出文件名格式 `SmKf_1790743883078.png`，dddd_trainer 取 `_` 前面当标签。
可随时关窗，下次运行自动跳过已标/已跳过的图。

按键：Enter 保存（必须 4 位字母数字，区分大小写）　Esc 跳过（看不清/不是验证码）
　　　Left 回上一张改标签　Ctrl+Q 退出
"""
import argparse
import re
import sys
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk

SRC_DEFAULT = Path("E:/xiadan-gateway/assets/captcha_archive")
DST_DEFAULT = Path("E:/xiadan-gateway/assets/captcha_labeled")
LABEL_RE = re.compile(r"[A-Za-z0-9]{4}")
SCALE = 8


def src_id(p: Path) -> str:
    return p.stem.split("_", 1)[1]


class App:
    def __init__(self, root, files, dst):
        self.root, self.files, self.dst = root, files, dst
        self.skipped_file = dst / "skipped.txt"
        self.skipped = set(self.skipped_file.read_text().split()) if self.skipped_file.exists() else set()
        self.i = 0
        self.history = []  # 已处理过的下标，供「上一张」使用
        self.img_label = tk.Label(root, bg="white")
        self.img_label.pack(padx=20, pady=(20, 10))
        self.entry = tk.Entry(root, font=("Consolas", 32), justify="center", width=8)
        self.entry.pack(pady=10)
        self.status = tk.Label(root, font=("Microsoft YaHei", 11), fg="#555")
        self.status.pack(pady=(0, 15))
        root.bind("<Return>", self.save)
        root.bind("<Escape>", self.skip)
        root.bind("<Left>", self.back)
        root.bind("<Control-q>", lambda e: root.destroy())
        self.entry.focus_set()
        self.advance_to_pending()

    def labeled(self, sid):
        return next(self.dst.glob(f"*_{sid}.png"), None)

    def pending(self, i):
        sid = src_id(self.files[i])
        return sid not in self.skipped and self.labeled(sid) is None

    def advance_to_pending(self):
        while self.i < len(self.files) and not self.pending(self.i):
            self.i += 1
        self.show()

    def show(self):
        if self.i >= len(self.files):
            self.status.config(text="全部处理完了，可以关窗", fg="#080")
            self.img_label.config(image="", text="✓", font=("Consolas", 40))
            return
        img = Image.open(self.files[self.i]).convert("RGB")
        img = img.resize((img.width * SCALE, img.height * SCALE), Image.NEAREST)
        self.tk_img = ImageTk.PhotoImage(img)
        self.img_label.config(image=self.tk_img)
        n_lab = len(list(self.dst.glob("*_*.png")))
        self.status.config(text=f"{self.i + 1}/{len(self.files)}　已标 {n_lab}　跳过 {len(self.skipped)}　"
                                f"Enter 保存 · Esc 跳过 · ← 上一张", fg="#555")
        self.entry.delete(0, tk.END)
        old = self.labeled(src_id(self.files[self.i]))
        if old:
            self.entry.insert(0, old.stem.split("_")[0])

    def save(self, _e=None):
        if self.i >= len(self.files):
            return
        text = self.entry.get().strip()
        if not LABEL_RE.fullmatch(text):
            self.status.config(text=f"「{text}」不是 4 位字母数字", fg="#c00")
            return
        f = self.files[self.i]
        sid = src_id(f)
        target = self.dst / f"{text}_{sid}.png"
        old = self.labeled(sid)
        if old and old.name != target.name:
            # 只改自己复制出来的文件名；NTFS 不分大小写，仅大小写不同的改名要绕一步
            tmp = old.with_suffix(".tmp")
            old.replace(tmp)
            tmp.replace(target)
        elif not old:
            target.write_bytes(f.read_bytes())
        self.skipped.discard(sid)
        self.write_skipped()
        self.history.append(self.i)
        self.i += 1
        self.advance_to_pending()

    def skip(self, _e=None):
        if self.i >= len(self.files):
            return
        self.skipped.add(src_id(self.files[self.i]))
        self.write_skipped()
        self.history.append(self.i)
        self.i += 1
        self.advance_to_pending()

    def back(self, _e=None):
        if self.history:
            self.i = self.history.pop()
            self.show()

    def write_skipped(self):
        self.skipped_file.write_text("\n".join(sorted(self.skipped)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=SRC_DEFAULT)
    ap.add_argument("--dst", type=Path, default=DST_DEFAULT)
    args = ap.parse_args()
    files = sorted(args.src.glob("failed_*.png"))
    if not files:
        sys.exit(f"{args.src} 下没有 failed_*.png")
    args.dst.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    root.title("验证码标注")
    App(root, files, args.dst)
    root.mainloop()


if __name__ == "__main__":
    main()
