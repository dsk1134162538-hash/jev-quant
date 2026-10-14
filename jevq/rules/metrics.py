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


# A 股日内累计成交量曲线：开盘后第 N 个交易分钟（午休不计），全天量通常完成了几成。
# 10-14 从 160 只股票 × 5 个交易日的分时统计（取中位数）：两头多、中间少，开盘半小时就成交了三成。
# 原来线性折算（量 ÷ 分钟 × 240）在 10:00 把全天量估高 2.4 倍，活跃的票几乎都被判成「今天放量」。
VOLUME_PROFILE = {
    0: 0.0037, 5: 0.0918, 10: 0.1439, 15: 0.1893, 20: 0.2313, 25: 0.2704, 30: 0.3004, 35: 0.3316, 40: 0.3557,
    45: 0.3781, 50: 0.3978, 55: 0.42, 60: 0.4389, 65: 0.4605, 70: 0.4777, 75: 0.4936, 80: 0.5078, 85: 0.5224,
    90: 0.5358, 95: 0.5488, 100: 0.5598, 105: 0.57, 110: 0.5814, 115: 0.5926, 120: 0.6038, 125: 0.6291,
    130: 0.6444, 135: 0.6581, 140: 0.6779, 145: 0.6899, 150: 0.7039, 155: 0.7175, 160: 0.7322, 165: 0.7481,
    170: 0.7612, 175: 0.7729, 180: 0.7865, 185: 0.8013, 190: 0.8154, 195: 0.8305, 200: 0.8466, 205: 0.8619,
    210: 0.8739, 215: 0.8938, 220: 0.911, 225: 0.9294, 230: 0.9496, 235: 0.9763, 240: 1.0}
_PROFILE_KEYS = sorted(VOLUME_PROFILE)


def intraday_fraction(minutes: float) -> float:
    """开盘后 minutes 个交易分钟时全天量通常完成的比例（VOLUME_PROFILE 两点之间线性插值）。"""
    if minutes >= 240:
        return 1.0
    lo = max(k for k in _PROFILE_KEYS if k <= minutes)
    hi = min(k for k in _PROFILE_KEYS if k >= minutes)
    if lo == hi:
        return VOLUME_PROFILE[lo]
    w = (minutes - lo) / (hi - lo)
    return VOLUME_PROFILE[lo] + (VOLUME_PROFILE[hi] - VOLUME_PROFILE[lo]) * w


def projected_volume(volume: float, t: time) -> float | None:
    """把当日截至 t 的成交量（手）折算成全天量 = 量 ÷ 此刻通常已完成的比例（VOLUME_PROFILE）。
    09:45 前或没成交返回 None。"""
    minutes = elapsed_trading_minutes(t)
    if minutes < MIN_PROJECT_MINUTES or not volume or volume <= 0:
        return None
    return volume / intraday_fraction(minutes)


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


def vol_series(feat: dict, q: Quote, t: time) -> tuple[list, float | None] | None:
    """阶梯缩量/放量用的成交量序列 (历史, 今天)。
    行情晚于历史最后一天（盘中/当天收盘后）：今天的量按已过交易分钟折算全天量。
    行情就是历史最后一天（周末/盘前预览，行情还是上一交易日的）：那天已收盘入库，把它当「今天」、用库里的全天量，
    历史往前挪一天，不把同一天算两次。没有历史、或行情比历史还旧 → None。"""
    until = feat.get("vols_until")
    vols = feat.get("vols") or []
    q_day = q.ts.date().isoformat()
    if until is None or q_day < until:
        return None
    if q_day == until:
        return vols[:-1], (vols[-1] if vols else None)
    return vols, projected_volume(q.volume, t)


MAX_SHRINK_DAYS = 5      # 与阶梯缩量字段 N 的上限一致（库里只留 6 天成交量）


def shrink_days(feat: dict, q: Quote, t: time) -> int | None:
    """连续缩量天数（含今天，不看涨跌）：今天比昨天小、昨天比前天小……最多数到 MAX_SHRINK_DAYS；今天没缩量是 0。
    量能数据不能判断（没有历史、今天的量不能折算，如 09:45 前）→ None；更早的历史有缺口只数到缺口前。"""
    series = vol_series(feat, q, t)
    if series is None:
        return None
    history, today = series
    if vol_step(history, today, 1, down=True) is None:
        return None
    days = 0
    for n in range(1, MAX_SHRINK_DAYS + 1):
        if vol_step(history, today, n, down=True) is not True:
            break
        days = n
    return days
