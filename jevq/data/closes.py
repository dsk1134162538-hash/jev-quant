"""本地收盘价库：5 日涨幅从这里算，不再每天拉 K 线（腾讯 K 线接口被 WAF 封了）。"""
import time
from collections.abc import Callable, Iterable
from datetime import date, datetime


UPSERT_SQL = ("INSERT INTO closes(date, code, close, volume, pre_close) VALUES (?, ?, ?, ?, ?) "
              "ON CONFLICT(date, code) DO UPDATE SET close = excluded.close, "
              "volume = COALESCE(excluded.volume, closes.volume), "
              "pre_close = COALESCE(excluded.pre_close, closes.pre_close)")


def _lots(v) -> float | None:
    """成交量（手）；缺失或 <= 0（停牌/没成交）记 NULL，不当成真实的 0 手。"""
    return float(v) if v is not None and v > 0 else None


def save_closes(conn, day: date, prices: dict[str, float],
                volumes: dict[str, float] | None = None, pre_closes: dict[str, float] | None = None) -> int:
    """把当天的收盘价（或当日报价）写入 closes 表；价格 <= 0 的跳过。返回写入条数。

    volumes：code → 当日成交量（手，Quote.volume 的口径）。没给量的代码保留库里已有的量。
    pre_closes：code → 当天行情里的昨收（已按除权除息调整），算真实涨跌幅用；没给的保留库里已有的。"""
    volumes, pre_closes = volumes or {}, pre_closes or {}
    rows = [(day.isoformat(), code, price, _lots(volumes.get(code)),
             (pre_closes.get(code) if (pre_closes.get(code) or 0) > 0 else None))
            for code, price in prices.items() if price is not None and price > 0]
    if not rows:
        return 0
    conn.executemany(UPSERT_SQL, rows)
    conn.commit()
    return len(rows)


def daily_ratios(conn, codes: Iterable[str], dates: list[str]) -> dict[str, list[float | None]]:
    """每只票在 dates（升序）相邻两天之间的「真实」涨跌比（今收 ÷ 昨收）：有存当天行情昨收（已按除权除息调整）就用它，
    没有就用库里上一个交易日的收盘价（10-19 之前的历史只能这样，除权日会被当成下跌——见 backfill_ex_rights）。
    第 k 项是 dates[k] 相对 dates[k-1] 的；缺数据为 None。"""
    codes = set(codes)
    if len(dates) < 2 or not codes:
        return {}
    marks = ",".join("?" * len(dates))
    px: dict[str, dict[str, tuple[float, float | None]]] = {}
    for code, d, close, pre in conn.execute(
            f"SELECT code, date, close, pre_close FROM closes WHERE date IN ({marks}) AND close > 0", dates):
        if code in codes:
            px.setdefault(code, {})[d] = (close, pre)
    out = {}
    for code, s in px.items():
        ratios: list[float | None] = []
        last = None
        for d in dates:
            cur = s.get(d)
            if cur is None:
                ratios.append(None)
                continue
            base = cur[1] if cur[1] else (last if last else None)
            ratios.append(cur[0] / base if base else None)
            last = cur[0]
        out[code] = ratios[1:]
    return out


def ret5_from_db(conn, codes: Iterable[str], today: date) -> dict[str, float]:
    """用 closes 表里 date < today 的最近 6 个不同日期算 5 日涨幅（逐日涨跌比连乘，除权除息日按调整后的昨收算，
    10-19 起）。首尾两天都要有收盘价；不足 6 个日期返回 {}。"""
    codes = list(codes)
    if not codes:
        return {}
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT 6",
        (today.isoformat(),)).fetchall()][::-1]
    if len(dates) < 6:
        return {}
    have = {(c, d) for c, d in conn.execute(
        f"SELECT code, date FROM closes WHERE date IN (?, ?) AND close > 0", (dates[0], dates[-1]))}
    out = {}
    for code, ratios in daily_ratios(conn, codes, dates).items():
        if (code, dates[0]) not in have or (code, dates[-1]) not in have:
            continue
        prod = 1.0
        for r in ratios:
            if r is not None:
                prod *= r
        out[code] = round((prod - 1) * 100, 2)
    return out


