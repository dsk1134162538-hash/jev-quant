"""高标竞价接力的规则（dj 2026-11-02）：选股看昨天连板数 + 今天竞价开盘幅度；卖出在下一交易日收盘前。"""
from datetime import date, datetime, time

from jevq import config
from jevq.models import Position, Quote
from jevq.rules.sell import SellDecision

MAIN_BOARD = ("60", "00")


def relay_candidates(quotes: dict[str, Quote], streaks: dict[str, int], today: date) -> list[tuple[Quote, int]]:
    """昨天 ≥RELAY_MIN_STREAK 连板、主板、今天的行情、竞价开盘涨幅在 RELAY_GAP_PCT 内（开在涨停的买不到，本来就不在区间）。
    连板数高的优先，同连板开得低的优先（离 6% 越远越没透支）。不截断：主账户排雷后再取前几只。"""
    lo, hi = config.RELAY_GAP_PCT
    out = []
    for code, s in streaks.items():
        q = quotes.get(code)
        if s < config.RELAY_MIN_STREAK or q is None or not code.startswith(MAIN_BOARD) or q.ts.date() != today:
            continue
        if q.open > 0 and lo <= q.open_pct < hi:
            out.append((q, s))
    out.sort(key=lambda x: (-x[1], x[0].open_pct))
    return out


def decide_relay_sell(pos: Position, q: Quote, now: datetime, calendar: list[date] | None,
                      sell_at: tuple[int, int] | None = None) -> SellDecision:
    """买入后的下一个交易日 RELAY_SELL_AT 之后卖；那天没卖成（没跑、跌停卖不掉）就之后任何一分钟卖。"""
    today = now.date()
    if today <= pos.buy_date:
        return SellDecision(False)
    later = [d for d in (calendar or []) if d > pos.buy_date]
    due = later[0] if later else today
    if today > due:
        return SellDecision(True, f"接力超期卖（{q.pct:+.2f}%）")
    if now.time() >= time(*(sell_at or config.RELAY_SELL_AT)):
        return SellDecision(True, f"接力次日收盘卖（{q.pct:+.2f}%）")
    return SellDecision(False)
