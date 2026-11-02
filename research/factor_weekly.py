"""慢节奏因子回测（dj 2026-11-03「总之你去想办法」）：持有 5 / 10 / 20 个交易日，手续费占比远小于日频。

信号用 t 日收盘前的数据；次日（t+1）开盘买，t+1+h 日开盘卖（开盘就涨停的票买不到，剔除）。
8 个因子 × 最低/最高 10% 两端 × 3 个持有期 = 48 格，只是扫方向，挑最好的格子有多重比较偏差；
并且只比较「相对全市场等权基准的超额」是否逐年稳定，绝对收益里混有大盘涨跌和幸存者偏差（名单是现在还在市的票）。
窗口里出现 |单日涨跌| > 10.6% 的票丢掉（除权假跌幅 / 新股前几天，主板正常交易日不可能出现）。

    python -m research.factor_weekly
"""
from __future__ import annotations

import pickle

import numpy as np
import pandas as pd

from research.classic_strats import FEE, matrices
from research.dragon_relay import ART

HORIZONS = (5, 10, 20)


def main():
    dates, codes, M = matrices()
    C, O, V = M["C"], M["O"], M["V"]
    pct = C / C.shift(1) - 1
    bad = ((pct.abs() > 0.106) | pct.isna()).astype(float)
    try:
        float_shares = pickle.load(open(ART / "dr_cache.pkl", "rb"))[1]
    except Exception:                                          # noqa: BLE001
        float_shares = {}
    size = C.mul(pd.Series({c: float_shares.get(c, np.nan) for c in codes}), axis=1)
    zt = C >= (C.shift(1) * 1.1 + 1e-9).round(2)
    clean60 = bad.rolling(61).sum() == 0                       # 近 60 天没有假跌幅
    base_ok = (~zt) & (V > 0) & clean60 & C.notna()
    factors = {
        "5日涨幅": C / C.shift(5) - 1,
        "20日涨幅": C / C.shift(20) - 1,
        "60日动量(跳过近5日)": C.shift(5) / C.shift(60) - 1,
        "20日波动率": pct.rolling(20).std(),
        "流通市值(现股本)": size,
        "近5日量/60日均量": V.rolling(5).mean() / V.rolling(60).mean(),
        "距60日高点": C / C.rolling(60).max() - 1,
        "股价": C,
    }
    limit_next = (C * 1.1 + 1e-9).round(2)
    O1 = O.shift(-1)
    buyable = O1.notna() & (O1 > 0) & (O1 < limit_next)
    years = np.array([d[:4] for d in dates])
    index_ret = pct.mean(axis=1)
    index = (1 + index_ret.fillna(0)).cumprod()
    uptrend = (index > index.rolling(60).mean()).to_numpy()

    for h in HORIZONS:
        fwd_bad = bad.rolling(h + 1).sum().shift(-(h + 1)) == 0
        ret = (O.shift(-(1 + h)) / O1 - 1) * 100 - FEE
        ok = base_ok & buyable & ret.notna() & fwd_bad
        base = ret.where(ok).mean(axis=1)
        print(f"\n######## 持有 {h} 天（扣 {FEE}% 双边）  基准（等权全市场）每笔 {base.mean():+.2f}%  "
              f"2019-22 {base[base.index < '2023'].mean():+.2f}  2023- {base[base.index >= '2023'].mean():+.2f}")
        print(f"{'因子':<18}{'端':<5}{'每笔':>7}{'超额':>7}{'2019-22':>9}{'2023-':>8}  超额为正的年数  逐年超额")
        for name, fac in factors.items():
            f = fac.where(ok)
            rk = f.rank(axis=1, pct=True)
            for side, sel in (("最低10%", rk <= 0.1), ("最高10%", rk > 0.9)):
                daily = ret.where(sel & ok).mean(axis=1).dropna()
                ex = (daily - base.reindex(daily.index))
                yr = ex.groupby(ex.index.str[:4]).mean()
                print(f"{name:<18}{side:<6}{daily.mean():>+7.2f}{ex.mean():>+7.2f}"
                      f"{daily[daily.index < '2023'].mean():>+9.2f}{daily[daily.index >= '2023'].mean():>+8.2f}"
                      f"  {int((yr > 0).sum())}/{len(yr)}  " + " ".join(f"{v:+.2f}" for v in yr.values))
        up = pd.Series(uptrend, index=dates)
        print(f"  基准按大盘趋势拆分（等权指数在 60 日均线上/下）：上 {base[up.reindex(base.index).fillna(False)].mean():+.2f}  "
              f"下 {base[~up.reindex(base.index).fillna(False)].mean():+.2f}  "
              f"（在均线上的天数占 {up.reindex(base.index).fillna(False).mean() * 100:.0f}%）")


if __name__ == "__main__":
    main()
