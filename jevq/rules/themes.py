"""题材梯队、阶段、补涨候选池（dj 2026-11-03）。数据：同花顺涨停揭秘（jevq.data.ztpool.fetch_reasons）——
每只涨停股的原因题材（用 + 分隔）和连板描述（首板 / N天M板）。

research/theme_ladder.py（一年真实题材，昨日涨停、今日竞价买、次日收盘卖）：
- 知识库认为「好」的一致高潮（题材涨停 ≥8 家、有二板先锋、梯队完整）次日竞价追买反而更差；
- 题材涨停家数比前一天回落的分歧日更好；
- 题材龙头 ≥5 板时，同题材首板（补涨）小幅高开 0~3% 买相对更好，高开 3~7% 反而亏。
"""
import re
from collections import defaultdict
from datetime import date

from jevq import config
from jevq.data.ztpool import _NOT_THEME, _SPLIT
from jevq.models import Quote

MAIN_BOARD = ("60", "00")


def parse_boards(high_days) -> int:
    m = re.match(r"(\d+)天(\d+)板", high_days or "")
    return int(m.group(2)) if m else 1


def themes_of(reason: str) -> list[str]:
    return [t.strip() for t in _SPLIT.split(reason or "") if t.strip() and not _NOT_THEME.search(t)]


def _stats(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(lambda: {"n": 0, "maxb": 0, "levels": set(), "members": []})
    for r in rows:
        b = parse_boards(r.get("high_days"))
        for t in themes_of(r.get("reason", "")):
            s = out[t]
            s["n"] += 1
            s["maxb"] = max(s["maxb"], b)
            s["levels"].add(min(b, 3))
            s["members"].append((b, r.get("name") or r.get("code") or ""))
    return out


def phase(n: int, prev_n: int, maxb: int) -> str:
    """题材阶段（知识库口径，盘中涨停数还在增加，分歧/退潮要收盘后才准）：空串 = 没有明确阶段。"""
    if prev_n >= 3 and n <= prev_n * 2 / 3:
        return "分歧/退潮"
    if maxb >= 5:
        return "高潮"
    if n >= 5 and n > prev_n and 2 <= maxb <= 4:
        return "发酵"
    if n >= 3 and prev_n < 3:
        return "启动"
    return ""


def ladder(rows: list[dict], prev_rows: list[dict] | None = None, top: int = 8, min_n: int = 2) -> list[dict]:
    """今天的题材梯队：[{theme, n, maxb, levels, leaders, prev_n, phase}]，涨停家数多的在前。
    只留涨停 ≥min_n 家、或最高板 ≥3 的题材（单只首板不算梯队）。"""
    prev = _stats(prev_rows or [])
    out = []
    for theme, s in _stats(rows).items():
        if s["n"] < min_n and s["maxb"] < 3:
            continue
        leaders = [name for b, name in sorted(s["members"], key=lambda x: -x[0]) if b == s["maxb"]][:3]
        prev_n = prev[theme]["n"] if theme in prev else 0
        out.append({"theme": theme, "n": s["n"], "maxb": s["maxb"], "levels": len(s["levels"]), "leaders": leaders,
                    "prev_n": prev_n, "phase": phase(s["n"], prev_n, s["maxb"])})
    out.sort(key=lambda t: (-t["n"], -t["maxb"]))
    return out[:top]


def bz_pool(rows: list[dict], universe) -> dict[str, dict]:
    """补涨候选池（盘前，用昨天的涨停揭秘）：题材龙头 ≥BZ_MIN_LEADER 板，同题材昨天的首板、沪深主板、在股票池里。
    一只票属于几个题材取龙头最高的那个。返回 {代码: {"theme", "leader"}}。"""
    universe = set(universe)
    stats = _stats(rows)
    hot = {t: s for t, s in stats.items() if s["maxb"] >= config.BZ_MIN_LEADER}
    out = {}
    for r in rows:
        code = r.get("code") or ""
        if parse_boards(r.get("high_days")) != 1 or not code.startswith(MAIN_BOARD) or code not in universe:
            continue
        mine = [t for t in themes_of(r.get("reason", "")) if t in hot]
        if mine:
            best = max(mine, key=lambda t: (hot[t]["maxb"], hot[t]["n"]))
            out[code] = {"theme": best, "leader": hot[best]["maxb"]}
    return out


def bz_candidates(quotes: dict[str, Quote], pool: dict[str, dict], today: date) -> list[tuple[Quote, int]]:
    """补涨：今天竞价开盘涨幅在 BZ_GAP_PCT 内（高开太多反而亏，一字买不到）。龙头高的优先，同高度开得低的优先。"""
    lo, hi = config.BZ_GAP_PCT
    out = []
    for code, info in pool.items():
        q = quotes.get(code)
        if q is None or not code.startswith(MAIN_BOARD) or q.ts.date() != today:
            continue
        if 0 < q.open < q.limit_up and lo <= q.open_pct < hi:
            out.append((q, info["leader"]))
    out.sort(key=lambda x: (-x[1], x[0].open_pct))
    return out
