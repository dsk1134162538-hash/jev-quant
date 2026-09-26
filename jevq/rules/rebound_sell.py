"""超跌反弹的卖出规则（买入次日起，纯代码，不依赖 Jev）。按顺序，先满足先卖：

1. 持仓天数超过上限（中间有交易日没跑）→ 隔夜超期，开盘即卖
2. 持仓盈亏（相对买入价）≤ REBOUND_STOP（-2%）→ 止损
3. 开盘 ≤ REBOUND_LOW_OPEN（相对昨收 -1.5%），且从当天最低拉起 ≥ REBOUND_LOW_OPEN_BOUNCE（0.8 个点，
   相对昨收）→ 低开反抽，必须卖（当天最低不高于开盘，「开盘 -1.5% 拉到 -0.7%」也包含在内）
4. 当天最高相对买入价 ≥ REBOUND_TRAIL_START（+1.5%）后启动跟踪止盈：
   - 跌回成本价（持仓盈亏 ≤ 0）→ 回到成本，不管买卖力量都卖（保底）
   - 从当天最高回落 ≥ REBOUND_TRAIL_PULLBACK（0.8 个点，相对买入价）→ 跟踪止盈；
     但最近几分钟主动买入大于主动卖出（buy_pressure=True）时先留着
5. 最多拿 REBOUND_MAX_HOLD_DAYS（2）个交易日，最后一天 14:55 → 到期清仓
交易日历不可用（hold_days=None）时保守处理：当天 14:55 清仓。
"""
from datetime import datetime

from jevq.config import (EOD_SELL, MARKET_OPEN, REBOUND_LOW_OPEN, REBOUND_LOW_OPEN_BOUNCE, REBOUND_MAX_HOLD_DAYS,
                         REBOUND_STOP, REBOUND_TRAIL_PULLBACK, REBOUND_TRAIL_START)
from jevq.models import Position, Quote
from jevq.rules.sell import SellDecision

HOLD = SellDecision(False)


def decide_rebound_sell(pos: Position, q: Quote, now: datetime, hold_days: int | None,
                        buy_pressure: bool = False) -> SellDecision:
    if pos.buy_date >= now.date() or now.time() < MARKET_OPEN:
        return HOLD
    if hold_days is not None and hold_days > REBOUND_MAX_HOLD_DAYS:
        return SellDecision(True, "隔夜超期")
    pnl = round((q.price / pos.buy_price - 1) * 100, 2)
    if pnl <= REBOUND_STOP:
        return SellDecision(True, "止损")
    if q.open_pct <= REBOUND_LOW_OPEN and round(q.pct - q.low_pct, 2) >= REBOUND_LOW_OPEN_BOUNCE:
        return SellDecision(True, "低开反抽")
    high_pnl = round((q.high / pos.buy_price - 1) * 100, 2)
    if high_pnl >= REBOUND_TRAIL_START:
        if pnl <= 0:
            return SellDecision(True, "回到成本")
        if round(high_pnl - pnl, 2) >= REBOUND_TRAIL_PULLBACK and not buy_pressure:
            return SellDecision(True, "跟踪止盈")
    if now.time() >= EOD_SELL and (hold_days is None or hold_days >= REBOUND_MAX_HOLD_DAYS):
        return SellDecision(True, "到期清仓")
    return HOLD
