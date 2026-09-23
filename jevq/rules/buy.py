from collections.abc import Iterable
from datetime import time

from jevq.config import BUY_END, BUY_ZONE_HIGH, BUY_ZONE_LOW, MARKET_OPEN, TAIL_BUY_START
from jevq.models import Quote

WEAK_SENTIMENT = {"冰点", "偏弱"}


def in_buy_zone(q: Quote) -> bool:
    return (not q.suspended and not q.is_st
            and BUY_ZONE_LOW <= q.pct <= BUY_ZONE_HIGH)


def buy_mode(sentiment: str | None, overseas: str | None) -> str:
    if sentiment is None or overseas is None:
        return "tail"
    if sentiment in WEAK_SENTIMENT or overseas == "偏弱":
        return "tail"
    return "intraday"


def buy_window_open(mode: str, t: time) -> bool:
    start = TAIL_BUY_START if mode == "tail" else MARKET_OPEN
    return start <= t < BUY_END


def rank_candidates(quotes: Iterable[Quote], ret5: dict[str, float]) -> list[Quote]:
    pool = [q for q in quotes if q.code in ret5 and in_buy_zone(q)]
    return sorted(pool, key=lambda q: ret5[q.code], reverse=True)
