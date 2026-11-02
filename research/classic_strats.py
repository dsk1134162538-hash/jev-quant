"""线上低吸 / 超跌反弹两个策略的 7 年日线回测（dj 2026-11-03「做吧」）。

线上盘点（2026-09-24 ~ 11-02，一个月）：低吸 −8.9%/−14.6%，超跌反弹 −23%/−17.5%，情绪轮动 −15.8%，只有打首板 +6.7%（6 笔）。
这里用 2019 至今 3050 只主板票的日线，把两个策略的选股条件原样搬过来，看一个月的亏是运气还是策略本身：

低吸     当日涨幅 [-3%, 0]、截至昨天的 5 日涨幅 [+5%, +20%]、今天缩量（成交量比昨天小）。「主线题材」条件历史上没有概念成员，没法测。
超跌反弹 当日涨幅 [-3%, 0]、截至昨天的 5 日涨幅 [-20%, 0]、前 40 天没出现过 2 连板、连续缩量 ≥3 天；另测「大盘 5 日涨幅中位数 ≤ -2%」（主账户的环境条件）。

买点：当天收盘价（线上是尾盘 14:50 后或盘中，用收盘近似）。卖点用日线能近似的几种：次日开盘 / 次日收盘 / 持有两天收盘 /
「止损 -3%、止盈 +2%、都没碰就次日收盘」（同一天两个都碰到按止损算，偏悲观）。双边费用 0.15%。
日线不复权：除权日的假跌幅会污染 5 日涨幅，窗口里出现 |单日涨跌| > 10.6% 的整笔丢掉。ST 无法识别（涨跌幅 5%），有少量噪声。
线上是全仓单票：除了「每个候选的平均」，另算「每天只取排名第一的一只」。

    python -m research.classic_strats
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.dragon_relay import load

FEE = 0.15
STOP, TAKE = -0.03, 0.02
SPLIT = "2023-01-01"


def matrices():
    bars = load()
    dates = sorted({r[0] for rows in bars.values() for r in rows})
    codes = sorted(bars)
    idx = {d: i for i, d in enumerate(dates)}
    shape = (len(dates), len(codes))
    M = {k: np.full(shape, np.nan) for k in "OCHLV"}
    for j, code in enumerate(codes):
        for d, o, c, h, lo, v in bars[code]:
            i = idx[d]
            M["O"][i, j], M["C"][i, j], M["H"][i, j], M["L"][i, j], M["V"][i, j] = o, c, h, lo, v
    return dates, codes, {k: pd.DataFrame(v, index=dates, columns=codes) for k, v in M.items()}


def features(M):
    C, V = M["C"], M["V"]
    pct = C / C.shift(1) - 1
    bad = (pct.abs() > 0.106) | pct.isna()
    clean5 = bad.astype(float).rolling(6).sum().shift(1) == 0              # i-6..i-1 的 6 根都干净（算 5 日涨幅用）
    ret5 = (C.shift(1) / C.shift(6) - 1).where(clean5)
    zt = C >= (C.shift(1) * 1.1 + 1e-9).round(2)
    streak = np.zeros(C.shape)
    z = zt.fillna(False).to_numpy()
    for t in range(1, len(C)):
        streak[t] = (streak[t - 1] + 1) * z[t]
    excl = pd.DataFrame(streak, index=C.index, columns=C.columns).rolling(40).max().shift(1) >= 2
    dn = (V < V.shift(1)).to_numpy()
    shrink = np.zeros(C.shape)
    for t in range(1, len(C)):
        shrink[t] = (shrink[t - 1] + 1) * dn[t]
    return pct, ret5, excl, pd.DataFrame(shrink, index=C.index, columns=C.columns), ret5.median(axis=1)


def exits(M):
    C, O, H, L = M["C"], M["O"], M["H"], M["L"]
    O1, C1, H1, L1, C2 = O.shift(-1), C.shift(-1), H.shift(-1), L.shift(-1), C.shift(-2)
    stop, take = C * (1 + STOP), C * (1 + TAKE)
    br = C1.copy()
    br = br.where(~(H1 >= take), take)
    br = br.where(~(L1 <= stop), stop)
    br = br.where(~(O1 <= stop), O1)
    f = lambda x: (x / C - 1) * 100 - FEE                       # noqa: E731
    return {"次开": f(O1), "次收": f(C1), "两天收": f(C2), "止损3止盈2": f(br)}


def run(name, mask, rank, X, dates):
    """mask：候选；rank：越小越优先。输出：每候选平均 / 每天取第一名，逐年。"""
    cand = mask & X["次收"].notna()
    print(f"\n######## {name}  候选 {int(cand.sum().sum())} 笔，有候选的天数 {int((cand.sum(axis=1) > 0).sum())}")
    has = cand.any(axis=1)
    best = rank.where(cand)[has].idxmin(axis=1)                  # 每天排名第一的代码（只算有候选的天）
    for ex, df in X.items():
        pool = df.where(cand).stack().dropna()
        top = pd.Series({d: df.at[d, code] for d, code in best.items()}).dropna()
        by = pool.groupby(pool.index.get_level_values(0).str[:4]).agg(["count", "mean"])
        topy = top.groupby(top.index.str[:4]).agg(["count", "mean"])
        line = "  ".join(f"{y}:{by.at[y, 'mean']:+.2f}/{topy.at[y, 'mean']:+.2f}" for y in by.index if y in topy.index)
        pre, post = (pool[pool.index.get_level_values(0) < SPLIT], pool[pool.index.get_level_values(0) >= SPLIT])
        print(f"{ex:<10} 全期 候选均{pool.mean():+.2f} 胜{(pool > 0).mean() * 100:.0f}% | 每天第一名均{top.mean():+.2f} 胜{(top > 0).mean() * 100:.0f}% n={len(top)}"
              f" | 2019-22 {pre.mean():+.2f} 2023- {post.mean():+.2f}")
        print(f"{'':<10} 逐年(候选均/第一名均): {line}")


def main():
    dates, codes, M = matrices()
    pct, ret5, excl, shrink, mkt = features(M)
    X = exits(M)
    zone = (pct >= -0.03) & (pct <= 0.0)
    ok = zone & ret5.notna()
    low = ok & (ret5 >= 0.05) & (ret5 <= 0.20) & (shrink >= 1)
    run("低吸（缺主线题材条件）", low, -ret5, X, dates)
    reb = ok & (ret5 >= -0.20) & (ret5 <= 0.0) & (~excl) & (shrink >= 3)
    rank_reb = ret5 + (shrink < 4) * 10                           # 连续缩量 ≥4 天优先，同档 5 日跌得多的优先
    run("超跌反弹", reb, rank_reb, X, dates)
    weak = (mkt <= -0.02).to_numpy()[:, None]
    run("超跌反弹 + 大盘5日中位数≤-2%", reb & pd.DataFrame(np.broadcast_to(weak, reb.shape), index=reb.index, columns=reb.columns),
        rank_reb, X, dates)
    strong = ~weak
    run("超跌反弹 + 大盘5日中位数>-2%（对照）", reb & pd.DataFrame(np.broadcast_to(strong, reb.shape), index=reb.index, columns=reb.columns),
        rank_reb, X, dates)
    print(f"\n大盘5日中位数 ≤ -2% 的交易日占 {float(weak.mean()) * 100:.0f}%")


if __name__ == "__main__":
    main()
