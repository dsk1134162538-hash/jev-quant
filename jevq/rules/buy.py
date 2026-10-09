from collections.abc import Callable, Iterable
from datetime import time

from jevq import config
from jevq.config import (BUY_END, BUY_ZONE_HIGH, BUY_ZONE_LOW, MARKET_OPEN, REBOUND_RET5_HIGH,
                         REBOUND_RET5_LOW, TAIL_BUY_START)
from jevq.models import Quote

WEAK_SENTIMENT = {"冰点", "偏弱"}


def in_buy_zone(q: Quote) -> bool:
    return (not q.suspended and not q.is_st
            and BUY_ZONE_LOW <= q.pct <= BUY_ZONE_HIGH)


def buy_window_open(mode: str, t: time) -> bool:
    """mode "none"（情绪偏弱/冰点/未知）不买；"tail" 只是旧决策记录里的值，不再产生。"""
    if mode == "none":
        return False
    start = TAIL_BUY_START if mode == "tail" else MARKET_OPEN
    return start <= t < BUY_END


def rebound_mode(sentiment: str | None) -> str:
    """超跌反弹的买入时机只看情绪（不看外围）：正常/火热 → 盘中可买；偏弱/冰点/未知 → 不买
    （用户 2026-10-09 定，原来是等尾盘买）。情绪每 10 分钟刷新，盘中转好就恢复买。"""
    if sentiment is None or sentiment in WEAK_SENTIMENT:
        return "none"
    return "intraday"


def rank_rebound(quotes: Iterable[Quote], ret5: dict[str, float], excluded: set[str],
                 shrink_days: Callable[[Quote], int | None] | None = None) -> list[Quote]:
    """超跌反弹候选：当日涨幅在买入区间（in_buy_zone）、近 5 日涨幅在 [REBOUND_RET5_LOW, REBOUND_RET5_HIGH]、
    近两个月没连板过（excluded）；5 日跌得多的优先。

    给了 shrink_days（Quote → 连续缩量天数，含今天，None = 量能数据不全）时再加成交量条件：至少连续缩量
    REBOUND_MIN_SHRINK_DAYS 天才入选（不知道的不入选），连续缩量 ≥ REBOUND_PRIORITY_SHRINK_DAYS 天的排在前面，
    同档内仍是 5 日跌得多的优先。REBOUND_MIN_SHRINK_DAYS 设为 0 表示不看成交量。"""
    pool = [q for q in quotes if q.code in ret5 and q.code not in excluded and in_buy_zone(q)
            and REBOUND_RET5_LOW <= ret5[q.code] <= REBOUND_RET5_HIGH]
    if shrink_days is None or config.REBOUND_MIN_SHRINK_DAYS <= 0:
        return sorted(pool, key=lambda q: ret5[q.code])
    days = {q.code: shrink_days(q) for q in pool}
    pool = [q for q in pool if (days[q.code] or 0) >= config.REBOUND_MIN_SHRINK_DAYS]
    return sorted(pool, key=lambda q: (days[q.code] < config.REBOUND_PRIORITY_SHRINK_DAYS, ret5[q.code]))
