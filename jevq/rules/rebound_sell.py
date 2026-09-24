"""超跌反弹的卖出规则（买入次日起，纯代码，不依赖 Jev）。全部按「相对买入价」算，不按相对昨收：

1. 持仓盈亏 ≤ REBOUND_STOP（-2%）→ 止损
2. 当天最高价相对买入价 ≥ REBOUND_TRAIL_START（+1.5%）后，从当天最高回落 REBOUND_TRAIL_PULLBACK（0.8 点）→ 跟踪止盈
3. 最多拿 REBOUND_MAX_HOLD_DAYS（2）个交易日：第 2 个交易日 14:55 仍未触发 → 到期清仓；
   错过了（持有天数已超过上限，说明中间有交易日没跑）→ 开盘即卖
交易日历不可用（hold_days=None）时保守处理：当天 14:55 清仓。
"""
from datetime import datetime

from jevq.config import (EOD_SELL, MARKET_OPEN, REBOUND_MAX_HOLD_DAYS, REBOUND_STOP, REBOUND_TRAIL_PULLBACK,
                         REBOUND_TRAIL_START)
from jevq.models import Position, Quote
from jevq.rules.sell import SellDecision

HOLD = SellDecision(False)


def decide_rebound_sell(pos: Position, q: Quote, now: datetime, hold_days: int | None) -> SellDecision:
    if pos.buy_date >= now.date() or now.time() < MARKET_OPEN:
        return HOLD
    if hold_days is not None and hold_days > REBOUND_MAX_HOLD_DAYS:
        return SellDecision(True, "隔夜超期")
    pnl = round((q.price / pos.buy_price - 1) * 100, 2)
    if pnl <= REBOUND_STOP:
        return SellDecision(True, "止损")
    high_pnl = round((q.high / pos.buy_price - 1) * 100, 2)
    if high_pnl >= REBOUND_TRAIL_START and round(high_pnl - pnl, 2) >= REBOUND_TRAIL_PULLBACK:
        return SellDecision(True, "跟踪止盈")
    if now.time() >= EOD_SELL and (hold_days is None or hold_days >= REBOUND_MAX_HOLD_DAYS):
        return SellDecision(True, "到期清仓")
    return HOLD
