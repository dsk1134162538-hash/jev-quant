from datetime import date, datetime

from jevq.config import EOD_SELL, MARKET_OPEN
from jevq.models import Position, Quote
from jevq.rules.sell import SellDecision

HOLD = SellDecision(False)
STOP = -3.0
PULLBACK = 2.0


def decide_board_sell(pos: Position, q: Quote, now: datetime, prev_trade_day: date | None = None) -> SellDecision:
    if pos.buy_date >= now.date() or now.time() < MARKET_OPEN:
        return HOLD
    sealed = q.price >= q.limit_up
    if prev_trade_day is not None and pos.buy_date < prev_trade_day and not sealed:
        return SellDecision(True, "隔夜超期")
    if q.pct <= STOP:
        return SellDecision(True, "止损")
    if sealed:
        return HOLD
    high_pct = round((q.high / q.pre_close - 1) * 100, 2)
    if q.high >= q.limit_up:
        return SellDecision(True, "炸板")
    if high_pct > 0 and round(high_pct - q.pct, 2) >= PULLBACK:
        return SellDecision(True, "冲高回落")
    if now.time() >= EOD_SELL:
        return SellDecision(True, "尾盘清仓")
    return HOLD
