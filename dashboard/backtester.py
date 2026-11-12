"""回测员（dj 2026-11-12「加一个股票回测员，专门看当前前期涨跌幅」）。

起因：11-12 灵活交易员和经理账户都买了冰山冷热，一个月翻倍、20 日涨 79%，买在山顶。它们只看当天盘口，看不到前期涨幅。
回测员给每只候选票报两件事：
1. 前期位置：5 日、20 日涨了多少，比 60 日最低高多少（jevq.data.closes.position_stats，只用今天之前的收盘价）；
2. 历史同档：20 日涨幅落在同一档的票，次日开盘买进之后 5 天怎么走（research/position_base_rates.py 算好的底账，
   dashboard/data/position_base_rates.json；2024-11～2026-10 主板，只是历史统计，不是买卖信号）。

不调模型，纯算术。结果进三个地方：交易员和经理的候选信息（每只票的「回测」字段）、经理开会听的汇报（role=backtest）、
经理账户的票池。20 日涨幅 ≥ config.FLEX_MAX_RISE20 的买单由执行器直接拒，回测员只负责说清楚为什么。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from jevq import config

RATES_PATH = Path(__file__).resolve().parent / "data" / "position_base_rates.json"
HIGH_RISE20 = 30.0          # 20 日涨幅到这里算高位：历史上 5 天内跌超 10% 的概率是普通票的 3 倍以上
_cache: dict = {}


def base_rates(path: Path = RATES_PATH) -> list[dict]:
    """底账的「all」表；读不到返回 []（回测员只报位置，不报历史）。按文件修改时间缓存。"""
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return []
    if _cache.get("key") != (str(path), mtime):
        try:
            rows = json.loads(Path(path).read_text(encoding="utf-8")).get("all") or []
        except (OSError, ValueError):
            rows = []
        _cache.update(key=(str(path), mtime), rows=rows)
    return _cache["rows"]


def bucket_of(rise20: float, rows: list[dict]) -> dict | None:
    for r in rows:
        lo = r.get("lo") if r.get("lo") is not None else float("-inf")
        hi = r.get("hi") if r.get("hi") is not None else float("inf")
        if lo <= rise20 < hi:
            return r
    return None


def verdict(rise20: float | None) -> str:
    if rise20 is None:
        return "历史不够"
    if rise20 >= config.FLEX_MAX_RISE20:
        return "高位·程序拒买"
    if rise20 >= HIGH_RISE20:
        return "高位"
    if rise20 <= -20:
        return "超跌"
    return "正常"


def note(stats: dict, rows: list[dict] | None = None) -> str:
    """给一只票写一句回测意见，进候选信息的「回测」字段。没有位置数据返回 ""。"""
    rise20 = stats.get("rise20")
    if rise20 is None:
        return ""
    rows = base_rates() if rows is None else rows
    head = f"{verdict(rise20)}：20日{rise20:+.0f}%"
    if stats.get("fromLow60") is not None:
        head += f"、比60日低点高{stats['fromLow60']:.0f}%"
    b = bucket_of(rise20, rows)
    if not b:
        return head
    return (f"{head}｜历史同档（20日{b['label']}，{b['n']}次）次日开盘买：5日中位{b['med5']:+.1f}%、胜率{b['win5']:.0f}%、"
            f"5天内跌超10%的占{b['deep5']:.0f}%")


def annotate(payload: dict, positions: dict, rows: list[dict] | None = None) -> dict:
    """把位置和回测意见并进候选（code → 字段）。原 payload 不改，返回新的。"""
    rows = base_rates() if rows is None else rows
    out = {}
    for code, q in payload.items():
        st = positions.get(code) or {}
        text = note(st, rows)
        out[code] = {**q, **st, **({"回测": text} if text else {})}
    return out


def report(quotes: dict, now: datetime, rows: list[dict] | None = None) -> dict:
    """经理开会听的那一行（和 manager.desk_reports 里的其他岗位同一个格式），外加逐只明细 items。"""
    rows = base_rates() if rows is None else rows
    known = [(c, q) for c, q in (quotes or {}).items() if q.get("rise20") is not None]
    base = {"role": "backtest", "code": "BACKTEST", "name": "回测", "kind": "回测", "ts": f"{now:%H:%M}"}
    if not known:
        return {**base, "line": "候选没有位置数据", "level": "idle", "items": []}
    known.sort(key=lambda x: x[1]["rise20"], reverse=True)
    high = [(c, q) for c, q in known if q["rise20"] >= HIGH_RISE20]
    banned = [(c, q) for c, q in high if q["rise20"] >= config.FLEX_MAX_RISE20]
    line = f"看了 {len(known)} 只 · 高位 {len(high)} 只"
    if banned:
        line += f"（拒买 {len(banned)}）"
    if high:
        c, q = high[0]
        line += f" · 最高 {q.get('name') or c} 20日{q['rise20']:+.0f}%"
    level = "bad" if banned else "warn" if high else "ok"
    items = [{"code": c, "name": q.get("name"), "rise5": q.get("rise5"), "rise20": q["rise20"], "fromLow60": q.get("fromLow60"),
              "verdict": verdict(q["rise20"]), "note": note(q, rows)} for c, q in known[:12]]
    return {**base, "line": line, "level": level, "items": items}
