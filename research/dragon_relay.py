"""打板接力 / 龙头（dj 2026-11-02「还是打板或做龙头，去想办法」）：只测「竞价能买到」的买点。

9% 扫板买不到快板（entry_points.py 已证明），所以买点改到下一天集合竞价：
昨天收涨停（连板数 s），今天按开盘价买（开在涨停 = 排不到，不算），次日开盘 / 次日收盘 / 「一直封板就拿，
断板次日开盘卖」三种卖法。按连板数、开盘幅度、是不是全市场最高板（龙头）、前一天市场情绪分格。
数据：research/artifacts/first_board/kline*.sqlite（腾讯日线不复权，2024-11 ~ 2026-10，主板）。

    python -m research.dragon_relay
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import mean

ART = Path(__file__).resolve().parent / "artifacts" / "first_board"
FEE = 0.15
SPLIT = "2026-03-03"
RIDE_MAX = 10


def lim(p: float) -> float:
    return round(p * 1.1 + 1e-9, 2)


def load() -> dict[str, list[tuple]]:
    by = defaultdict(dict)
    for name in ("kline_long.sqlite", "kline_2025.sqlite", "kline.sqlite"):    # kline_long：2019~2024（kline_long.py）
        if not (ART / name).exists():
            continue
        conn = sqlite3.connect(ART / name)
        for code, d, o, c, h, lo, v in conn.execute("SELECT code, date, open, close, high, low, volume FROM k"):
            if code.startswith(("60", "00")) and o > 0:
                by[code][d] = (d, o, c, h, lo, v)
        conn.close()
    return {code: [m[d] for d in sorted(m)] for code, m in by.items()}


def build(bars: dict[str, list[tuple]]):
    """每只票每天：涨停？连板数。再汇总每天的全市场最高板、涨停/跌停家数、昨日涨停今日红盘率。"""
    streak = {}                                   # (code, i) -> 连板数（当天收涨停才有）
    per_day = defaultdict(lambda: {"zt": 0, "dt": 0, "max": 0})
    zt_codes = defaultdict(set)
    for code, rows in bars.items():
        s = 0
        for i in range(1, len(rows)):
            d, o, c, h, lo, v = rows[i]
            pre = rows[i - 1][2]
            is_zt = pre > 0 and c >= lim(pre)
            s = s + 1 if is_zt else 0
            if is_zt:
                streak[(code, i)] = s
                per_day[d]["zt"] += 1
                per_day[d]["max"] = max(per_day[d]["max"], s)
                zt_codes[d].add(code)
            elif pre > 0 and c <= round(pre * 0.9 + 1e-9, 2):
                per_day[d]["dt"] += 1
    return streak, per_day, zt_codes


def samples(bars, streak, per_day):
    out = []
    for code, rows in bars.items():
        for i in range(1, len(rows) - 2):
            s = streak.get((code, i))
            if not s:
                continue
            d = rows[i][0]
            j = i + 1
            o1, pre1 = rows[j][1], rows[i][2]
            if o1 >= lim(pre1):
                continue                                         # 一字 / 开在涨停：竞价排不到
            entry = o1
            k = i + 2
            ride = None                                          # 封板就拿，断板次日开盘卖
            for m in range(j, min(len(rows) - 1, j + RIDE_MAX)):
                if rows[m][2] < lim(rows[m - 1][2]):
                    ride = rows[m + 1][1]
                    break
            mk = per_day[d]
            out.append({"date": rows[j][0], "code": code, "s": s, "gap": (o1 / pre1 - 1) * 100,
                        "dragon": s == mk["max"] and s >= 2, "zt": mk["zt"], "dt": mk["dt"],
                        "x_open": (rows[k][1] / entry - 1) * 100 - FEE,
                        "x_close": (rows[k][2] / entry - 1) * 100 - FEE,
                        "x_ride": None if ride is None else (ride / entry - 1) * 100 - FEE,
                        "sealed1": rows[j][2] >= lim(pre1)})
    return out


def gap_bucket(g: float) -> str:
    return ("<-3%" if g < -3 else "-3~0%" if g < 0 else "0~3%" if g < 3 else "3~6%" if g < 6 else "≥6%")


def s_bucket(s: int) -> str:
    return f"{s}板" if s < 4 else "4板+"


def fmt(xs: list[dict]) -> str:
    if not xs:
        return "n=0"
    ride = [x["x_ride"] for x in xs if x["x_ride"] is not None]
    return (f"n={len(xs):5}  当天封{sum(x['sealed1'] for x in xs) / len(xs) * 100:3.0f}%  "
            f"次开{mean(x['x_open'] for x in xs):+6.2f}  次收{mean(x['x_close'] for x in xs):+6.2f}  "
            f"骑板{mean(ride):+6.2f}" if ride else "")


def report(rows: list[dict], key, title: str, min_n: int = 60) -> list[str]:
    lines = [f"\n—— {title} ——"]
    groups = defaultdict(lambda: {"训练": [], "验证": []})
    for x in rows:
        k = key(x)
        if k is not None:
            groups[k]["训练" if x["date"] < SPLIT else "验证"].append(x)
    for k in sorted(groups):
        g = groups[k]
        if len(g["训练"]) < min_n or len(g["验证"]) < min_n // 2:
            continue
        lines.append(f"{str(k):<22} 训练 {fmt(g['训练'])}")
        lines.append(f"{'':<22} 验证 {fmt(g['验证'])}")
    return lines


def by_year(rows: list[dict], rule) -> list[str]:
    """一条规则逐年的笔数、平均、中位、胜率（次日收盘卖），看是不是只在某几年赚钱。"""
    from statistics import median
    years = defaultdict(list)
    for x in rows:
        if rule(x):
            years[x["date"][:4]].append(x["x_close"])
    lines = []
    for y in sorted(years):
        v = years[y]
        lines.append(f"{y}  n={len(v):4}  均{mean(v):+6.2f}  中位{median(v):+6.2f}  胜{sum(a > 0 for a in v) / len(v) * 100:3.0f}%  "
                     f"合计{sum(v):+7.1f}")
    return lines


def main():
    bars = load()
    streak, per_day, _ = build(bars)
    rows = samples(bars, streak, per_day)
    out = [f"样本 {len(rows)}（昨日收涨停、今天开盘没封死、竞价买入），训练 < {SPLIT} ≤ 验证"]
    out += report(rows, lambda x: s_bucket(x["s"]), "按连板数")
    out += report(rows, lambda x: (s_bucket(x["s"]), gap_bucket(x["gap"])), "连板数 × 竞价开盘")
    out += report(rows, lambda x: ("龙头" if x["dragon"] else "非龙头", gap_bucket(x["gap"])) if x["s"] >= 2 else None,
                  "2板以上：龙头 × 竞价开盘", min_n=30)
    out += report(rows, lambda x: (s_bucket(x["s"]), "情绪好" if x["zt"] > x["dt"] * 2 else "情绪差"),
                  "连板数 × 昨日涨停是否超过跌停 2 倍")
    print("\n".join(out))


if __name__ == "__main__":
    main()
