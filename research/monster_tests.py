"""妖股跟随的细查（dj 2026-11-02 夜里）：
A. 6 板以上、次日竞价买、一直拿到断板那天收盘：逐年、能不能卖（断板当天收跌停）、利润是不是靠少数几笔。
B. 高标接力加「前一日高标赚钱效应」过滤：昨天（已知）买了 3 板以上的那批，次日收盘平均赚没赚；赚了今天才做。

    python -m research.monster_tests
"""
from __future__ import annotations

import pickle
from collections import defaultdict
from statistics import mean, median

from research import dragon_relay as dr
from research.dragon_relay import ART, lim
from research.monster import streak_series
from research.relay_years import fmt


def a_six_plus(bars, eps):
    print("== A. 6 板以上、次日竞价买（能买到的）、拿到断板当天收盘卖 ==")
    xs = [e for e in eps if e["k"] >= 6 and "x_break_close" in e]
    print("全部", fmt([e["x_break_close"] for e in xs]))
    by = defaultdict(list)
    for e in xs:
        by[e["date"][:4]].append(e["x_break_close"])
    for y in sorted(by):
        print(f"  {y} {fmt(by[y])}")
    v = sorted(e["x_break_close"] for e in xs)
    total = sum(v)
    for cut in (0.01, 0.05, 0.10):
        top = sum(v[-max(1, int(len(v) * cut)):])
        print(f"  最好的 {cut * 100:.0f}% 笔（{max(1, int(len(v) * cut))} 笔）合计 {top:+.1f}，占全部 {total:+.1f}")
    print(f"  去掉最好 10 笔后均值 {mean(v[:-10]):+.2f}，最差 5 笔 {[round(x, 1) for x in v[:5]]}")
    # 断板当天收跌停 = 实盘卖不出去
    stuck = 0
    for e in xs:
        rows = bars[e["code"]]
        d = next(i for i, r in enumerate(rows) if r[0] == e["date"])
        m = d + 1
        while m < len(rows) - 1 and rows[m][2] >= lim(rows[m - 1][2]):
            m += 1
        m = max(m, d + 2)
        if m < len(rows) and rows[m][2] <= round(rows[m - 1][2] * 0.9 + 1e-9, 2):
            stuck += 1
    print(f"  断板当天收盘就是跌停（卖不掉）的有 {stuck}/{len(xs)} = {stuck / len(xs) * 100:.1f}%")
    print("  6 板+ 且次日高开 0~6%：", fmt([e["x_break_close"] for e in xs if 0 <= e["gap"] < 6]))
    print("  6 板+ 且次日低开/平开(<0)：", fmt([e["x_break_close"] for e in xs if e["gap"] < 0]))


def b_feedback_filter(rows):
    print("\n== B. 前一日高标赚钱效应过滤（高标接力：3 板以上、高开 3~6%、次日收盘卖）==")
    sel = [x for x in rows if x["s"] >= 3 and 3 <= x["gap"] < 6]
    by_day = defaultdict(list)
    for x in sel:
        by_day[x["date"]].append(x["x_close"])
    days = sorted(by_day)
    # 前一信号日的结果，在今天开盘时还不知道（它的卖出是今天收盘）——用「前一信号日买的票在其买入当天的收盘表现」更保守：
    # 这里取更简单可知的量：上上个信号日及以前买的票的次日收盘结果（已全部结算）
    kept, dropped = [], []
    for i, d in enumerate(days):
        if i < 2:
            continue
        known = by_day[days[i - 2]]            # 已结算
        (kept if mean(known) > 0 else dropped).append((d, mean(by_day[d])))
    for name, grp in (("前一批已结算的高标接力赚钱 → 做", kept), ("前一批亏 → 不做", dropped)):
        v = [m for _, m in grp]
        by = defaultdict(list)
        for d, m in grp:
            by[d[:4]].append(m)
        print(f"{name}: 信号日 {len(v)} 天，日均 {mean(v):+.2f}  " + " ".join(f"{y}:{mean(vs):+.1f}({len(vs)})" for y, vs in sorted(by.items())))


def main():
    bars = dr.load()
    eps = pickle.load(open(ART / "monster_episodes.pkl", "rb"))
    a_six_plus(bars, eps)
    streak, per_day, _ = dr.build(bars)
    b_feedback_filter(dr.samples(bars, streak, per_day))


if __name__ == "__main__":
    main()
