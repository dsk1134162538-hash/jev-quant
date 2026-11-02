"""高标接力 7 年复验（dj 2026-11-02）：逐年表现、相邻参数、连亏与回撤。数据 2019 起（kline_long.py）+ 现有两年。

    python -m research.relay_years
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean, median

from research import dragon_relay as dr


def fmt(v: list[float]) -> str:
    if not v:
        return "n=0"
    return (f"n={len(v):4}  均{mean(v):+6.2f}  中位{median(v):+6.2f}  胜{sum(a > 0 for a in v) / len(v) * 100:3.0f}%  "
            f"合计{sum(v):+8.1f}")


def worst_streak(rows: list[dict], key: str = "x_close") -> tuple[int, float]:
    """按日期排的最长连亏笔数、最大回撤（等权单利累加，%）。"""
    cum = peak = dd = 0.0
    run = best = 0
    for x in sorted(rows, key=lambda r: r["date"]):
        v = x[key]
        run = run + 1 if v < 0 else 0
        best = max(best, run)
        cum += v
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return best, round(dd, 1)


def main():
    bars = dr.load()
    streak, per_day, _ = dr.build(bars)
    rows = dr.samples(bars, streak, per_day)
    dates = sorted({r["date"] for r in rows})
    print(f"数据 {dates[0]} ~ {dates[-1]}，股票 {len(bars)} 只，昨日涨停且今天能买到的样本 {len(rows)}")
    base = lambda x: x["s"] >= 3 and 3 <= x["gap"] < 6                        # noqa: E731
    print("\n== 现行规则：3 板以上、竞价高开 3~6%、次日收盘卖，逐年 ==")
    for line in dr.by_year(rows, base):
        print(line)
    sel = [x for x in rows if base(x)]
    print("全部", fmt([x["x_close"] for x in sel]), " 最长连亏/最大回撤(单利累加%)", worst_streak(sel))
    print("\n== 现行规则，次日开盘卖 vs 收盘卖（全部年份）==")
    print("次开", fmt([x["x_open"] for x in sel]))
    print("次收", fmt([x["x_close"] for x in sel]))
    print("\n== 相邻参数（次日收盘卖，按年份分段）==")
    eras = {"2019-2021": ("2019", "2021"), "2022-2023": ("2022", "2023"), "2024-2026": ("2024", "2026")}
    grid = [("3板+ 开3~6%", 3, 3, 6), ("3板+ 开2~5%", 3, 2, 5), ("3板+ 开4~7%", 3, 4, 7), ("3板+ 开2~7%", 3, 2, 7),
            ("2板+ 开3~6%", 2, 3, 6), ("4板+ 开3~6%", 4, 3, 6), ("5板+ 开3~6%", 5, 3, 6), ("3板+ 开0~3%", 3, 0, 3),
            ("3板+ 开6%+", 3, 6, 99)]
    for name, k, lo, hi in grid:
        cells = []
        for era, (a, b) in eras.items():
            v = [x["x_close"] for x in rows if x["s"] >= k and lo <= x["gap"] < hi and a <= x["date"][:4] <= b]
            cells.append(f"{era} " + (f"n={len(v):4} {mean(v):+5.2f}" if v else "n=0"))
        print(f"{name:<14}", "   ".join(cells))
    print("\n== 按月（现行规则）：正月份占比 ==")
    m = defaultdict(list)
    for x in sel:
        m[x["date"][:7]].append(x["x_close"])
    pos = sum(mean(v) > 0 for v in m.values())
    print(f"{len(m)} 个有信号的月份里 {pos} 个平均为正；每月平均笔数 {len(sel) / len(m):.1f}")


if __name__ == "__main__":
    main()
