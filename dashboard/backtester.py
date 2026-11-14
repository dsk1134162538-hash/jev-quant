"""回测员（dj 2026-11-12「加一个股票回测员，专门看当前前期涨跌幅」）。

起因：11-12 灵活交易员和经理账户都买了冰山冷热，一个月翻倍、20 日涨 79%，买在山顶。它们只看当天盘口，看不到前期涨幅。
回测员给每只候选票报两件事：
1. 前期位置：5 日、20 日涨了多少，比 60 日最低高多少（jevq.data.closes.position_stats，只用今天之前的收盘价）；
2. 历史同档：20 日涨幅落在同一档的票，次日开盘买进之后 5 天怎么走（research/position_base_rates.py 算好的底账，
   dashboard/data/position_base_rates.json；2024-11～2026-10 主板，只是历史统计，不是买卖信号）；
3. 短期追高（dj 2026-11-14）：昨收比 5 日线高 HIGH_BIAS 以上的另写一句，带乖离那张表的历史同档。
   11-12 买、11-13 全部止损的三只（冰山冷热、002310、600327）买前乖离都在 14% 以上；002310 的 20 日只涨 35%，前两条看不出来。

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
# MA5 乖离到这里算短期追高（dj 2026-11-14，借 daily_stock_analysis 的「乖离率 > 5% 不追高」，按我们自己的底账定在 8%）：
# 主板历史上 8~12% 那档 5 天内跌超 10% 的占 29%、≥12% 占 48%，普通票（0~2%）只有 5%。只提醒，不拒单。
HIGH_BIAS = 8.0
_cache: dict = {}


def _rates(key: str, path: Path) -> list[dict]:
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return []
    if _cache.get("key") != (str(path), mtime):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _cache.update(key=(str(path), mtime), data=data)
    return _cache["data"].get(key) or []


def base_rates(path: Path = RATES_PATH) -> list[dict]:
    """底账的「all」表（按 20 日涨幅分档）；读不到返回 []（回测员只报位置，不报历史）。按文件修改时间缓存。"""
    return _rates("all", path)


def bias_rates(path: Path = RATES_PATH) -> list[dict]:
    """底账的「bias」表（按 MA5 乖离分档）；读不到返回 []。"""
    return _rates("bias", path)


def bucket_of(value: float, rows: list[dict]) -> dict | None:
    for r in rows:
        lo = r.get("lo") if r.get("lo") is not None else float("-inf")
        hi = r.get("hi") if r.get("hi") is not None else float("inf")
        if lo <= value < hi:
            return r
    return None


def _history(b: dict, what: str) -> str:
    return (f"历史同档（{what}{b['label']}，{b['n']}次）次日开盘买：5日中位{b['med5']:+.1f}%、胜率{b['win5']:.0f}%、"
            f"5天内跌超10%的占{b['deep5']:.0f}%")


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


def chasing(stats: dict) -> bool:
    """短期追高：昨收比 MA5 高 HIGH_BIAS 以上。"""
    return (stats.get("biasMa5") or 0) >= HIGH_BIAS


def note(stats: dict, rows: list[dict] | None = None, bias_rows: list[dict] | None = None) -> str:
    """给一只票写一句回测意见，进候选信息的「回测」字段。没有位置数据返回 ""。
    两段：20 日位置（总写）+ 短期追高（MA5 乖离 ≥ HIGH_BIAS 才写，带乖离那张表的历史同档）。"""
    rise20, bias = stats.get("rise20"), stats.get("biasMa5")
    parts = []
    if rise20 is not None:
        rows = base_rates() if rows is None else rows
        head = f"{verdict(rise20)}：20日{rise20:+.0f}%"
        if stats.get("fromLow60") is not None:
            head += f"、比60日低点高{stats['fromLow60']:.0f}%"
        b = bucket_of(rise20, rows)
        parts.append(f"{head}｜{_history(b, '20日')}" if b else head)
    if chasing(stats):
        bias_rows = bias_rates() if bias_rows is None else bias_rows
        head = f"短期追高：昨收比5日线高{bias:.0f}%"
        b = bucket_of(bias, bias_rows)
        parts.append(f"{head}｜{_history(b, '乖离')}" if b else head)
    return "；".join(parts)


def annotate(payload: dict, positions: dict, rows: list[dict] | None = None, bias_rows: list[dict] | None = None) -> dict:
    """把位置和回测意见并进候选（code → 字段）。原 payload 不改，返回新的。"""
    rows = base_rates() if rows is None else rows
    bias_rows = bias_rates() if bias_rows is None else bias_rows
    out = {}
    for code, q in payload.items():
        st = positions.get(code) or {}
        text = note(st, rows, bias_rows)
        out[code] = {**q, **st, **({"回测": text} if text else {})}
    return out


def report(quotes: dict, now: datetime, rows: list[dict] | None = None, bias_rows: list[dict] | None = None) -> dict:
    """经理开会听的那一行（和 manager.desk_reports 里的其他岗位同一个格式），外加逐只明细 items。"""
    rows = base_rates() if rows is None else rows
    bias_rows = bias_rates() if bias_rows is None else bias_rows
    known = [(c, q) for c, q in (quotes or {}).items() if q.get("rise20") is not None]
    base = {"role": "backtest", "code": "BACKTEST", "name": "回测", "kind": "回测", "ts": f"{now:%H:%M}"}
    if not known:
        return {**base, "line": "候选没有位置数据", "level": "idle", "items": []}
    known.sort(key=lambda x: x[1]["rise20"], reverse=True)
    high = [(c, q) for c, q in known if q["rise20"] >= HIGH_RISE20]
    banned = [(c, q) for c, q in high if q["rise20"] >= config.FLEX_MAX_RISE20]
    chase = [(c, q) for c, q in known if chasing(q)]
    line = f"看了 {len(known)} 只 · 高位 {len(high)} 只"
    if banned:
        line += f"（拒买 {len(banned)}）"
    if chase:
        line += f" · 短期追高 {len(chase)} 只"
    if high:
        c, q = high[0]
        line += f" · 最高 {q.get('name') or c} 20日{q['rise20']:+.0f}%"
    elif chase:
        c, q = max(chase, key=lambda x: x[1]["biasMa5"])
        line += f" · 最急 {q.get('name') or c} 比5日线高{q['biasMa5']:.0f}%"
    level = "bad" if banned else "warn" if (high or chase) else "ok"
    items = [{"code": c, "name": q.get("name"), "rise5": q.get("rise5"), "rise20": q["rise20"], "fromLow60": q.get("fromLow60"),
              "biasMa5": q.get("biasMa5"), "verdict": verdict(q["rise20"]) + ("·短期追高" if chasing(q) else ""),
              "note": note(q, rows, bias_rows)} for c, q in known[:12]]
    return {**base, "line": line, "level": level, "items": items}
