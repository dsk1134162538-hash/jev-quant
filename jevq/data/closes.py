"""本地收盘价库：5 日涨幅从这里算，不再每天拉 K 线（腾讯 K 线接口被 WAF 封了）。"""
import time
from collections.abc import Callable, Iterable
from datetime import date, datetime


def save_closes(conn, day: date, prices: dict[str, float]) -> int:
    """把当天的收盘价（或当日报价）写入 closes 表；价格 <= 0 的跳过。返回写入条数。"""
    rows = [(day.isoformat(), code, price) for code, price in prices.items()
            if price is not None and price > 0]
    if not rows:
        return 0
    conn.executemany("INSERT OR REPLACE INTO closes(date, code, close) VALUES (?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def ret5_from_db(conn, codes: Iterable[str], today: date) -> dict[str, float]:
    """用 closes 表里 date < today 的最近 6 个不同日期算 5 日涨幅。不足 6 个日期返回 {}。"""
    codes = list(codes)
    if not codes:
        return {}
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT 6",
        (today.isoformat(),)).fetchall()]
    if len(dates) < 6:
        return {}
    latest, sixth = dates[0], dates[5]
    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, date, close FROM closes WHERE date IN (?, ?) AND code IN ({placeholders})",
        (latest, sixth, *codes)).fetchall()
    by_code: dict[str, dict[str, float]] = {}
    for code, d, close in rows:
        by_code.setdefault(code, {})[d] = close
    out = {}
    for code, vals in by_code.items():
        if latest in vals and sixth in vals and vals[sixth]:
            out[code] = round((vals[latest] / vals[sixth] - 1) * 100, 2)
    return out


def prev_closes_from_db(conn, codes: Iterable[str], today: date) -> dict[str, tuple[float | None, float | None]]:
    """用 closes 表里 date < today 的最近两个不同日期算（昨收、前日收）。缺数据的日期/代码给 None。"""
    codes = list(codes)
    if not codes:
        return {}
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT 2",
        (today.isoformat(),)).fetchall()]
    if not dates:
        return {c: (None, None) for c in codes}
    latest = dates[0]
    prev = dates[1] if len(dates) > 1 else None
    query_dates = [latest] if prev is None else [latest, prev]
    date_placeholders = ",".join("?" * len(query_dates))
    code_placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, date, close FROM closes WHERE date IN ({date_placeholders}) "
        f"AND code IN ({code_placeholders})", (*query_dates, *codes)).fetchall()
    by_code: dict[str, dict[str, float]] = {}
    for code, d, close in rows:
        by_code.setdefault(code, {})[d] = close
    return {c: (by_code.get(c, {}).get(latest), by_code.get(c, {}).get(prev) if prev else None)
            for c in codes}


def backfill_closes(conn, codes: Iterable[str], today: date,
                     fetch_kline: Callable[[str], list[tuple[str, float]]], pause: float = 0.1,
                     max_consecutive_failures: int = 30, deadline: datetime | None = None,
                     log=print) -> int:
    """给收盘价不足 6 条的 code 逐只串行补数。返回实际写入了新收盘价的 code 数（fetch 成功但一行都没写进去的不算）。"""
    codes = list(codes)
    if not codes:
        return 0
    today_iso = today.isoformat()
    placeholders = ",".join("?" * len(codes))
    counts = dict(conn.execute(
        f"SELECT code, COUNT(*) FROM closes WHERE date < ? AND code IN ({placeholders}) "
        "GROUP BY code", (today_iso, *codes)).fetchall())
    todo = [c for c in codes if counts.get(c, 0) < 6]

    done = 0
    consecutive_failures = 0
    for code in todo:
        if deadline is not None and datetime.now() >= deadline:
            break
        try:
            rows = fetch_kline(code)
        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                log(f"K 线接口连续失败 {consecutive_failures} 次，停止补数（可能被限流）")
                break
            time.sleep(pause)
            continue
        consecutive_failures = 0
        rows = [(d, code, c) for d, c in rows if d < today_iso and c > 0]
        if rows:
            conn.executemany("INSERT OR REPLACE INTO closes(date, code, close) VALUES (?, ?, ?)", rows)
            conn.commit()
            done += 1
        time.sleep(pause)
    return done
