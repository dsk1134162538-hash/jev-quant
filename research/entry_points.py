"""买点对比（dj 2026-11-02「怎么买到快速封板的票」）：用新浪 5 分钟线（research/min5_backfill.py）比较
9% 追 / 7% 追 / 涨速触发的半路板，次日五种卖法。

5 分钟线看不到一根 K 线里的先后：9% 追的那根如果收在涨停，算两遍——乐观（买到了）和悲观（没买到）。
7% 和涨速触发都在 9% 之前，价格连续走过去时一定成交（跳空高开在触发价之上就按开盘价，开在涨停就买不到）。
涨速只用触发那一刻之前看得到的：触发价相对「前两根最低价和本根开盘价」的涨幅。

    python -m research.entry_points
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import mean

ART = Path(__file__).resolve().parent / "artifacts" / "first_board"
DB = ART / "min5.sqlite"
FEE = 0.15
START, SPLIT = "2026-06-01", "2026-08-20"


def lim(p: float) -> float:
    return round(p * 1.1 + 1e-9, 2)


def load(db: Path = DB):
    conn = sqlite3.connect(db)
    bars = defaultdict(list)
    for d, code, t, o, h, lo, c, v in conn.execute("SELECT * FROM bars ORDER BY code, date, hhmm"):
        bars[(d, code)].append((t, o, h, lo, c, v))
    days = [(d, code, pre) for d, code, pre, kind in conn.execute("SELECT * FROM days") if kind == "touch"]
    conn.close()
    cal = sorted({d for d, _ in bars})
    nxt = {d: cal[i + 1] for i, d in enumerate(cal[:-1])}
    return bars, days, nxt


def exits(entry: float, b: list[tuple]) -> dict[str, float]:
    def r(px):
        return (px / entry - 1) * 100 - FEE
    first, last = b[0][1], b[-1][4]
    at10 = next((c for t, o, h, lo, c, v in b if t >= "10:00"), last)

    def limit(target):
        if first >= target:
            return first
        return target if any(h >= target for _, o, h, lo, c, v in b) else last
    return {"开盘": r(first), "10点": r(at10), "收盘": r(last), "保本挂单": r(limit(entry)), "+3%挂单": r(limit(entry * 1.03))}


def entry_at(b: list[tuple], pre: float, level: float, until: str, velocity: float | None = None):
    """第一次够到 pre*level 的那根 → (买价, 那根是否收在涨停)；买不到返回 None。"""
    L, px = lim(pre), round(pre * level, 2)
    for k, (t, o, h, lo, c, v) in enumerate(b):
        if t > until:
            return None
        if h < px:
            continue
        if o >= L:
            return None                                       # 开在涨停 / 一根内跳到涨停开盘：买不到
        entry = max(o, px)
        if velocity is not None:
            if entry >= pre * 1.09:
                return None                                   # 涨速没满足就过了 9%，半路板不追
            base = min([o] + [b[j][3] for j in range(max(0, k - 2), k)])
            if entry / base - 1 < velocity:
                return None                                   # 第一次到触发价时涨速不够：这天不做（不等第二次）
        return entry, c >= L
    return None


RULES = {
    "9%追（乐观）": dict(level=1.09, optimistic=True),
    "9%追（悲观）": dict(level=1.09, optimistic=False),
    "7%追": dict(level=1.07),
    "8%追": dict(level=1.08),
    "7%且10分钟涨≥3%": dict(level=1.07, velocity=0.03),
    "7%且10分钟涨≥4%": dict(level=1.07, velocity=0.04),
    "5%且10分钟涨≥3%": dict(level=1.05, velocity=0.03),
}


def run(until: str = "10:30"):
    bars, days, nxt = load()
    res = {name: defaultdict(list) for name in RULES}
    for d, code, pre in days:
        if d < START or d not in nxt:
            continue
        b, f = bars.get((d, code)), bars.get((nxt[d], code))
        if not b or not f or pre <= 0 or not (0.85 < b[0][1] / pre < 1.11):
            continue
        L = lim(pre)
        seg = "训练" if d < SPLIT else "验证"
        for name, p in RULES.items():
            got = entry_at(b, pre, p["level"], until, p.get("velocity"))
            if got is None:
                continue
            entry, bar_sealed = got
            if p.get("optimistic") is False and bar_sealed:
                continue
            res[name][seg].append({"sealed": b[-1][4] >= L, **exits(entry, f)})
    return res


def fmt(xs: list[dict]) -> str:
    if not xs:
        return "n=0"
    seal = sum(x["sealed"] for x in xs) / len(xs) * 100
    return (f"n={len(xs):5}  封住{seal:4.0f}%  开盘{mean(x['开盘'] for x in xs):+.2f}  "
            f"10点{mean(x['10点'] for x in xs):+.2f}  收盘{mean(x['收盘'] for x in xs):+.2f}  "
            f"保本{mean(x['保本挂单'] for x in xs):+.2f}  +3%{mean(x['+3%挂单'] for x in xs):+.2f}")


if __name__ == "__main__":
    for until in ("10:30", "14:30"):
        print(f"\n######## 触发截止 {until}（训练 {START}~{SPLIT}，验证 {SPLIT} 以后）")
        for name, segs in run(until).items():
            print(f"{name:<16} 训练 {fmt(segs['训练'])}")
            print(f"{'':<16} 验证 {fmt(segs['验证'])}")
