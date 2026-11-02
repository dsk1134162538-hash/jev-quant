"""分时存档（dj 2026-11-02）：每天收盘后把当天摸到过 9% 的主板票的逐分钟价格和累计量存下来。

腾讯分时只留最近几天，过期就没了；攒下来才能回测「几点、多快封板的票该打」「半路板从几个点开始追」
「实盘延迟让我们错过多少快板」。只读行情、只写 data/minutes.sqlite，不碰账户。
"""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MINUTES_DB = ROOT / "data" / "minutes.sqlite"
JEVQ_DB = ROOT / "data" / "jevq.sqlite"
TOUCH_PCT = 9.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_bars (date TEXT, code TEXT, hhmm TEXT, price REAL, cum_volume INTEGER,
                                        PRIMARY KEY(date, code, hhmm));
CREATE TABLE IF NOT EXISTS minute_days (date TEXT, code TEXT, name TEXT, pre_close REAL, limit_up REAL,
                                        high REAL, close REAL, bars INTEGER, PRIMARY KEY(date, code));
"""


def universe(db: Path = JEVQ_DB) -> list[str]:
    conn = sqlite3.connect(f"{Path(db).as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        return [r[0] for r in conn.execute("SELECT DISTINCT code FROM closes") if str(r[0]).startswith(("60", "00"))]
    finally:
        conn.close()


def archive_day(day: date, codes: list[str], fetch_quotes, fetch_minutes, db: Path = MINUTES_DB,
                workers: int = 8) -> dict:
    """当天最高涨幅 ≥ 9% 的票逐只拉分时入库；已经存过的跳过。返回 {"touched": n, "saved": n, "failed": [...]}。"""
    quotes = fetch_quotes(codes)
    touched = [q for q in quotes.values()
               if q.pre_close > 0 and q.ts.date() == day and (q.high / q.pre_close - 1) * 100 >= TOUCH_PCT]
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    done = {r[0] for r in conn.execute("SELECT code FROM minute_days WHERE date=?", (day.isoformat(),))}
    todo = [q for q in touched if q.code not in done]

    def one(q):
        try:
            return q, fetch_minutes(q.code)
        except Exception:                                     # noqa: BLE001
            return q, None
    saved, failed = 0, []
    with ThreadPoolExecutor(workers) as ex:
        for q, bars in ex.map(one, todo):
            if not bars:
                failed.append(q.code)
                continue
            conn.executemany("INSERT OR REPLACE INTO minute_bars VALUES (?,?,?,?,?)",
                             [(day.isoformat(), q.code, hhmm, price, vol) for hhmm, price, vol in bars])
            conn.execute("INSERT OR REPLACE INTO minute_days VALUES (?,?,?,?,?,?,?,?)",
                         (day.isoformat(), q.code, q.name, q.pre_close, q.limit_up, q.high, q.price, len(bars)))
            saved += 1
    conn.commit()
    conn.close()
    return {"touched": len(touched), "saved": saved, "failed": failed}


def archive_today(day: date | None = None) -> dict:
    from jevq.data import tencent
    day = day or date.today()
    return archive_day(day, universe(), tencent.fetch_quotes, tencent.fetch_minutes)
