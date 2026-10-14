"""低吸（9-23 版规则，2026-10-09 按用户要求重新上线并细化，给情绪轮动在偏弱/正常时用）。纯代码。

买入：当日涨幅在 BUY_ZONE（0 ~ -3%）；近 5 日涨幅在 [LOWBUY_RET5_LOW, LOWBUY_RET5_HIGH]（+5%~+20%，强势股，
不要高位）；今天缩量（盘中折算的量比昨天小）；属于今天的主线题材（同扫首板：强势成员 ≥5 只或文章点名）。
按近 5 日涨幅从高到低。情绪偏弱/冰点/未知或外围偏弱 → 只在尾盘（TAIL_BUY_START ~ BUY_END）买，否则盘中可买。
卖出（买入次日起，先满足先卖）：
1. 中间有交易日没跑（买入日早于上一交易日）→ 隔夜超期，开盘即卖
2. 当前涨幅或开盘涨幅 ≤ LOWBUY_STOP（-3%，相对昨收）→ 止损
3. 低开（开盘 ≤ LOWBUY_LOW_OPEN，-1%）：回到 0 轴（≥ -0.2%）或从当天最低反弹 1.5 个点 → 卖
4. 当天最高相对买入价 ≥ LOWBUY_TRAIL_START（+1%）后：跌回成本价 → 回到成本；从最高回落 ≥ LOWBUY_TRAIL_PULLBACK
   （0.8 个点，相对买入价）→ 跟踪止盈（取代原来的「高开止盈 / 摸到 +1%」，少卖飞）
5. 14:55 还没卖 → 尾盘清仓，不隔第二夜
"""
from collections.abc import Iterable
from datetime import date, datetime

from jevq import config
from jevq.models import Position, Quote
from jevq.rules.buy import WEAK_SENTIMENT, in_buy_zone
from jevq.rules.sell import SellDecision

HOLD = SellDecision(False)


def lowbuy_mode(sentiment: str | None, overseas: str | None) -> str:
    if sentiment is None or overseas is None or sentiment in WEAK_SENTIMENT or overseas == "偏弱":
        return "tail"
    return "intraday"


def rank_lowbuy(quotes: Iterable[Quote], ret5: dict[str, float], shrink=None, mainline=None) -> list[Quote]:
    """shrink(q) -> 连续缩量天数或 None；mainline(code) -> 是否属于主线。传 None = 不看这一条（只有测试这么用）。"""
    pool = [q for q in quotes if q.code in ret5 and in_buy_zone(q)
            and config.LOWBUY_RET5_LOW <= ret5[q.code] <= config.LOWBUY_RET5_HIGH
            and (shrink is None or (shrink(q) or 0) >= config.LOWBUY_MIN_SHRINK_DAYS)
            and (mainline is None or mainline(q.code))]
    return sorted(pool, key=lambda q: ret5[q.code], reverse=True)


def decide_lowbuy_sell(pos: Position, q: Quote, now: datetime, prev_trade_day: date | None = None,
                       pullback: float | None = None) -> SellDecision:
    """pullback：跟踪止盈回落线（个点）；主账户传 LOWBUY_MAIN_TRAIL_PULLBACK，None = LOWBUY_TRAIL_PULLBACK（影子）。"""
    if pos.buy_date >= now.date() or now.time() < config.MARKET_OPEN:
        return HOLD
    if prev_trade_day is not None and pos.buy_date < prev_trade_day:
        return SellDecision(True, "隔夜超期")
    pct, op = q.pct, q.open_pct
    if pct <= config.LOWBUY_STOP or op <= config.LOWBUY_STOP:
        return SellDecision(True, "止损")
    if op <= config.LOWBUY_LOW_OPEN:
        if pct >= config.LOWBUY_ZERO_LINE:
            return SellDecision(True, "回到0轴")
        if round(pct - q.low_pct, 2) >= config.LOWBUY_BOUNCE:
            return SellDecision(True, "反弹")
    pnl = round((q.price / pos.buy_price - 1) * 100, 2)
    high_pnl = round((q.high / pos.buy_price - 1) * 100, 2)
    if high_pnl >= config.LOWBUY_TRAIL_START:
        if pnl <= 0:
            return SellDecision(True, "回到成本")
        if round(high_pnl - pnl, 2) >= (config.LOWBUY_TRAIL_PULLBACK if pullback is None else pullback):
            return SellDecision(True, "跟踪止盈")
    if now.time() >= config.EOD_SELL:
        return SellDecision(True, "尾盘清仓")
    return HOLD
