"""扫首板卖出（买入次日起，相对昨收；纯代码）。2026-10-09 用户按 162 只首板的次日数据定：
开盘 -3%~0 的收盘平均比开盘再低 1.24 点、0~+3% 的低 1.32 点 → 竞价不及预期，开盘直接卖；
低开 ≤-3% 的平均从开盘反抽 +1.75 点 → 不在开盘止损，等反抽。先满足先卖：
1. 中间有交易日没跑（买入日早于上一交易日）且没封板 → 隔夜超期
2. 封死涨停 → 拿着
3. 开盘 ≤ LOW_OPEN（-3%）：跌到 HARD_STOP（-6%）止损；从开盘拉起 LOW_OPEN_BOUNCE（1.5 点）卖；
   LOW_OPEN_DEADLINE（10:00）还没反抽也卖
4. 开盘在 (-3%, +3%]：竞价不及预期，卖
5. 高开 >3%：跌到 STOP（-3%）止损；摸过涨停又打开 → 炸板；从当天最高回落 PULLBACK（2 点）→ 冲高回落；
   14:55 尾盘清仓
"""
from datetime import date, datetime, time

from jevq.config import EOD_SELL, MARKET_OPEN
from jevq.models import Position, Quote
from jevq.rules.sell import SellDecision

HOLD = SellDecision(False)
STOP = -3.0
PULLBACK = 2.0
LOW_OPEN = -3.0
WEAK_OPEN_HIGH = 3.0
HARD_STOP = -6.0
LOW_OPEN_BOUNCE = 1.5
LOW_OPEN_DEADLINE = time(10, 0)


def decide_board_sell(pos: Position, q: Quote, now: datetime, prev_trade_day: date | None = None) -> SellDecision:
    if pos.buy_date >= now.date() or now.time() < MARKET_OPEN:
        return HOLD
    sealed = q.price >= q.limit_up
    if prev_trade_day is not None and pos.buy_date < prev_trade_day and not sealed:
        return SellDecision(True, "隔夜超期")
    if sealed:
        return HOLD
    op = q.open_pct
    if op <= LOW_OPEN:
        if q.pct <= HARD_STOP:
            return SellDecision(True, "止损")
        if round(q.pct - op, 2) >= LOW_OPEN_BOUNCE:
            return SellDecision(True, "低开反抽")
        if now.time() >= LOW_OPEN_DEADLINE:
            return SellDecision(True, "低开到点")
        return HOLD
    if op <= WEAK_OPEN_HIGH:
        return SellDecision(True, "竞价不及预期")
    if q.pct <= STOP:
        return SellDecision(True, "止损")
    high_pct = round((q.high / q.pre_close - 1) * 100, 2)
    if q.high >= q.limit_up:
        return SellDecision(True, "炸板")
    if high_pct > 0 and round(high_pct - q.pct, 2) >= PULLBACK:
        return SellDecision(True, "冲高回落")
    if now.time() >= EOD_SELL:
        return SellDecision(True, "尾盘清仓")
    return HOLD
