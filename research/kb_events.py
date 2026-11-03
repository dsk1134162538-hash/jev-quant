"""知识库两条有苗头的规则按「事件」细看（kb_rules.py 里按股票×天算，同一天几千只票高度相关，样本数是虚的）：
1. 冰点修复：冰点日 = 跌停 ≥10 家且炸板率 ≥40%（知识库刻度）。收盘买入全市场等权，持有 1/3/5 天，按「冰点事件」逐个列出，
   并和「所有交易日」比；看事件数、平均、中位数、胜率、最差。
2. 高标烂板弱转强：昨天炸板（摸过涨停没封住）且炸板前 ≥2 连板，今天竞价高开 0~6%，开盘买，次日收盘卖。
   按交易日聚合（同一天几只等权），看有多少个独立交易日；再按「知识库闸门」（前一日炸板率≤30%、跌停≤10、最高板≥3）拆开。

    python -m research.kb_events
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.classic_strats import FEE, matrices


def main():
    dates, codes, M = matrices()
    C, O, H, V = M["C"], M["O"], M["H"], M["V"]
    pre = C.shift(1)
    lim = (pre * 1.1 + 1e-9).round(2)
    pct = C / pre - 1
    bad = ((pct.abs() > 0.106) | pct.isna()).astype(float)
    zt, touch = C >= lim, H >= lim
    dt = C <= (pre * 0.9 + 1e-9).round(2)
    ntouch, nzt, ndt = touch.sum(axis=1), zt.sum(axis=1), dt.sum(axis=1)
    zr = (ntouch - nzt) / ntouch.replace(0, np.nan)
    z = zt.fillna(False).to_numpy()
    st = np.zeros(C.shape)
    for t in range(1, len(C)):
        st[t] = (st[t - 1] + 1) * z[t]
    streak = pd.DataFrame(st, index=C.index, columns=C.columns)
    ice = (ndt >= 10) & (zr >= 0.4)
    ice2 = ice & ice.shift(1, fill_value=False)

    ok_close = (V > 0) & C.notna() & ~zt & ~dt & (bad.rolling(8).sum().shift(-5) == 0)
    print("== 1. 冰点修复（收盘买全市场等权，持有 h 天，扣费）==")
    print(f"冰点日 {int(ice.sum())} 天；连续第 2 个冰点日 {int(ice2.sum())} 天；全部 {len(dates)} 天")
    ew = {h: ((C.shift(-h) / C - 1) * 100 - FEE).where(ok_close & C.shift(-h).notna()).mean(axis=1) for h in (1, 3, 5)}
    for lab, m in (("所有交易日(对照)", pd.Series(True, index=dates)), ("冰点日", ice), ("连续第2个冰点日", ice2)):
        line = []
        for h, s in ew.items():
            v = s[m.reindex(s.index).fillna(False).to_numpy(dtype=bool)].dropna()
            line.append(f"持{h}天 事件{len(v):4} 均{v.mean():+5.2f} 中位{v.median():+5.2f} 胜{(v > 0).mean() * 100:3.0f}% 最差{v.min():+6.1f}")
        print(f"  {lab:<14}" + " | ".join(line))
    print("  连续第2个冰点日逐个事件（日期：买入当天全市场当日涨幅中位% → 持 1/3/5 天等权收益%）：")
    med = pct.median(axis=1) * 100
    for d in ice2[ice2].index:
        print(f"    {d}  当日中位{med[d]:+5.2f}  跌停{int(ndt[d]):3d} 炸板率{zr[d] * 100:4.0f}%   " +
              "  ".join(f"{h}天{ew[h].get(d, np.nan):+6.2f}" for h in (1, 3, 5)))

    print("\n== 2. 高标烂板弱转强（昨日炸板、炸板前≥2连板；今日竞价高开 0~6%；开盘买，次日收盘卖）==")
    gap = O / pre - 1
    yest = (touch & ~zt).shift(1, fill_value=False) & (streak.shift(2) >= 2)
    can = (V > 0) & (O < lim) & (gap >= 0) & (gap < 0.06) & C.shift(-1).notna() & (bad.rolling(5).sum().shift(-1) == 0)
    ret = ((C.shift(-1) / O - 1) * 100 - FEE).where(yest & can)
    gate_ok = ((zr <= 0.30) & (ndt <= 10) & (streak.max(axis=1) >= 3)).shift(1, fill_value=False)
    daily = ret.mean(axis=1).dropna()
    print(f"  共 {int(ret.notna().sum().sum())} 笔，分布在 {len(daily)} 个交易日（每天平均 {ret.notna().sum().sum() / len(daily):.1f} 笔）")
    for lab, mask in (("全部", pd.Series(True, index=daily.index)), ("闸门通过", gate_ok.reindex(daily.index)), ("闸门不通过", ~gate_ok.reindex(daily.index))):
        v = daily[mask.to_numpy(dtype=bool)]
        a, b = v[v.index < "2023"], v[v.index >= "2023"]
        print(f"  {lab:<8} 交易日{len(v):4}  日均{v.mean():+5.2f} 中位{v.median():+5.2f} 胜{(v > 0).mean() * 100:3.0f}%  "
              f"2019-22 {a.mean():+5.2f}(日数{len(a)})  2023- {b.mean():+5.2f}(日数{len(b)})")
    yr = daily.groupby(daily.index.str[:4]).agg(["count", "mean"])
    print("  逐年(日数,日均): " + " ".join(f"{y[2:]}:{int(r['count'])}d/{r['mean']:+.1f}" for y, r in yr.iterrows()))


if __name__ == "__main__":
    main()
