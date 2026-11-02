"""妖股研究（dj 2026-11-02「每月都有几只连板很多的票，一年有几只翻倍妖股，专门做这种」）。

1. 普查：每年/每月最高连板、≥5 板 / ≥7 板的票数、20 个交易日内翻倍的票数。
2. 漏斗：到了 k 板的票，有多大概率到 k+1 板、到 6 板、到 8 板、之后 20 天翻倍——早期能不能分出妖股。
3. 跟妖：k 板次日竞价能买到（没一字）就买，三种卖法：次日收盘、断板当天收盘、断板次日开盘（骑板）。
   看平均、中位、胜率，以及利润有多少来自最好的几笔（妖股策略的命门：少数大赢家养活大量小亏）。
4. 早期特征：3 板那天，后来成妖（≥6 板或 20 天翻倍）和没成妖的票，在一字板占比、流通市值、
   3 板时累计涨幅、当天全市场最高板上有什么区别。

数据：research/artifacts/first_board/kline*.sqlite（含 kline_long 2019 起），不复权，主板。幸存者偏差：退市票没有。

    python -m research.monster
"""
from __future__ import annotations

import pickle
from collections import defaultdict
from statistics import mean, median

from research.dragon_relay import ART, FEE, build, lim, load

DOUBLE_DAYS = 20


def streak_series(bars):
    """每只票逐日的连板数（收盘涨停才 >0）。"""
    out = {}
    for code, rows in bars.items():
        s, seq = 0, [0]
        for i in range(1, len(rows)):
            pre = rows[i - 1][2]
            s = s + 1 if pre > 0 and rows[i][2] >= lim(pre) else 0
            seq.append(s)
        out[code] = seq
    return out


def census(bars, streaks):
    by_year = defaultdict(lambda: {"max": 0, "ge5": set(), "ge7": set(), "double": set()})
    for code, rows in bars.items():
        seq = streaks[code]
        for i, (d, o, c, h, lo, v) in enumerate(rows):
            y = by_year[d[:4]]
            y["max"] = max(y["max"], seq[i])
            if seq[i] >= 5:
                y["ge5"].add(code)
            if seq[i] >= 7:
                y["ge7"].add(code)
            if i >= DOUBLE_DAYS and rows[i - DOUBLE_DAYS][2] > 0 and c / rows[i - DOUBLE_DAYS][2] >= 2:
                y["double"].add(code)
    return {y: {"最高板": v["max"], "≥5板只数": len(v["ge5"]), "≥7板只数": len(v["ge7"]),
                "20天翻倍只数": len(v["double"])} for y, v in sorted(by_year.items())}


def episodes(bars, streaks, per_day):
    """每段连板（到 k 板那天）→ 后来的最高板、之后 20 天最大涨幅、跟妖三种卖法的收益。"""
    out = []
    for code, rows in bars.items():
        seq = streaks[code]
        for i in range(1, len(rows) - 1):
            k = seq[i]
            if k < 2:
                continue
            j = i
            while j + 1 < len(rows) and seq[j + 1] > 0:
                j += 1
            peak = seq[j]
            base = rows[i][2]
            ahead = [r[2] for r in rows[i + 1:i + 1 + DOUBLE_DAYS]]
            # 跟妖：i+1 天竞价开盘买（开在涨停买不到）
            pre1, o1 = rows[i][2], rows[i + 1][1]
            buyable = o1 < lim(pre1)
            rec = {"date": rows[i][0], "code": code, "k": k, "peak": peak, "gap": (o1 / pre1 - 1) * 100,
                   "buyable": buyable, "max20": (max(ahead) / base - 1) * 100 if ahead else 0.0,
                   "yizi": rows[i][1] >= lim(rows[i - 1][2]) and rows[i][4] >= lim(rows[i - 1][2]),
                   "run": (rows[i][2] / rows[max(0, i - k)][2] - 1) * 100,
                   "mkt_max": per_day[rows[i][0]]["max"]}
            if buyable and i + 2 < len(rows):
                rec["x_next_close"] = (rows[i + 2][2] / o1 - 1) * 100 - FEE
                # 断板：从 i+1 天起第一天收盘没涨停（i+1 当天买入不能卖，最早 i+2）
                m = i + 1
                while m < len(rows) - 1 and rows[m][2] >= lim(rows[m - 1][2]):
                    m += 1
                m = max(m, i + 2)
                if m < len(rows):
                    rec["x_break_close"] = (rows[m][2] / o1 - 1) * 100 - FEE
                if m + 1 < len(rows):
                    rec["x_break_next_open"] = (rows[m + 1][1] / o1 - 1) * 100 - FEE
            out.append(rec)
    return out