def backfill_ex_rights(conn, rows: list[tuple[str, str, float, float]]) -> int:
    """补历史的除权除息：rows = [(代码, 除权除息日, 每 10 股送转股数, 每 10 股派现金)]。
    那天库里有收盘价、还没有 pre_close 的，按「(上一交易日收盘 - 每股派现) ÷ (1 + 每股送转)」补上。返回补了几条。"""
    n = 0
    for code, d, bonus10, cash10 in rows:
        row = conn.execute("SELECT pre_close FROM closes WHERE code=? AND date=?", (code, d)).fetchone()
        if row is None or row[0]:
            continue
        prev = conn.execute("SELECT close FROM closes WHERE code=? AND date<? AND close > 0 ORDER BY date DESC LIMIT 1",
                            (code, d)).fetchone()
        if not prev:
            continue
        pre = (prev[0] - (cash10 or 0) / 10) / (1 + (bonus10 or 0) / 10)
        if pre > 0:
            conn.execute("UPDATE closes SET pre_close=? WHERE code=? AND date=?", (round(pre, 4), code, d))
            n += 1
    conn.commit()
    return n


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
        rows = [(r[0], code, r[1], _lots(r[2]) if len(r) > 2 else None, None)    # 补数的 K 线没有调整后的昨收
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


def limit_streaks(conn, codes: Iterable[str], today: date, max_days: int = 15) -> dict[str, int]:
    """截至 today 之前最后一个交易日的连板数（收盘价 ≥ 昨收×1.1 四舍五入到分）。昨收优先用 closes.pre_close
    （已按除权调整）；pre_close 是空的（补抓的那天）就用库里前一天的收盘价。只返回连板数 ≥1 的代码。
    高标竞价接力用（dj 2026-11-02）。"""
    codes = set(codes)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT ?",
        (today.isoformat(), max_days + 1)).fetchall()]
    if not dates or not codes:
        return {}
    marks = ",".join("?" * len(dates))
    rows: dict[str, dict[str, tuple]] = {}
    for d, code, close, pre in conn.execute(
            f"SELECT date, code, close, pre_close FROM closes WHERE date IN ({marks})", dates):
        if code in codes:
            rows.setdefault(code, {})[d] = (close, pre)
    out = {}
    for code in codes:
        days, n = rows.get(code, {}), 0
        for i, d in enumerate(dates):               # 新 → 旧
            close, pre = days.get(d, (None, None))
            if pre is None and i + 1 < len(dates):
                pre = days.get(dates[i + 1], (None, None))[0]
            if not close or not pre or close < round(pre * 1.1 + 1e-9, 2):
                break
            n += 1
        if n:
            out[code] = n
    return out


def position_stats(conn, codes: Iterable[str], today: date, lookback: int = 60) -> dict[str, dict[str, float]]:
    """这只票在高位还是低位（dj 2026-11-12：冰山冷热一个月翻倍还去买，「正常人思维都不进这只票，太高了」）。

    只用 today 之前的收盘价：rise5 / rise20 = 昨收比 5 / 20 个交易日前的收盘涨了多少（%），
    fromLow60 = 昨收比近 60 个交易日最低收盘高多少（%）。历史不够的项不给；一只都算不出就不在结果里。"""
    out: dict[str, dict[str, float]] = {}
    for code in dict.fromkeys(codes):
        rows = [r[0] for r in conn.execute(
            "SELECT close FROM closes WHERE code = ? AND date < ? AND close > 0 ORDER BY date DESC LIMIT ?",
            (code, today.isoformat(), lookback + 1)).fetchall()]
        if len(rows) < 6:
            continue
        last = rows[0]
        stats = {"rise5": round((last / rows[5] - 1) * 100, 1)}
        if len(rows) > 20:
            stats["rise20"] = round((last / rows[20] - 1) * 100, 1)
        stats["fromLow60"] = round((last / min(rows[:lookback]) - 1) * 100, 1)
        out[code] = stats
    return out
