"""9% 触线快照（dj 2026-11-02）：全市场沪深主板票当天第一次涨到 ≥9% 的那一分钟，记下盘口（买卖五档）、
量能、换手和这一刻全市场的封板/触线家数，写进 data/minutes.sqlite 的 touch_snaps。

回测里破板的票占 43%、每笔 −3.6%，买点只能靠分辨个股；这些字段历史上拿不到，只能从现在开始攒。
只记录，不参与任何买卖；出错由调用方吞掉。分钟级：一分钟内从 8% 直接封死的票第一条就是 sealed=1（买不到）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from jevq.models import Quote

MINUTES_DB = Path(__file__).resolve().parents[2] / "data" / "minutes.sqlite"
TOUCH_PCT = 9.0
MAIN_BOARD = ("60", "00")

SCHEMA = """
CREATE TABLE IF NOT EXISTS touch_snaps (
    date TEXT, code TEXT, name TEXT, ts TEXT, hhmm TEXT, pct REAL, price REAL, pre_close REAL, open REAL,
    high REAL, limit_up REAL, sealed INTEGER, volume INTEGER, amount REAL, turnover REAL, float_mcap REAL,
    outer_vol REAL, inner_vol REAL, bid1 REAL, bid_vols TEXT, ask_vols TEXT,
    market_sealed INTEGER, market_touched INTEGER, PRIMARY KEY(date, code));
"""


class TouchRecorder:
    def __init__(self, db: Path = MINUTES_DB):
        self.db = Path(db)
        self._day: date | None = None
        self._seen: set[str] = set()

    def _load_day(self, conn, day: date):
        self._day = day
        self._seen = {r[0] for r in conn.execute("SELECT code FROM touch_snaps WHERE date=?", (day.isoformat(),))}

    def step(self, quotes: dict[str, Quote], now: datetime) -> int:
        """这一分钟的全市场行情 → 新触线的票入库，返回新记条数。"""
        day = now.date()
        fresh = [q for q in quotes.values() if q.pre_close > 0 and q.ts.date() == day]
        m_sealed = sum(1 for q in fresh if q.limit_up > 0 and q.price >= q.limit_up)
        m_touched = sum(1 for q in fresh if q.pct >= TOUCH_PCT)
        hits = [q for q in fresh if q.code.startswith(MAIN_BOARD) and q.pct >= TOUCH_PCT]
        if not hits and self._day == day:
            return 0
        self.db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db, timeout=10)
        try:
            conn.executescript(SCHEMA)
            if self._day != day:
                self._load_day(conn, day)
            new = [q for q in hits if q.code not in self._seen]
            conn.executemany("INSERT OR IGNORE INTO touch_snaps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                (day.isoformat(), q.code, q.name, now.isoformat(timespec="seconds"), now.strftime("%H:%M"),
                 q.pct, q.price, q.pre_close, q.open, q.high, q.limit_up, int(q.price >= q.limit_up),
                 q.volume, q.amount, q.turnover, q.float_mcap, q.outer_vol, q.inner_vol, q.bid1,
                 json.dumps(list(q.bid_vols)), json.dumps(list(q.ask_vols)), m_sealed, m_touched)
                for q in new])
            conn.commit()
        finally:
            conn.close()
        self._seen.update(q.code for q in new)
        return len(new)
