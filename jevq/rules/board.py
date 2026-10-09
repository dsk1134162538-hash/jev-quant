from dataclasses import dataclass, field, replace
from datetime import datetime, time

from jevq.models import Quote

ENTER_PCT = 7.0
EXIT_PCT = 6.0
HOT_PCT = 5.0
HOT_MIN = 3
WATCH_MAX = 5
TRIGGER_PCT = 9.0
MAINLINE_MIN = 5            # 只做主线（用户 2026-10-09）：题材内涨超 HOT_PCT 的 ≥ 这么多只，或早报/晚报点名（点名的仍要 ≥ HOT_MIN）
VOLUME_MIN = 1.5            # 扫板那一刻累计成交量 ÷ 前几天同一时刻的平均累计量，低于这个算缩量板，不扫（用户 2026-10-09）
SCAN_WINDOWS: list[tuple[time, time]] = [(time(9, 30), time(10, 30))]
# 情绪闸门（试验中：只记录、不拦截）。上一交易日收盘前的统计碰到任意一条就记为「会拦」
GATE_BREAK_RATE = 30.0      # 炸板率高于这个百分比
GATE_LIMIT_DOWN = 10        # 跌停家数多于这个数
GATE_MIN_BOARD = 3          # 最高连板不到这个板数


def limit_price(pre_close: float) -> float:
    return round(pre_close * 1.1, 2)


def is_first_board(prev_close: float | None, prev_prev_close: float | None) -> bool:
    """昨日未涨停才算首板；缺数据时不算（保守）。"""
    if not prev_close or not prev_prev_close:
        return False
    return prev_close < limit_price(prev_prev_close)


def hot_concepts(quotes: dict[str, Quote], concept_map: dict[str, list[str]], today) -> dict[str, list[str]]:
    """题材 → 该题材中当日涨幅 ≥ HOT_PCT 的成员（仅当成员数 ≥ HOT_MIN）。quotes 应只含股票池内代码。"""
    strong: dict[str, list[str]] = {}
    for code, q in quotes.items():
        if q.ts.date() < today or q.is_st or q.suspended or q.pct < HOT_PCT:
            continue
        for c in concept_map.get(code, []):
            strong.setdefault(c, []).append(code)
    return {c: codes for c, codes in strong.items() if len(codes) >= HOT_MIN}


@dataclass(frozen=True)
class Candidate:
    code: str
    concept: str
    heat: int
    score: int
    ranks: dict = field(default_factory=dict)     # {"pct": n, "amount": n, "ret5": n}
    article_hot: str = ""                          # hi189 文章点名该题材的原因（空 = 没点名）


def _rank(codes: list[str], key) -> dict[str, int]:
    ordered = sorted(codes, key=key, reverse=True)
    return {c: i + 1 for i, c in enumerate(ordered)}


def leader(concept: str, members: list[str], quotes: dict[str, Quote], ret5: dict[str, float]) -> Candidate | None:
    """在热点题材的强势成员中按三项名次之和选龙头；缺 ret5 的成员不参与。"""
    pool = [c for c in members if c in ret5]
    if not pool:
        return None
    rp = _rank(pool, lambda c: quotes[c].pct)
    ra = _rank(pool, lambda c: quotes[c].amount)
    rr = _rank(pool, lambda c: ret5[c])
    best = min(pool, key=lambda c: (rp[c] + ra[c] + rr[c], rp[c]))
    return Candidate(best, concept, len(members), rp[best] + ra[best] + rr[best],
                     {"pct": rp[best], "amount": ra[best], "ret5": rr[best]})


