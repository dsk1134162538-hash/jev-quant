"""全市场日线因子扫描（dj 2026-11-03）：8 个常见因子，每天按因子把票分 10 档，看最低档 / 最高档收盘买、
次日开盘或收盘卖的平均净收益（扣 0.15% 双边），逐年看稳不稳。一共 8 因子 × 2 端 × 2 卖法 = 32 个格子，
扫的是「有没有值得深挖的方向」，不是拿来直接上线——挑最好的那一格出结论有多重比较偏差，必须再用别的口径复验。

排除：收盘涨停（买不到）、停牌、窗口内有除权假跌幅的票。

    python -m research.factor_scan
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.classic_strats import FEE, features, matrices


def main():
    dates, codes, M = matrices()
    C, O, H, L, V = (M[k] for k in "COHLV")
    pct, ret5, excl, shrink, mkt = features(M)
    zt = C >= (C.shift(1) * 1.1 + 1e-9).round(2)
    ok = (~zt) & (V > 0) & pct.notna() & (pct.abs() <= 0.106) & ret5.notna()
    ret20 = (C.shift(1) / C.shift(21) - 1).where(ok)
    f = {
        "1日涨幅(反转)": pct,
        "5日涨幅(反转)": ret5 + pct,                       # 含今天的近 5 日
        "20日涨幅": ret20,
        "缩量比 V/20日均量": V / V.rolling(20).mean(),
        "日内振幅": (H - L) / C.shift(1),
        "收盘位置 (C-L)/(H-L)": (C - L) / (H - L).replace(0, np.nan),
        "上影线 (H-C)/C": (H - C) / C,
        "20日波动率": pct.rolling(20).std(),
    }
    nxt_open, nxt_close = (M["O"].shift(-1) / C - 1) * 100 - FEE, (C.shift(-1) / C - 1) * 100 - FEE
    ok &= nxt_open.notna() & nxt_close.notna()
    years = pd.Index([d[:4] for d in dates])
    base_o, base_c = nxt_open.where(ok).mean(axis=1), nxt_close.where(ok).mean(axis=1)
    print(f"全市场基准（等权买入全部可买的票）次开 {base_o.mean():+.3f}  次收 {base_c.mean():+.3f}   单位 % / 笔（已扣费）")
    print(f"{'因子':<22}{'端':<4}{'卖法':<4}{'全期':>8}{'2019-22':>9}{'2023-':>8}  逐年（只数正的年数/8）")
    for name, fac in f.items():
        fac = fac.where(ok)
        rk = fac.rank(axis=1, pct=True)
        for side, sel in (("低10%", rk <= 0.1), ("高10%", rk > 0.9)):
            for exit_name, X in (("次开", nxt_open), ("次收", nxt_close)):
                daily = X.where(sel & ok).mean(axis=1).dropna()
                yr = daily.groupby(years[daily.index.map(dates.index)] if False else daily.index.str[:4]).mean()
                pre = daily[daily.index < "2023"].mean()
                post = daily[daily.index >= "2023"].mean()
                print(f"{name:<22}{side:<5}{exit_name:<4}{daily.mean():>+8.3f}{pre:>+9.3f}{post:>+8.3f}  "
                      f"{int((yr > 0).sum())}/{len(yr)}  " + " ".join(f"{v:+.2f}" for v in yr.values))


if __name__ == "__main__":
    main()
