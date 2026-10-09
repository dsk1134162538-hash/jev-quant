"""低吸（9-23 版规则，2026-10-09 按用户要求重新上线，给情绪轮动在偏弱/正常时用）。纯代码。

买入：当日涨幅在 BUY_ZONE（0 ~ -3%）、近 5 日涨幅从高到低（强势股回调优先）；
情绪偏弱/冰点/未知或外围偏弱 → 只在尾盘（TAIL_BUY_START ~ BUY_END）买，否则盘中可买（原设计）。
卖出（买入次日起，相对昨收，先满足先卖）：
1. 中间有交易日没跑（买入日早于上一交易日）→ 隔夜超期，开盘即卖
2. 当前涨幅或开盘涨幅 ≤ LOWBUY_STOP（-3%）→ 止损
3. 开盘 ≥ LOWBUY_TAKE_PROFIT（+1%）→ 高开止盈
4. 平开（开盘 > LOWBUY_LOW_OPEN，-1%）：盘中摸到 +1% → 卖
5. 低开（开盘 ≤ -1%）：回到 0 轴（≥ -0.2%）或从当天最低反弹 1.5 个点 → 卖
6. 14:55 还没卖 → 尾盘清仓，不隔第二夜
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


def rank_lowbuy(quotes: Iterable[Quote], ret5: dict[str, float]) -> list[Quote]:
    pool = [q for q in quotes if q.code in ret5 and in_buy_zone(q)]
    return sorted(pool, key=lambda q: ret5[q.code], reverse=True)


def decide_lowbuy_sell(pos: Position, q: Quote, now: datetime, prev_trade_day: date | None = None) -> SellDecision:
    if pos.buy_date >= now.date() or now.time() < config.MARKET_OPEN:
        return HOLD
    if prev_trade_day is not None and pos.buy_date < prev_trade_day:
        return SellDecision(True, "隔夜超期")
    pct, op = q.pct, q.open_pct
    if pct <= config.LOWBUY_STOP or op <= config.LOWBUY_STOP:
        return SellDecision(True, "止损")
    if op >= config.LOWBUY_TAKE_PROFIT:
        return SellDecision(True, "高开止盈")
    if op > config.LOWBUY_LOW_OPEN:
        if pct >= config.LOWBUY_TAKE_PROFIT:
            return SellDecision(True, "摸到+1%")
    else:
        if pct >= config.LOWBUY_ZERO_LINE:
            return SellDecision(True, "回到0轴")
        if round(pct - q.low_pct, 2) >= config.LOWBUY_BOUNCE:
            return SellDecision(True, "反弹")
    if now.time() >= config.EOD_SELL:
        return SellDecision(True, "尾盘清仓")
    return HOLD