def watch_candidates(quotes: dict[str, Quote], concept_map: dict[str, list[str]], ret5: dict[str, float],
                     prev_prev_closes: dict[str, float | None], today,
                     exclude: set[str] = frozenset(),
                     article_hot=None, mainline_min: int | None = None) -> list[Candidate]:
    """每个热点题材取龙头；满足：涨幅 ≥ ENTER_PCT、今天还没摸过涨停（只扫第一次冲板）、首板。
    按热度降序、龙头分升序，去重后最多 WATCH_MAX。
    首板判断的昨收用实时行情的 pre_close，prev_prev_closes 只提供前日收（code → 收盘价）。
    exclude：近 10 日涨停过的代码，龙头命中就跳过该题材（不取第二名——只做龙头）。
    article_hot(题材, 强势成员) -> 命中原因：文章点名的题材排在前面；None = 不用文章。
    只做主线（2026-10-09）：题材强势成员 ≥ mainline_min，或文章点名了（点名的仍要满足 HOT_MIN）。"""
    out: dict[str, Candidate] = {}
    hot = hot_concepts(quotes, concept_map, today)
    why = {c: (article_hot(c, m) if article_hot else "") or "" for c, m in hot.items()}
    line = MAINLINE_MIN if mainline_min is None else mainline_min
    hot = {c: m for c, m in hot.items() if len(m) >= line or why[c]}      # 只做主线
    for concept, members in sorted(hot.items(), key=lambda kv: (not why[kv[0]], -len(kv[1]))):
        cand = leader(concept, members, quotes, ret5)
        if cand is None or cand.code in out or cand.code in exclude:
            continue
        q = quotes[cand.code]
        if q.pct < ENTER_PCT or q.high >= q.limit_up                 or not is_first_board(q.pre_close, prev_prev_closes.get(cand.code)):
            continue
        out[cand.code] = replace(cand, article_hot=why[concept])
    return sorted(out.values(), key=lambda c: (not c.article_hot, -c.heat, c.score))[:WATCH_MAX]


def in_scan_window(t: time, windows: list[tuple[time, time]] = SCAN_WINDOWS) -> bool:
    return any(a <= t < b for a, b in windows)


def scan_trigger(q: Quote, now: datetime, windows: list[tuple[time, time]] = SCAN_WINDOWS) -> bool:
    """扫板买点：窗口内、行情当日、涨幅 ≥ TRIGGER_PCT、今天最高价还没到涨停价。
    已封死视为排队不成交；摸过涨停又回落的（炸过板）不再扫，只扫第一次冲板。"""
    return (in_scan_window(now.time(), windows) and q.ts.date() >= now.date() and not q.suspended
            and q.pct >= TRIGGER_PCT and q.price < q.limit_up and q.high < q.limit_up)


def sentiment_gate(stats: dict | None) -> tuple[bool | None, str]:
    """情绪闸门：按上一交易日收盘前最后一次情绪统计，判断今天该不该打板。
    返回（会不会拦, 说明）；统计缺失或缺项时不下结论，返回 (None, 原因)。"""
    rate, down, top = ((stats or {}).get(k) for k in ("炸板率%", "跌停家数", "最高连板"))
    if rate is None or down is None or top is None:
        return None, "没有上一交易日的情绪统计"
    reasons = []
    if rate > GATE_BREAK_RATE:
        reasons.append(f"炸板率 {rate}% 高于 {GATE_BREAK_RATE:g}%")
    if down > GATE_LIMIT_DOWN:
        reasons.append(f"跌停 {down} 家多于 {GATE_LIMIT_DOWN} 家")
    if top < GATE_MIN_BOARD:
        reasons.append(f"最高只有 {top} 连板，不到 {GATE_MIN_BOARD} 板")
    if reasons:
        return True, "；".join(reasons)
    return False, f"炸板率 {rate}%、跌停 {down} 家、最高 {top} 连板"


def same_time_ratio(volume: float, days: dict[str, dict[str, int]], today: str, hhmm: str) -> float | None:
    """同时段放量倍数：今天到 hhmm 的累计成交量 ÷ 之前几天到同一分钟（没有这一分钟就取它之前最近的一分钟）
    的平均累计量。之前的日子一天都没有 → None（不判断）。"""
    prior = []
    for day, curve in days.items():
        if day >= today or not curve:
            continue
        upto = [m for m in curve if m <= hhmm]
        if upto:
            prior.append(curve[max(upto)])
    prior = [v for v in prior if v > 0]
    if not prior or volume <= 0:
        return None
    return round(volume / (sum(prior) / len(prior)), 2)


def mainline_concepts(quotes: dict[str, Quote], concept_map: dict[str, list[str]], today, article_hot=None,
                      mainline_min: int | None = None) -> set[str]:
    """今天的主线题材（扫首板、低吸共用）：强势成员 ≥ MAINLINE_MIN，或文章点名且强势成员 ≥ HOT_MIN。"""
    line = MAINLINE_MIN if mainline_min is None else mainline_min
    out = set()
    for concept, members in hot_concepts(quotes, concept_map, today).items():
        if len(members) >= line or (article_hot and article_hot(concept, members)):
            out.add(concept)
    return out