def _stats(v):
    if not v:
        return "n=0"
    s = sorted(v)
    top = sum(s[-max(1, len(s) // 20):])            # 最好的 5%
    total = sum(s)
    return (f"n={len(v):5}  均{mean(v):+6.2f}  中位{median(v):+6.2f}  胜{sum(x > 0 for x in v) / len(v) * 100:3.0f}%  "
            f"合计{total:+8.1f}  最好5%贡献{top:+8.1f}")


def main():
    bars = load()
    streak_map, per_day, _ = build(bars)
    streaks = streak_series(bars)
    print("== 普查（主板，幸存者）==")
    for y, v in census(bars, streaks).items():
        print(y, v)
    eps = episodes(bars, streaks, per_day)
    pickle.dump(eps, open(ART / "monster_episodes.pkl", "wb"))
    print("\n== 漏斗：到了 k 板的票（全部，不管能不能买）==")
    for k in range(2, 11):
        xs = [e for e in eps if e["k"] == k]
        if xs:
            print(f"{k}板 n={len(xs):5}  再进一板 {sum(e['peak'] > k for e in xs) / len(xs) * 100:4.1f}%  "
                  f"到6板 {sum(e['peak'] >= 6 for e in xs) / len(xs) * 100:4.1f}%  "
                  f"到8板 {sum(e['peak'] >= 8 for e in xs) / len(xs) * 100:4.1f}%  "
                  f"20天内再翻倍 {sum(e['max20'] >= 100 for e in xs) / len(xs) * 100:4.1f}%  "
                  f"次日竞价能买到 {sum(e['buyable'] for e in xs) / len(xs) * 100:4.1f}%")
    print("\n== 跟妖：k 板次日竞价买（能买到的）==")
    for k in (2, 3, 4, 5, 6):
        xs = [e for e in eps if (e["k"] == k if k < 6 else e["k"] >= 6) and "x_next_close" in e]
        for key, label in (("x_next_close", "次日收盘卖"), ("x_break_close", "断板当天收盘卖"),
                           ("x_break_next_open", "断板次日开盘卖")):
            print(f"{k}{'板' if k < 6 else '板+'} {label:<8} {_stats([e[key] for e in xs if key in e])}")
    print("\n== 早期特征：3 板那天，后来成妖（≥6 板或 20 天翻倍）vs 没成妖 ==")
    three = [e for e in eps if e["k"] == 3]
    for label, grp in (("成妖", [e for e in three if e["peak"] >= 6 or e["max20"] >= 100]),
                       ("没成妖", [e for e in three if not (e["peak"] >= 6 or e["max20"] >= 100)])):
        if grp:
            print(f"{label} n={len(grp):4}  3板当天一字 {sum(e['yizi'] for e in grp) / len(grp) * 100:4.1f}%  "
                  f"3板时累计涨幅中位 {median(e['run'] for e in grp):5.1f}%  "
                  f"是全市场最高板 {sum(e['k'] >= e['mkt_max'] for e in grp) / len(grp) * 100:4.1f}%  "
                  f"次日竞价开盘中位 {median(e['gap'] for e in grp):+5.2f}%  "
                  f"次日一字买不到 {sum(not e['buyable'] for e in grp) / len(grp) * 100:4.1f}%")


if __name__ == "__main__":
    main()
