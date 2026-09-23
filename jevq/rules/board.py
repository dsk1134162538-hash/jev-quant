from dataclasses import dataclass, field
from datetime import datetime, time

from jevq.models import Quote

ENTER_PCT = 7.0
EXIT_PCT = 6.0
HOT_PCT = 5.0
HOT_MIN = 3
WATCH_MAX = 5
TRIGGER_PCT = 9.0
SCAN_WINDOWS: list[tuple[time, time]] = [(time(9, 30), time(10, 30))]


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
                     prev_prev_closes: dict[str, float | None], today) -> list[Candidate]:
    """每个热点题材取龙头；满足：涨幅 ≥ ENTER_PCT、今天还没摸过涨停（只扫第一次冲板）、首板。
    按热度降序、龙头分升序，去重后最多 WATCH_MAX。
    首板判断的昨收用实时行情的 pre_close，prev_prev_closes 只提供前日收（code → 收盘价）。"""
    out: dict[str, Candidate] = {}
    hot = hot_concepts(quotes, concept_map, today)
    for concept, members in sorted(hot.items(), key=lambda kv: -len(kv[1])):
        cand = leader(concept, members, quotes, ret5)
        if cand is None or cand.code in out:
            continue
        q = quotes[cand.code]
        if q.pct < ENTER_PCT or q.high >= q.limit_up                 or not is_first_board(q.pre_close, prev_prev_closes.get(cand.code)):
            continue
        out[cand.code] = cand
    return sorted(out.values(), key=lambda c: (-c.heat, c.score))[:WATCH_MAX]


def in_scan_window(t: time, windows: list[tuple[time, time]] = SCAN_WINDOWS) -> bool:
    return any(a <= t < b for a, b in windows)


def scan_trigger(q: Quote, now: datetime, windows: list[tuple[time, time]] = SCAN_WINDOWS) -> bool:
    """扫板买点：窗口内、行情当日、涨幅 ≥ TRIGGER_PCT、今天最高价还没到涨停价。
    已封死视为排队不成交；摸过涨停又回落的（炸过板）不再扫，只扫第一次冲板。"""
    return (in_scan_window(now.time(), windows) and q.ts.date() >= now.date() and not q.suspended
            and q.pct >= TRIGGER_PCT and q.price < q.limit_up and q.high < q.limit_up)
