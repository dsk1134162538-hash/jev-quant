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


def prev_closes_from_db(conn, codes: Iterable[str], today: date, prev_day: date,
                        prev_prev_day: date | None = None) -> dict[str, float | None]:
    """首板判断用的前日收（上一交易日的再前一个交易日的收盘价），code → 收盘价，缺的给 None。

    昨收不从库里取（直接用实时行情的 pre_close，永远是对的），库里只取前日收。
    对齐校验：库里 date < today 的最新日期必须就是 prev_day（昨天收盘价存上了），否则说明
    中间漏了一天，第二新的日期就不是前日，返回 {}，由调用方停掉扫首板买入。
    给了 prev_prev_day 时第二新的日期也必须等于它，否则前日收全给 None（没有首板，宁可不买）。
    """
    codes = list(codes)
    if not codes:
        return {}
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT 2",
        (today.isoformat(),)).fetchall()]
    if not dates or dates[0] != prev_day.isoformat():
        return {}
    second = dates[1] if len(dates) > 1 else None
    if prev_prev_day is not None and second != prev_prev_day.isoformat():
        second = None
    if second is None:
        return {c: None for c in codes}
    placeholders = ",".join("?" * len(codes))
    rows = dict(conn.execute(
        f"SELECT code, close FROM closes WHERE date = ? AND code IN ({placeholders})",
        (second, *codes)).fetchall())
    return {c: rows.get(c) for c in codes}


def recent_limit_up_codes(conn, codes: Iterable[str], today: date, days: int = 10) -> set[str]:
    """近 days 个交易日（不含 today）里涨停过的代码集合，扫首板用来排除「不是首板」的龙头。

    取 closes 表 date < today 的最近 days + 1 个不同日期（升序后按相邻两天比较）：
    若 close_d >= round(close_{d-1} * 1.1, 2)（主板 10%），该代码这天算涨停过。
    数据不足 days + 1 个日期时，用已有日期尽力判断（不足 2 个日期直接返回空集）。
    """
    codes = list(codes)
    if not codes:
        return set()
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT ?",
        (today.isoformat(), days + 1)).fetchall()]
    if len(dates) < 2:
        return set()
    dates.sort()    # 升序，方便按相邻两天比较
    date_placeholders = ",".join("?" * len(dates))
    code_placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, date, close FROM closes WHERE date IN ({date_placeholders}) "
        f"AND code IN ({code_placeholders})", (*dates, *codes)).fetchall()
    by_code: dict[str, dict[str, float]] = {}
    for code, d, close in rows:
        by_code.setdefault(code, {})[d] = close
    out: set[str] = set()
    for code, vals in by_code.items():
        for i in range(1, len(dates)):
            prev, cur = vals.get(dates[i - 1]), vals.get(dates[i])
            if prev and cur and cur >= round(prev * 1.1, 2):
                out.add(code)
                break
    return out


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
