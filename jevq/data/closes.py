"""本地收盘价库：5 日涨幅从这里算，不再每天拉 K 线（腾讯 K 线接口被 WAF 封了）。"""
import time
from collections.abc import Callable, Iterable
from datetime import date, datetime


UPSERT_SQL = ("INSERT INTO closes(date, code, close, volume) VALUES (?, ?, ?, ?) "
              "ON CONFLICT(date, code) DO UPDATE SET close = excluded.close, "
              "volume = COALESCE(excluded.volume, closes.volume)")


def _lots(v) -> float | None:
    """成交量（手）；缺失或 <= 0（停牌/没成交）记 NULL，不当成真实的 0 手。"""
    return float(v) if v is not None and v > 0 else None


def save_closes(conn, day: date, prices: dict[str, float],
                volumes: dict[str, float] | None = None) -> int:
    """把当天的收盘价（或当日报价）写入 closes 表；价格 <= 0 的跳过。返回写入条数。

    volumes：code → 当日成交量（手，Quote.volume 的口径）。没给量的代码保留库里已有的量。"""
    volumes = volumes or {}
    rows = [(day.isoformat(), code, price, _lots(volumes.get(code))) for code, price in prices.items()
            if price is not None and price > 0]
    if not rows:
        return 0
    conn.executemany(UPSERT_SQL, rows)
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


GAP_WINDOW = 21          # 检查缺口的交易日数：与 history_features 用到的最近 21 个日期一致
GAP_RATIO = 0.9          # 某天行数不到窗口内最多那天的 90% 算不完整（停牌的票本来就没有行，占比很小）


def incomplete_trade_days(conn, trade_days: Iterable[date], today: date,
                          window: int = GAP_WINDOW, ratio: float = GAP_RATIO) -> list[str]:
    """最近 window 个交易日（date < today）里，库里整天没有、或行数明显偏少的日期（升序）。

    后台某天没跑（关机/断网）时那天整天没有收盘价：history_features 按库里实际存在的日期对齐，
    会把缺口两边当成相邻交易日，近 N 日涨幅、连涨连跌、阶梯缩量都悄悄算错，所以要按交易日历找出来补。"""
    expected = [d.isoformat() for d in sorted(trade_days) if d < today][-window:]
    if not expected:
        return []
    placeholders = ",".join("?" * len(expected))
    counts = dict(conn.execute(
        f"SELECT date, COUNT(*) FROM closes WHERE date IN ({placeholders}) GROUP BY date", expected).fetchall())
    full = max(counts.values(), default=0)
    return [d for d in expected if full == 0 or counts.get(d, 0) < full * ratio]


def backfill_closes(conn, codes: Iterable[str], today: date,
                     fetch_kline: Callable[[str], list[tuple]], pause: float = 0.1,
                     max_consecutive_failures: int = 30, deadline: datetime | None = None,
                     log=print, fill_volume: bool = False, volume_days: int = 10,
                     trade_days: Iterable[date] | None = None) -> int:
    """给收盘价不足 6 条的 code 逐只串行补数。返回实际写入了新收盘价的 code 数（fetch 成功但一行都没写进去的不算）。

    给了 trade_days（交易日历）时，还会按 incomplete_trade_days 找出最近 21 个交易日里整天缺失/明显不完整的日期，
    在这些日期上没有行的 code 也重新拉一遍（补上后台没跑的那天）。

    fetch_kline(code) 返回 (date_str, close) 或 (date_str, close, volume_手) 的列表；
    带量的行同时写量，不带量的只更新收盘价、保留库里已有的量。
    fill_volume=True 时，最近 volume_days 个日期（date < today）里有量为 NULL 的 code 也重新拉一遍，
    用来给历史行补上成交量（fetch_kline 需带量，例如 akdata.fetch_daily_sina）。"""
    codes = list(codes)
    if not codes:
        return 0
    today_iso = today.isoformat()
    placeholders = ",".join("?" * len(codes))
    counts = dict(conn.execute(
        f"SELECT code, COUNT(*) FROM closes WHERE date < ? AND code IN ({placeholders}) "
        "GROUP BY code", (today_iso, *codes)).fetchall())
    need_volume: set[str] = set()
    if fill_volume:
        recent = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT ?",
            (today_iso, volume_days)).fetchall()]
        if recent:
            date_placeholders = ",".join("?" * len(recent))
            need_volume = {r[0] for r in conn.execute(
                f"SELECT DISTINCT code FROM closes WHERE volume IS NULL AND date IN ({date_placeholders}) "
                f"AND code IN ({placeholders})", (*recent, *codes)).fetchall()}
    need_gap: set[str] = set()
    if trade_days is not None:
        gaps = incomplete_trade_days(conn, trade_days, today)
        if gaps:
            gap_placeholders = ",".join("?" * len(gaps))
            have = dict(conn.execute(
                f"SELECT code, COUNT(*) FROM closes WHERE date IN ({gap_placeholders}) "
                f"AND code IN ({placeholders}) GROUP BY code", (*gaps, *codes)).fetchall())
            need_gap = {c for c in codes if have.get(c, 0) < len(gaps)}
            log(f"收盘价缺口 {len(gaps)} 天（{gaps[0]}～{gaps[-1]}），需补 {len(need_gap)} 只")
    todo = [c for c in codes if counts.get(c, 0) < 6 or c in need_volume or c in need_gap]

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
        rows = [(r[0], code, r[1], _lots(r[2]) if len(r) > 2 else None)
                for r in rows if r[0] < today_iso and r[1] > 0]
        if rows:
            conn.executemany(UPSERT_SQL, rows)
            conn.commit()
            done += 1
        time.sleep(pause)
    return done


def consecutive_board_codes(conn, codes: Iterable[str], today: date, days: int = 40,
                            min_boards: int = 2) -> tuple[set[str], int]:
    """近 days 个交易日（不含 today）里出现过 ≥ min_boards 连板的代码集合，超跌反弹用来排除炒作过的票。

    取 closes 表 date < today 的最近 days + 1 个不同日期，按全局相邻日期对判断涨停
    （close_d >= round(close_{d-1} * 1.1, 2)，主板 10%）；缺数据的日期对是断点，不跨缺口把两段涨停焊成连板。
    返回 (代码集合, 实际用到的日期数)；历史不足时用已有日期尽力判断，调用方据日期数写日志。
    """
    codes = list(codes)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT ?",
        (today.isoformat(), days + 1)).fetchall()]
    if len(dates) < 2 or not codes:
        return set(), len(dates)
    dates.sort()
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
        streak = 0
        for i in range(1, len(dates)):
            prev, cur = vals.get(dates[i - 1]), vals.get(dates[i])
            streak = streak + 1 if (prev and cur and cur >= round(prev * 1.1, 2)) else 0
            if streak >= min_boards:
                out.add(code)
                break
    return out, len(dates)
