"""高标接力 × 竞价状态闸门（dj 2026-11-03「指标和各项规则结合」）。7 年日线（2019 起，kline_long + 现有）。

接力规则不变：昨天收盘 ≥3 连板、今天竞价高开 3~6%、开盘价买、次日收盘卖（也看次日开盘卖）。
闸门用 9:25 竞价结束时就能看到的状态（不用任何未来数据）：
  昨日涨停股今日竞价平均高开 %（昨日所有涨停股的 今日开盘/昨收−1 的均值，一字开在涨停算 +10）
  昨日涨停股今日竞价红开率
  昨日高标（≥3 板）今日竞价平均高开 %
  全市场今日竞价开盘幅度中位数
  昨日涨停家数 / 昨日跌停家数 / 昨日最高板
阈值用 2019-2022 的三分位定，套到 2023-2026 检验；只测单个闸门，不做多闸门穷举（样本不够，会过拟合）。

    python -m research.relay_state
"""
from __future__ import annotations

from statistics import mean, median

from research import dragon_relay as dr

TRAIN_END = "2023"


def main():
    bars = dr.load()
    streak, per_day, zt_codes = dr.build(bars)
    cal = sorted({r[0] for rows in bars.values() for r in rows})
    prev = {d: cal[i - 1] for i, d in enumerate(cal) if i}
    idx = {code: {r[0]: i for i, r in enumerate(rows)} for code, rows in bars.items()}

    state = {}
    for d1, d0 in prev.items():
        gaps, high, allg = [], [], []
        for code in zt_codes.get(d0, ()):
            i1 = idx[code].get(d1)
            i0 = idx[code].get(d0)
            if i1 is None or i0 is None:
                continue
            g = bars[code][i1][1] / bars[code][i0][2] - 1
            gaps.append(g)
            if streak.get((code, i0), 0) >= 3:
                high.append(g)
        for code, rows in bars.items():
            i1 = idx[code].get(d1)
            i0 = idx[code].get(d0)
            if i1 is not None and i0 is not None and rows[i0][2] > 0:
                allg.append(rows[i1][1] / rows[i0][2] - 1)
        if len(gaps) < 5 or len(allg) < 500:
            continue
        pdy = per_day[d0]
        state[d1] = {"昨涨停股竞价均开%": mean(gaps) * 100, "昨涨停股红开率%": mean(g > 0 for g in gaps) * 100,
                     "昨高标竞价均开%": mean(high) * 100 if high else None, "全市场竞价开盘中位%": median(allg) * 100,
                     "昨涨停家数": pdy["zt"], "昨跌停家数": pdy["dt"], "昨最高板": pdy["max"]}
    rows = [dict(x, **state[x["date"]]) for x in dr.samples(bars, streak, per_day)
            if x["s"] >= 3 and 3 <= x["gap"] < 6 and x["date"] in state]
    tr = [x for x in rows if x["date"] < TRAIN_END]
    te = [x for x in rows if x["date"] >= TRAIN_END]
    print(f"接力样本 {len(rows)}（训练 {len(tr)} / 检验 {len(te)}），不加闸门：训练 次收{mean(x['x_close'] for x in tr):+.2f} "
          f"次开{mean(x['x_open'] for x in tr):+.2f}  检验 次收{mean(x['x_close'] for x in te):+.2f} 次开{mean(x['x_open'] for x in te):+.2f}")
    for feat in ("昨涨停股竞价均开%", "昨涨停股红开率%", "昨高标竞价均开%", "全市场竞价开盘中位%", "昨涨停家数", "昨跌停家数", "昨最高板"):
        vals = sorted(x[feat] for x in tr if x[feat] is not None)
        lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
        print(f"\n== {feat}（训练段三分位 {lo:.2f} / {hi:.2f}）==   次日收盘卖（次日开盘卖）")
        for seg, name in ((tr, "训练 2019-22"), (te, "检验 2023-26")):
            cells = []
            for lab, pick in (("低", lambda v: v < lo), ("中", lambda v: lo <= v < hi), ("高", lambda v: v >= hi)):
                g = [x for x in seg if x[feat] is not None and pick(x[feat])]
                cells.append(f"{lab} n={len(g):3} {mean(x['x_close'] for x in g):+5.2f}({mean(x['x_open'] for x in g):+5.2f})" if g else f"{lab} n=0")
            print(f"  {name}  " + "   ".join(cells))


if __name__ == "__main__":
    main()
