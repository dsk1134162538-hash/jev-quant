"""7x24 快讯存档（dj 2026-11-02）：盘中每分钟把财联社 / 东财快讯带时间戳写进 data/minutes.sqlite 的 news_flash。

想验证「快讯出来几十秒内买相关题材股」有没有空间，历史快讯拿不到，只能从现在开始存；配合分时存档，
收盘后能对照快讯时刻之后 1/3/10 分钟的走势。只记录，不参与交易；出错由调用方吞掉。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from jevq.data.touch_snaps import MINUTES_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_flash (ts TEXT, source TEXT, title TEXT, seen TEXT, PRIMARY KEY(source, title, ts));
"""


class FlashArchiver:
    def __init__(self, db: Path = MINUTES_DB):
        self.db = Path(db)

    def step(self, flash, now: datetime) -> int:
        """flash：jevq.data.news7x24.Flash（共用它的 60 秒缓存）。返回新存的条数。"""
        items = flash.items()
        if not items:
            return 0
        self.db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db, timeout=10)
        try:
            conn.executescript(SCHEMA)
            before = conn.total_changes
            conn.executemany("INSERT OR IGNORE INTO news_flash VALUES (?, ?, ?, ?)",
                             [(ts.isoformat(timespec="seconds"), src, title, now.isoformat(timespec="seconds"))
                              for ts, src, title in items if title])
            conn.commit()
            return conn.total_changes - before
        finally:
            conn.close()
