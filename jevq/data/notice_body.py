"""公告正文：东方财富正文接口 + 本地缓存 + 早盘预抓（只抓标题像雷的）。"""
import re
import sqlite3
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests

from jevq.config import BODY_CHARS, NOTICE_LOOKBACK_DAYS, PREFETCH_CAP
from jevq.judge.notice_risk import keyword_category

BODY_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
_UA = {"User-Agent": "Mozilla/5.0"}


def fetch_body(art_code: str, session=None) -> str | None:
    try:
        r = (session or requests).get(BODY_URL, params={"art_code": art_code, "client_source": "web",
                                                        "page_index": 1}, timeout=8, headers=_UA)
        data = (r.json() or {}).get("data") or {}
        text = re.sub(r"\s+", " ", data.get("notice_content") or "").strip()
    except Exception:
        return None
    return text[:BODY_CHARS] or None


def get_body(conn: sqlite3.Connection, notice_id: str,
             fetch: Callable[[str], str | None] = fetch_body) -> str | None:
    row = conn.execute("SELECT text FROM notice_bodies WHERE id=?", (notice_id,)).fetchone()
    if row:
        return row[0]
    text = fetch(notice_id)
    if text:
        _save(conn, notice_id, text)
    return text


def _save(conn, notice_id: str, text: str):
    conn.execute("INSERT OR REPLACE INTO notice_bodies(id, text, fetched) VALUES (?, ?, ?)",
                 (notice_id, text, datetime.now().isoformat(timespec="seconds")))
    conn.commit()


def prefetch_bodies(conn: sqlite3.Connection, today: date, codes: Iterable[str],
                    fetch: Callable[[str], str | None] = fetch_body,
                    cap: int = PREFETCH_CAP, workers: int = 8) -> int:
    """股票池内、近 NOTICE_LOOKBACK_DAYS 天、标题命中风险关键词、还没有正文缓存的公告，预抓正文。
    网络请求并发；写库只在调用线程里做（sqlite 连接不跨线程）。"""
    codes = set(codes)
    since = (today - timedelta(days=NOTICE_LOOKBACK_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT n.id, n.code, n.title FROM notices n LEFT JOIN notice_bodies b ON b.id = n.id "
        "WHERE n.date >= ? AND b.id IS NULL ORDER BY n.date DESC, n.id", (since,)).fetchall()
    todo = [nid for nid, code, title in rows if code in codes and keyword_category(title)][:cap]
    if not todo:
        return 0
    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(fetch, todo))
    n = 0
    for nid, text in zip(todo, results):
        if text:
            _save(conn, nid, text)
            n += 1
    return n
