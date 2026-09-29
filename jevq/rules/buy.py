from collections.abc import Iterable
from datetime import time

from jevq.config import (BUY_END, BUY_ZONE_HIGH, BUY_ZONE_LOW, MARKET_OPEN, REBOUND_RET5_HIGH,
                         REBOUND_RET5_LOW, TAIL_BUY_START)
from jevq.models import Quote

WEAK_SENTIMENT = {"冰点", "偏弱"}


def in_buy_zone(q: Quote) -> bool:
    return (not q.suspended and not q.is_st
            and BUY_ZONE_LOW <= q.pct <= BUY_ZONE_HIGH)


def buy_window_open(mode: str, t: time) -> bool:
    start = TAIL_BUY_START if mode == "tail" else MARKET_OPEN
    return start <= t < BUY_END


def rebound_mode(sentiment: str | None) -> str:
    """超跌反弹的买入时机只看情绪（不看外围）：正常/火热 → 盘中；偏弱/冰点/未知 → 尾盘。"""
    if sentiment is None or sentiment in WEAK_SENTIMENT:
        return "tail"
    return "intraday"


def rank_rebound(quotes: Iterable[Quote], ret5: dict[str, float], excluded: set[str]) -> list[Quote]:
    """超跌反弹候选：当日涨幅在买入区间（in_buy_zone）、近 5 日涨幅在 [REBOUND_RET5_LOW, REBOUND_RET5_HIGH]、
    近两个月没连板过（excluded）；5 日跌得多的优先。"""
    pool = [q for q in quotes if q.code in ret5 and q.code not in excluded and in_buy_zone(q)
            and REBOUND_RET5_LOW <= ret5[q.code] <= REBOUND_RET5_HIGH]
    return sorted(pool, key=lambda q: ret5[q.code])
