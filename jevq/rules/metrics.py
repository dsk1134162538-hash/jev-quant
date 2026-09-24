"""从 Quote 派生的盘口指标。纯函数，数据缺失一律返回 None，供 Task 5 的取值函数复用。"""
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
