"""开盘首笔快照（dj 2026-11-03，知识库「集合竞价」）：每天第一次拿到今天行情的那一分钟，把全部沪深主板票的
开盘价、成交量/额（此刻 ≈ 竞价成交 + 开盘头几秒）、五档挂单存进 data/minutes.sqlite 的 open_snaps。

知识库里最具体的筛选条件之一是「竞价量占昨日成交额的比例」（小票 10~20%、十亿到二十亿 5~8%、二十亿以上约 4%，
开得越高要求越高）。历史上没有竞价量，只能从现在开始攒，攒够几周用它回测弱转强、一进二、接力。只记录，不参与交易。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from jevq.data.touch_snaps import MAIN_BOARD, MINUTES_DB
from jevq.models import Quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS open_snaps (
    date TEXT, code TEXT, name TEXT, ts TEXT, open REAL, price REAL, pre_close REAL, limit_up REAL,
    volume INTEGER, amount REAL, bid_vols TEXT, ask_vols TEXT, PRIMARY KEY(date, code));
"""


class OpenSnapper:
    def __init__(self, db: Path = MINUTES_DB):
        self.db = Path(db)
        self._done: date | None = None

    def step(self, quotes: dict[str, Quote], now: datetime) -> int:
        day = now.date()
        if self._done == day:
            return 0
        fresh = [q for q in quotes.values() if q.code.startswith(MAIN_BOARD) and q.ts.date() == day and q.pre_close > 0]
        if not fresh:
            return 0
        self.db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db, timeout=10)
        try:
            conn.executescript(SCHEMA)
            if conn.execute("SELECT 1 FROM open_snaps WHERE date=? LIMIT 1", (day.isoformat(),)).fetchone():
                self._done = day
                return 0
            conn.executemany("INSERT OR IGNORE INTO open_snaps VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                (day.isoformat(), q.code, q.name, now.isoformat(timespec="seconds"), q.open, q.price, q.pre_close, q.limit_up,
                 q.volume, q.amount, json.dumps(list(q.bid_vols)), json.dumps(list(q.ask_vols))) for q in fresh])
            conn.commit()
        finally:
            conn.close()
        self._done = day
        return len(fresh)
