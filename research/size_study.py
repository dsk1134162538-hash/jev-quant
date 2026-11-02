"""小市值效应的压力测试（dj 2026-11-03）。factor_weekly.py 里「流通市值最低 10%、持有 20 天」8 年里 7 年跑赢全市场，
这里检查它是不是真的、能不能买、有多大：

1. 十分位：从最小到最大是不是单调；
2. 换口径：用「60 日均成交额」代替「现在的股本×历史股价」（后者股本是现在的，历史上有偏差），两个口径结论是否一致；
3. 排除 ST 的近似（120 天里最大单日涨跌 ≤5.25% 的当 ST 丢掉，线上本来就不买 ST）、排除股价 < 3 元；
4. 实盘式组合：每 20 个交易日调仓一次，从最小 10% 里随机选 N 只等权，另扣 0.3% 滑点（小票买卖价差、买不满），
   画净值曲线：年化、最大回撤、逐年、最差一期；
5. 大盘趋势（等权指数在 60 日均线上/下）下的差别。

偏差（都让结果偏乐观，要打折）：名单是现在还在市的票，退市的没有（小盘股退市最集中）；股本用的是现在的。

    python -m research.size_study
"""
from __future__ import annotations

import pickle

import numpy as np
import pandas as pd

from research.classic_strats import FEE, matrices
from research.dragon_relay import ART

SLIP = 0.3          # 额外滑点 %，每笔（买卖合计）


def main():
    dates, codes, M = matrices()
    C, O, V = M["C"], M["O"], M["V"]
    pct = C / C.shift(1) - 1
    bad = ((pct.abs() > 0.106) | pct.isna()).astype(float)
    fs = pickle.load(open(ART / "dr_cache.pkl", "rb"))[1]
    size = C.mul(pd.Series({c: fs.get(c, np.nan) for c in codes}), axis=1)
    amount60 = (C * V * 100).rolling(60).mean()
    st_like = pct.abs().rolling(120).max() <= 0.0525
    zt = C >= (C.shift(1) * 1.1 + 1e-9).round(2)
    O1 = O.shift(-1)
    buyable = O1.notna() & (O1 > 0) & (O1 < (C * 1.1 + 1e-9).round(2))
    base_ok = (~zt) & (V > 0) & (bad.rolling(61).sum() == 0) & C.notna()
    years = np.array([d[:4] for d in dates])
    pos = {d: i for i, d in enumerate(dates)}
    index = (1 + pct.mean(axis=1).fillna(0)).cumprod()
    up = pd.Series((index > index.rolling(60).mean()).to_numpy(), index=dates)

    def fwd(h):
        fb = bad.rolling(h + 1).sum().shift(-(h + 1)) == 0
        r = (O.shift(-(1 + h)) / O1 - 1) * 100 - FEE
        return r, base_ok & buyable & r.notna() & fb

    def deciles(fac, ok, r, label):
        rk = fac.where(ok).rank(axis=1, pct=True)
        out = []
        for k in range(10):
            sel = (rk > k / 10) & (rk <= (k + 1) / 10)
            d = r.where(sel & ok).mean(axis=1).dropna()
            out.append(f"{d.mean():+.2f}")
        print(f"{label:<28}" + " ".join(out))

    r20, ok20 = fwd(20)
    print(f"全市场基准 20 天：{r20.where(ok20).mean(axis=1).mean():+.2f}%\n")
    print("== 1/2. 十分位（从最小到最大，持有 20 天，净收益%）==")
    print(f"{'':<28}" + " ".join(f"D{k + 1:<5}" for k in range(10)))
    deciles(size, ok20, r20, "现股本×历史股价")
    deciles(amount60, ok20, r20, "60日均成交额")
    okx = ok20 & ~st_like
    deciles(size, okx, r20, "现股本，排除ST近似")
    deciles(amount60, okx, r20, "成交额，排除ST近似")
    okp = okx & (C >= 3)
    deciles(size, okp, r20, "现股本，排ST且股价>=3")
    deciles(amount60, okp, r20, "成交额，排ST且股价>=3")

    print("\n== 3. 最小 10%（排除 ST 近似、股价>=3）逐年超额（20 天，对全市场等权）==")
    base = r20.where(ok20).mean(axis=1)
    for name, fac in (("现股本", size), ("60日成交额", amount60)):
        rk = fac.where(okp).rank(axis=1, pct=True)
        d = r20.where((rk <= 0.1) & okp).mean(axis=1).dropna()
        ex = d - base.reindex(d.index)
        yr_d, yr_e = d.groupby(d.index.str[:4]).mean(), ex.groupby(ex.index.str[:4]).mean()
        print(f"{name:<10} 每笔 {d.mean():+.2f}  超额 {ex.mean():+.2f}  逐年每笔: " + " ".join(f"{y[2:]}:{v:+.1f}" for y, v in yr_d.items())
              + f"   超额为正 {int((yr_e > 0).sum())}/{len(yr_e)}")
        upd, dnd = d[up.reindex(d.index).fillna(False)], d[~up.reindex(d.index).fillna(False)]
        print(f"{'':<10} 大盘在60日线上 {upd.mean():+.2f}（{len(upd)} 天）  线下 {dnd.mean():+.2f}（{len(dnd)} 天）")

    print(f"\n== 4. 实盘式组合：每 20 个交易日调仓，最小 10%（现股本，排ST且股价>=3）随机选 N 只等权，扣 {SLIP}% 额外滑点 ==")
    rk = size.where(okp).rank(axis=1, pct=True)
    pool = (rk <= 0.1) & okp
    rng = np.random.default_rng(7)
    arr_r, arr_pool = r20.to_numpy(), pool.to_numpy()
    for N in (10, 20, 40):
        results = []
        for off in range(0, 20, 4):                                   # 5 个起点偏移，看对调仓日的敏感度
            rets, when = [], []
            for t in range(60 + off, len(dates) - 22, 20):
                cand = np.flatnonzero(arr_pool[t] & ~np.isnan(arr_r[t]))
                if len(cand) < N:
                    continue
                pick = rng.choice(cand, N, replace=False)
                rets.append(arr_r[t, pick].mean() - SLIP)
                when.append(years[t])
            eq = np.cumprod(1 + np.array(rets) / 100)
            peak = np.maximum.accumulate(eq)
            n_years = len(rets) * 20 / 245
            results.append({"cagr": (eq[-1] ** (1 / n_years) - 1) * 100, "maxdd": ((eq / peak) - 1).min() * 100,
                            "mean": np.mean(rets), "win": np.mean(np.array(rets) > 0) * 100, "worst": np.min(rets),
                            "by_year": pd.Series(rets, index=when).groupby(level=0).sum()})
        by = pd.concat([x["by_year"] for x in results], axis=1).mean(axis=1)
        print(f"N={N:<3} 年化 {np.mean([x['cagr'] for x in results]):+.1f}%（各起点 "
              f"{min(x['cagr'] for x in results):+.0f}~{max(x['cagr'] for x in results):+.0f}）  最大回撤 {np.mean([x['maxdd'] for x in results]):.0f}%  "
              f"每期均 {np.mean([x['mean'] for x in results]):+.2f}%  胜率 {np.mean([x['win'] for x in results]):.0f}%  "
              f"最差一期 {np.mean([x['worst'] for x in results]):.1f}%")
        print(f"      逐年累加收益(%): " + " ".join(f"{y}:{v:+.0f}" for y, v in by.items()))


if __name__ == "__main__":
    main()
