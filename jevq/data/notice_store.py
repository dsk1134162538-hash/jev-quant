import sqlite3
from collections.abc import Callable
from datetime import date, timedelta

from jevq.config import NOTICE_LOOKBACK_DAYS
from jevq.data.akdata import Notice, fetch_notices_for_date


def sync_notices(conn: sqlite3.Connection, today: date,
                 fetch: Callable[[date], list[Notice]] = fetch_notices_for_date,
                 lookback: int = NOTICE_LOOKBACK_DAYS) -> int:
    done = {r[0] for r in conn.execute("SELECT date FROM notice_fetch")}
    total = 0
    for i in range(lookback, -1, -1):
        d = today - timedelta(days=i)
        if d != today and d.isoformat() in done:
            continue
        try:
            items = fetch(d)
        except Exception as e:
            print(f"[notices] {d} 拉取失败: {e!r}")
            continue
        conn.executemany("INSERT OR IGNORE INTO notices(id, code, title, type, date) VALUES (?, ?, ?, ?, ?)",
                         [(n.id, n.code, n.title, n.type, n.date) for n in items])
        if d != today:
            conn.execute("INSERT OR IGNORE INTO notice_fetch(date) VALUES (?)", (d.isoformat(),))
        conn.commit()
        total += len(items)
    return total


def notices_for(conn: sqlite3.Connection, code: str, today: date,
                lookback: int = NOTICE_LOOKBACK_DAYS) -> list[Notice]:
    since = (today - timedelta(days=lookback)).isoformat()
    rows = conn.execute("SELECT id, code, title, type, date FROM notices "
                        "WHERE code=? AND date>=? ORDER BY date DESC, id", (code, since)).fetchall()
    return [Notice(*r) for r in rows]
