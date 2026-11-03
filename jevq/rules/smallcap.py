"""小市值轮动的规则（dj 2026-11-03，research/size_study.py）：60 日均成交额最小的 10%、每 20 个交易日调仓、20 只等权。

用成交额而不是流通市值排名：成交额每天都有、不依赖股本数据的准确性（size_study 里两个口径结论一致）。
"""
import hashlib
from datetime import date, datetime, time

from jevq import config
from jevq.models import Position, Quote
from jevq.rules.sell import SellDecision

MAIN_BOARD = ("60", "00")


def amount60_from_db(conn, codes, today: date) -> dict[str, float]:
    """closes 里 date < today 最近 SC_AMOUNT_DAYS 个交易日的日均成交额（元）= 收盘价 × 成交量(手) × 100；历史不足的丢掉。"""
    codes = set(codes)
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT ?",
                                        (today.isoformat(), config.SC_AMOUNT_DAYS))]
    if not dates or not codes:
        return {}
    marks = ",".join("?" * len(dates))
    total, count = {}, {}
    for code, close, volume in conn.execute(f"SELECT code, close, volume FROM closes WHERE date IN ({marks})", dates):
        if code in codes and close and volume:
            total[code] = total.get(code, 0.0) + close * volume * 100
            count[code] = count.get(code, 0) + 1
    return {c: total[c] / count[c] for c in total if count[c] >= config.SC_MIN_HISTORY}


def pick_smallcap(quotes: dict[str, Quote], amount60: dict[str, float], today: date, n: int) -> list[Quote]:
    """成交额最小的 SC_BOTTOM_PCT 里（按全部有成交额的票算分位），今天能买的票，按「日期+代码」哈希取 n 只：
    同一天同一池永远同一篮子，也不偏向任何一类票。池子不够 n 只就全要。"""
    ranked = sorted(amount60, key=amount60.get)
    pool_codes = set(ranked[:max(1, int(len(ranked) * config.SC_BOTTOM_PCT))])
    pool = [q for c, q in quotes.items()
            if c in pool_codes and c.startswith(MAIN_BOARD) and q.ts.date() == today and not q.suspended and not q.is_st
            and q.price >= config.SC_MIN_PRICE and q.limit_down < q.price < q.limit_up]
    pool.sort(key=lambda q: hashlib.sha1(f"{today}{q.code}".encode()).hexdigest())
    return pool[:n]


def decide_sc_sell(pos: Position, q: Quote, now: datetime, calendar: list[date] | None) -> SellDecision:
    """买入后第 SC_HOLD_DAYS 个交易日 SC_SELL_AT 之后卖；那天没卖成（没跑、跌停）就之后任何一分钟卖。"""
    today = now.date()
    if today <= pos.buy_date:
        return SellDecision(False)
    later = [d for d in (calendar or []) if d > pos.buy_date]
    due = later[config.SC_HOLD_DAYS - 1] if len(later) >= config.SC_HOLD_DAYS else None
    if due is None:
        return SellDecision(False)
    if today > due:
        return SellDecision(True, f"小市值轮动超期卖（{q.pct:+.2f}%）")
    if today == due and now.time() >= time(*config.SC_SELL_AT):
        return SellDecision(True, f"小市值轮动持满 {config.SC_HOLD_DAYS} 天调仓（{q.pct:+.2f}%）")
    return SellDecision(False)
