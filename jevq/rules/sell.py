from dataclasses import dataclass
from datetime import datetime

from jevq.config import (EOD_SELL, LOW_OPEN, MARKET_OPEN, REBOUND, STOP_LOSS,
                         TAKE_PROFIT, ZERO_LINE)
from jevq.models import Position, Quote


@dataclass(frozen=True)
class SellDecision:
    sell: bool
    reason: str = ""


HOLD = SellDecision(False)


def decide_sell(pos: Position, q: Quote, now: datetime) -> SellDecision:
    if pos.buy_date >= now.date() or now.time() < MARKET_OPEN:
        return HOLD
    pct, op = q.pct, q.open_pct
    if pct <= STOP_LOSS or op <= STOP_LOSS:
        return SellDecision(True, "止损")
    if op >= TAKE_PROFIT:
        return SellDecision(True, "高开止盈")
    if op > LOW_OPEN:
        if pct >= TAKE_PROFIT:
            return SellDecision(True, "摸到+1%")
    else:
        if pct >= ZERO_LINE:
            return SellDecision(True, "回到0轴")
        if round(pct - q.low_pct, 2) >= REBOUND:
            return SellDecision(True, "反弹")
    if now.time() >= EOD_SELL:
        return SellDecision(True, "尾盘清仓")
    return HOLD
