"""弱转强的选股（dj 2026-11-03，知识库「弱转强」）：昨天炸板的高标（炸板前 ≥WTS_MIN_PRIOR_STREAK 连板），
今天竞价开盘涨幅在 WTS_GAP_PCT 内（一字、顶到涨停附近预期已打满，不做）。候选池由 jevq.data.wts_pool.build_pool 盘前算好。"""
from datetime import date

from jevq import config
from jevq.models import Quote

MAIN_BOARD = ("60", "00")


def wts_candidates(quotes: dict[str, Quote], pool: dict[str, int], today: date) -> list[tuple[Quote, int]]:
    """炸板前连板数高的优先，同连板开得低的优先。不截断：主账户排雷后再取前几只。"""
    lo, hi = config.WTS_GAP_PCT
    out = []
    for code, n in pool.items():
        q = quotes.get(code)
        if q is None or not code.startswith(MAIN_BOARD) or q.ts.date() != today:
            continue
        if 0 < q.open < q.limit_up and lo <= q.open_pct < hi:
            out.append((q, n))
    out.sort(key=lambda x: (-x[1], x[0].open_pct))
    return out
