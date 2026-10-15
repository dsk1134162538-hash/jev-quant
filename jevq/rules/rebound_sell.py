"""超跌反弹的卖出规则（买入次日起，纯代码；Jev 只经调用方传进来的 trend 函数参与）。按顺序，先满足先卖：

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

主账户（实盘跟的账户，传 stop_minutes 和 trend）在 2、3 上多两步（用户 2026-10-09 定）：
- 持仓盈亏 ≤ REBOUND_HARD_STOP（-3.5%）→ 硬止损，立即卖，不等确认、不问 Jev
- 止损要现价连续 REBOUND_STOP_CONFIRM（2）分钟在止损线下才算数，防瞬间下探
- 止损确认后、低开反抽触发时问 Jev 今天走向：判成 REBOUND_TREND_HOLD（探底回升/低开高走）先留着，
  下一分钟再看；其他走向或 Jev 没判出来 → 照卖。回到成本、跟踪止盈、到期清仓不问 Jev。
影子账户不传这两个参数，规则和以前完全一样，当对照组。
"""
from collections.abc import Callable
from datetime import datetime

from jevq.config import (EOD_SELL, MARKET_OPEN, REBOUND_HARD_STOP, REBOUND_LOW_OPEN, REBOUND_LOW_OPEN_BOUNCE,
                         REBOUND_MAX_HOLD_DAYS, REBOUND_STOP, REBOUND_STOP_CONFIRM, REBOUND_TRAIL_PULLBACK,
                         REBOUND_TRAIL_START, REBOUND_TREND_HOLD)
from jevq.models import Position, Quote
from jevq.rules.sell import SellDecision

HOLD = SellDecision(False)


def decide_rebound_sell(pos: Position, q: Quote, now: datetime, hold_days: int | None,
                        buy_pressure: bool = False, stop_minutes: int | None = None,
                        trend: Callable[[], str | None] | None = None,
                        pullback: float = REBOUND_TRAIL_PULLBACK, stop: float = REBOUND_STOP,
                        max_hold: int = REBOUND_MAX_HOLD_DAYS, low_open_sell: bool = True) -> SellDecision:
    """stop_minutes：现价连续几分钟（含这一分钟）在止损线下；None = 不要确认、也没有硬止损（旧规则）。
    trend：问 Jev 今天走向，返回 TREND_LEVELS 之一或 None（没判出来）；一次判断里最多调一次。None = 不问，照卖。
    pullback：跟踪止盈的回落线（个点，相对买入价）；主账户传 REBOUND_MAIN_TRAIL_PULLBACK，影子用默认。
    stop / max_hold / low_open_sell：止损线、最多拿几个交易日、低开反抽卖不卖；主账户传 REBOUND_MAIN_*（10-15 起
    -3%、3 天、不卖低开反抽），影子用默认（-2%、2 天、卖）。
    不卖时 reason 说明为什么还拿着（待确认 / 暂缓），调用方据此记录。"""
    if pos.buy_date >= now.date() or now.time() < MARKET_OPEN:
        return HOLD
    if hold_days is not None and hold_days > max_hold:
        return SellDecision(True, "隔夜超期")
    ask = _once(trend)
    waiting = HOLD
    pnl = round((q.price / pos.buy_price - 1) * 100, 2)
    if pnl <= stop:
        if stop_minutes is not None and pnl <= REBOUND_HARD_STOP:
            return SellDecision(True, "硬止损")
        if stop_minutes is None or stop_minutes >= REBOUND_STOP_CONFIRM:
            d = _unless_rising("止损", ask)
            if d.sell:
                return d
            waiting = d
        else:
            waiting = SellDecision(False, f"止损待确认（{stop_minutes}/{REBOUND_STOP_CONFIRM} 分钟）")
    if low_open_sell and q.open_pct <= REBOUND_LOW_OPEN and round(q.pct - q.low_pct, 2) >= REBOUND_LOW_OPEN_BOUNCE:
        d = _unless_rising("低开反抽", ask)
        if d.sell:
            return d
        waiting = waiting if waiting.reason else d
    high_pnl = round((q.high / pos.buy_price - 1) * 100, 2)
    if high_pnl >= REBOUND_TRAIL_START:
        if pnl <= 0:
            return SellDecision(True, "回到成本")
        if round(high_pnl - pnl, 2) >= pullback and not buy_pressure:
            return SellDecision(True, "跟踪止盈")
    if now.time() >= EOD_SELL and (hold_days is None or hold_days >= max_hold):
        return SellDecision(True, "到期清仓")
    return waiting


def _once(trend: Callable[[], str | None] | None) -> Callable[[], str | None] | None:
    if trend is None:
        return None
    memo: list = []

    def ask() -> str | None:
        if not memo:
            memo.append(trend())
        return memo[0]
    return ask


def _unless_rising(reason: str, ask: Callable[[], str | None] | None) -> SellDecision:
    if ask is None:
        return SellDecision(True, reason)
    label = ask()
    if label in REBOUND_TREND_HOLD:
        return SellDecision(False, f"{reason}暂缓（{label}）")
    return SellDecision(True, f"{reason}（{label or 'Jev 没判出来'}）")
