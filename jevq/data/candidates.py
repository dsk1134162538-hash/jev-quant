"""面板「候选」页的数据：后台每分钟把扫首板观察池、超跌反弹入选名单写成一份快照（存 day_cache），
面板只读。快照按日期存，休市/盘后读最近一天的，不会清空。"""
import json
import sqlite3
from datetime import date, datetime

KIND = "candidates"
KEY = "snapshot"


def save(conn: sqlite3.Connection, day: date, now: datetime, board: list[dict], rebound: dict | None) -> None:
    payload = {"updated": now.strftime("%H:%M"), "board": board, "rebound": rebound}
    conn.execute("INSERT OR REPLACE INTO day_cache VALUES (?, ?, ?, ?)",
                 (day.isoformat(), KIND, KEY, json.dumps(payload, ensure_ascii=False)))
    conn.commit()


def latest(conn: sqlite3.Connection) -> dict:
    """最近一天的快照（带 date）；还没有过 → 空结构。"""
    row = conn.execute("SELECT date, value FROM day_cache WHERE kind=? AND key=? ORDER BY date DESC LIMIT 1",
                       (KIND, KEY)).fetchone()
    if not row:
        return {"date": None, "updated": None, "board": [], "rebound": None}
    try:
        data = json.loads(row[1])
    except ValueError:
        return {"date": None, "updated": None, "board": [], "rebound": None}
    return {"date": row[0], "updated": data.get("updated"), "board": data.get("board") or [],
            "rebound": data.get("rebound")}
