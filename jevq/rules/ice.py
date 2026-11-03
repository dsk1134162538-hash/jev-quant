"""冰点修复的规则（dj 2026-11-03，知识库）：连续两个冰点日，第二个冰点日的尾盘买一篮子、持有 ICE_HOLD_DAYS 个交易日。

冰点日 = 主板跌停 ≥ICE_DT_MIN 家且炸板率 ≥ICE_BREAK_RATE%，和 research/kb_events.py 回测同口径：
炸板率 = (摸到涨停价的 − 收在涨停的) ÷ 摸到涨停价的。这里用 14:50 的实时主板行情近似「收盘」的统计。
"""
import hashlib
from datetime import date, datetime, time

from jevq import config
from jevq.models import Position, Quote
from jevq.rules.sell import SellDecision

MAIN_BOARD = ("60", "00")


def market_state(quotes: dict[str, Quote], today: date) -> dict | None:
    """今天行情的 {dt: 跌停家数, zt: 涨停家数, touch: 摸过涨停家数, rate: 炸板率%}；没有今天的行情返回 None。"""
    fresh = [q for q in quotes.values() if q.ts.date() == today and q.pre_close > 0 and not q.suspended]
    if not fresh:
        return None
    dt = sum(1 for q in fresh if q.limit_down > 0 and q.price <= q.limit_down)
    zt = sum(1 for q in fresh if q.limit_up > 0 and q.price >= q.limit_up)
    touch = sum(1 for q in fresh if q.limit_up > 0 and q.high >= q.limit_up)
    return {"dt": dt, "zt": zt, "touch": touch, "rate": round((touch - zt) / touch * 100, 1) if touch else 0.0}


def is_ice(state: dict | None) -> bool:
    return bool(state) and state["dt"] >= config.ICE_DT_MIN and state["touch"] > 0 and state["rate"] >= config.ICE_BREAK_RATE


def ice_basket(quotes: dict[str, Quote], today: date, n: int) -> list[Quote]:
    """一篮子：主板、今天行情、股价 ≥ICE_MIN_PRICE、没在涨停/跌停（买不到或不想接）、非 ST。
    按「日期+代码」的哈希排序取前 n 只：同一天同一池永远是同一篮子，也不偏向任何一类票。"""
    pool = [q for q in quotes.values()
            if q.code.startswith(MAIN_BOARD) and q.ts.date() == today and not q.suspended and not q.is_st
            and q.price >= config.ICE_MIN_PRICE and q.price < q.limit_up and q.price > q.limit_down]
    pool.sort(key=lambda q: hashlib.sha1(f"{today}{q.code}".encode()).hexdigest())
    return pool[:n]


def decide_ice_sell(pos: Position, q: Quote, now: datetime, calendar: list[date] | None) -> SellDecision:
    """买入后的第 ICE_HOLD_DAYS 个交易日 ICE_SELL_AT 之后卖；那天没卖成（没跑、跌停）就之后任何一分钟卖。"""
    today = now.date()
    if today <= pos.buy_date:
        return SellDecision(False)
    later = [d for d in (calendar or []) if d > pos.buy_date]
    due = later[config.ICE_HOLD_DAYS - 1] if len(later) >= config.ICE_HOLD_DAYS else None
    if due is None:
        return SellDecision(False)                    # 日历不够长：不猜，等日历补上
    if today > due:
        return SellDecision(True, f"冰点修复超期卖（{q.pct:+.2f}%）")
    if today == due and now.time() >= time(*config.ICE_SELL_AT):
        return SellDecision(True, f"冰点修复持满 {config.ICE_HOLD_DAYS} 天卖（{q.pct:+.2f}%）")
    return SellDecision(False)
