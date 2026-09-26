"""从 Quote 派生的盘口指标。纯函数，数据缺失一律返回 None，供 Task 5 的取值函数复用。"""
from datetime import time

from jevq.models import Quote


def committee(q: Quote) -> float | None:
    """委比% = (五档买量之和 - 五档卖量之和) / (买量之和 + 卖量之和) × 100。"""
    if not q.bid_vols or not q.ask_vols:
        return None
    buy, sell = sum(q.bid_vols), sum(q.ask_vols)
    total = buy + sell
    if total == 0:
        return None
    return round((buy - sell) / total * 100, 2)


def outer_ratio(q: Quote) -> float | None:
    """外盘占比% = 外盘 / (外盘 + 内盘) × 100。"""
    total = q.outer_vol + q.inner_vol
    if total <= 0:
        return None
    return round(q.outer_vol / total * 100, 2)


def avg_pct(q: Quote) -> float | None:
    """均价% = (均价 / 昨收 - 1) × 100。"""
    if q.avg_price <= 0 or q.pre_close <= 0:
        return None
    return round((q.avg_price / q.pre_close - 1) * 100, 2)


def body_pct(q: Quote) -> float | None:
    """实体% = (现价 - 开盘) / 昨收 × 100。"""
    if q.pre_close <= 0:
        return None
    return round((q.price - q.open) / q.pre_close * 100, 2)


def seal_amount(q: Quote) -> float | None:
    """封单额（亿）：仅封死涨停时 = 买一量 × 100 × 涨停价 / 1e8，否则 None。"""
    if q.limit_up <= 0 or q.price < q.limit_up:
        return None
    if not q.bid_vols:
        return None
    return round(q.bid_vols[0] * 100 * q.limit_up / 1e8, 4)


def seal_ratio(q: Quote) -> float | None:
    """封成比 = 封单额 / 成交额（同口径换算为元后相除）。"""
    sa = seal_amount(q)
    if sa is None or q.amount <= 0:
        return None
    return round(sa * 1e8 / q.amount, 4)


MIN_PROJECT_MINUTES = 15      # 09:45 之前开盘量太少，折算全天量不可靠，不判断


def elapsed_trading_minutes(t: time) -> float:
    """当日已过的连续竞价分钟数：只算 09:30–11:30 和 13:00–15:00（午休不计），收盘后封顶 240。"""
    m = t.hour * 60 + t.minute + t.second / 60
    morning = min(max(m - (9 * 60 + 30), 0), 120)
    afternoon = min(max(m - 13 * 60, 0), 120)
    return morning + afternoon


def projected_volume(volume: float, t: time) -> float | None:
    """把当日截至 t 的成交量（手）折算成全天量 = 量 ÷ 已过交易分钟 × 240。09:45 前或没成交返回 None。"""
    minutes = elapsed_trading_minutes(t)
    if minutes < MIN_PROJECT_MINUTES or not volume or volume <= 0:
        return None
    return volume / minutes * 240


def vol_step(history: list, today: float | None, n: int | None, down: bool) -> bool | None:
    """阶梯缩量（down=True）/ 阶梯放量：history 是最近几个交易日的成交量（升序，可含 None），
    取最后 n 天加上今天折算的全天量，共 n 步，每一步都严格变小（缩量）/ 变大（放量）才算。
    窗口内历史不足 n 天、有缺口（None）、今天量不能折算 → None（条件不成立）。"""
    if not n or today is None or len(history or []) < n:
        return None
    seq = [*history[-n:], today]
    if any(v is None for v in seq):
        return None
    pairs = zip(seq, seq[1:])
    return all(b < a for a, b in pairs) if down else all(b > a for a, b in pairs)
