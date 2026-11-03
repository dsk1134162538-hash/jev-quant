"""知识库（jevq/app/knowledge.json，Telegram @gsxt233 的整理）里能用日线验证的规则，7 年（2019 起）逐条回测。
dj 2026-11-03：「指标和各项规则结合」「知识库可以参考」。参数一律取知识库原文的数字，不调；每条规则的口径写在函数 doc 里。
只测日线能近似的部分——封板时间（10:30 前封死）、分时均价线、竞价量、盘口大单这些要分钟级数据，没法测。

    python -m research.kb_rules
"""
from __future__ import annotations

import pickle

import numpy as np
import pandas as pd

from research.classic_strats import FEE, matrices
from research.dragon_relay import ART

ERA = "2023"


def lim_of(pre: pd.DataFrame) -> pd.DataFrame:
    return (pre * 1.1 + 1e-9).round(2)


def report(name, mask, entry, X, years, note=""):
    """mask: 候选；entry: 'open'/'close'；X: {出场名: 收益%（已含 FEE）}。"""
    print(f"\n#### {name}  {note}")
    for ex, df in X.items():
        s = df.where(mask).stack().dropna()
        if len(s) < 30:
            print(f"  {ex:<8} n={len(s)}（太少）")
            continue
        d = s.index.get_level_values(0)
        yr = s.groupby(d.str[:4]).agg(["count", "mean"])
        a, b = s[d < ERA], s[d >= ERA]
        print(f"  {ex:<8} n={len(s):5}  均{s.mean():+6.2f}  中位{s.median():+6.2f}  胜{(s > 0).mean() * 100:3.0f}%  "
              f"2019-22 {a.mean():+5.2f}(n={len(a)})  2023- {b.mean():+5.2f}(n={len(b)})  逐年 "
              + " ".join(f"{y[2:]}:{r['mean']:+.1f}" for y, r in yr.iterrows() if r["count"] >= 5))


def main():
    dates, codes, M = matrices()
    C, O, H, L, V = (M[k] for k in "COHLV")
    fs = pickle.load(open(ART / "dr_cache.pkl", "rb"))[1]
    shares = pd.Series({c: fs.get(c, np.nan) for c in codes})
    pre = C.shift(1)
    lim = lim_of(pre)
    pct = C / pre - 1
    bad = ((pct.abs() > 0.106) | pct.isna()).astype(float)
    clean = bad.rolling(8).sum().shift(-3) == 0                  # 前 4 天到后 3 天都没有除权假跌幅
    zt = C >= lim
    touch = H >= lim
    zhaban = touch & ~zt
    dtlim = (pre * 0.9 + 1e-9).round(2)
    dt = C <= dtlim
    streak = np.zeros(C.shape)
    z = zt.fillna(False).to_numpy()
    for t in range(1, len(C)):
        streak[t] = (streak[t - 1] + 1) * z[t]
    streak = pd.DataFrame(streak, index=C.index, columns=C.columns)
    vr5 = V / V.rolling(5).mean().shift(1)                        # 当天量 / 前 5 日均量
    turn = V * 100 / shares * 100                                 # 换手率 %（现股本，近似）
    mcap = C.mul(shares, axis=1) / 1e8                            # 流通市值亿（现股本，近似）
    ma5, ma20 = C.rolling(5).mean(), C.rolling(20).mean()
    gap = O / pre - 1
    ok = (V > 0) & clean & C.notna()

    # 出场：开盘买 → 次日开盘 / 次日收盘；收盘买 → 次日开盘 / 次日收盘 / 第 3 天收盘
    f = lambda x, base: (x / base - 1) * 100 - FEE                # noqa: E731
    X_open = {"次开": f(O.shift(-1), O), "次收": f(C.shift(-1), O)}
    X_close = {"次开": f(O.shift(-1), C), "次收": f(C.shift(-1), C), "第3天收": f(C.shift(-3), C)}
    can_open = ok & (O < lim) & O.shift(-3).notna()               # 开盘价不在涨停（一字买不到）
    can_close = ok & ~zt & ~dt & C.shift(-3).notna()
    years = [d[:4] for d in dates]

    print(f"7 年日线 {dates[0]} ~ {dates[-1]}，{len(codes)} 只主板。FEE={FEE}%。2019-22 / 2023- 分段。")

    # ---- A. 一进二（知识库「首板和一进二」）----
    first = (streak.shift(1) == 1) & (streak.shift(2) == 0)       # 昨天是首板
    A_vol = (vr5.shift(1) >= 1.5) & (vr5.shift(1) <= 3.0)
    A_turn = (turn.shift(1) >= 10) & (turn.shift(1) <= 20)
    A_cap = (mcap.shift(1) >= 10) & (mcap.shift(1) <= 100)
    A_gap = (gap >= 0.03) & (gap < 0.07)
    base = can_open & first
    report("A0 昨日首板 + 竞价高开 3~7%（只有位置）", base & A_gap, "open", X_open, years)
    report("A1 + 量能 1.5~3 倍", base & A_gap & A_vol, "open", X_open, years)
    report("A2 + 量能 1.5~3 倍 + 换手 10~20%", base & A_gap & A_vol & A_turn, "open", X_open, years)
    report("A3 + 量能 + 换手 + 流通市值 10~100 亿（全套日线能测的条件）", base & A_gap & A_vol & A_turn & A_cap, "open", X_open, years,
           "股本用现在的，历史市值有偏差")

    # ---- B. 弱转强（知识库「弱转强」）：前一天烂板（摸过涨停没封住），次日竞价不一字、不顶涨停 ----
    for lab, prior in (("高标烂板（炸板前连板≥2）", streak.shift(2) >= 2), ("首板烂板（炸板前非连板）", streak.shift(2) == 0)):
        yest = zhaban.shift(1) & prior
        report(f"B 弱转强 {lab}：次日竞价高开 0~6%", can_open & yest & (gap >= 0) & (gap < 0.06), "open", X_open, years)
        report(f"B 弱转强 {lab}：次日竞价小幅低开 −3~0%（「小幅低开后翻红」）", can_open & yest & (gap >= -0.03) & (gap < 0), "open", X_open, years)

    # ---- C. 冰点修复（「冰点、修复和拐点」）：冰点 = 跌停≥10 家且炸板率≥40%；尾盘买，次日卖 ----
    ntouch, nzt, ndt = touch.sum(axis=1), zt.sum(axis=1), dt.sum(axis=1)
    zr = (ntouch - nzt) / ntouch.replace(0, np.nan)
    ice = (ndt >= 10) & (zr >= 0.4)
    ice2 = ice & ice.shift(1, fill_value=False)
    print(f"\n冰点日（跌停≥10 且 炸板率≥40%）共 {int(ice.sum())} 天，其中连续第二个冰点日 {int(ice2.sum())} 天；全部交易日 {len(dates)}")
    for lab, day_mask in (("冰点日", ice), ("连续第 2 个冰点日", ice2), ("全部交易日（对照）", pd.Series(True, index=dates))):
        m = can_close & pd.DataFrame(np.broadcast_to(day_mask.to_numpy()[:, None], C.shape), index=C.index, columns=C.columns)
        report(f"C {lab}：尾盘买入全市场等权，次日卖", m, "close", X_close, years)
    down = pct <= -0.03
    m = can_close & down & pd.DataFrame(np.broadcast_to(ice.to_numpy()[:, None], C.shape), index=C.index, columns=C.columns)
    report("C 冰点日 + 当天跌超 3% 的票（超跌）", m, "close", X_close, years)

    # ---- D. 情绪闸门（知识库刻度）× 高标接力 ----
    gate_ok = ((zr <= 0.30) & (ndt <= 10) & (streak.max(axis=1) >= 3))   # 昨日收盘的状态
    gate_y = pd.DataFrame(np.broadcast_to(gate_ok.shift(1, fill_value=False).to_numpy(dtype=bool)[:, None], C.shape),
                          index=C.index, columns=C.columns)
    relay = can_open & (streak.shift(1) >= 3) & (gap >= 0.03) & (gap < 0.06)
    report("D 高标接力（≥3板、竞价高开3~6%）全部", relay, "open", X_open, years)
    report("D 高标接力 × 知识库闸门通过（前一日炸板率≤30%、跌停≤10、最高板≥3）", relay & gate_y, "open", X_open, years)
    report("D 高标接力 × 闸门不通过", relay & ~gate_y, "open", X_open, years)

    # ---- E. 断板反包（「龙头与强势股」「低吸」）----
    # 断板日 b：前一天是 ≥3 连板的最后一板，b 当天没封住、跌幅 ≤3%、收盘不破 5 日线；
    # 之后调整 ≤3 天（b 本身算第 1 天）：b 之后每个调整日量 ≤ 断板日量的 70%、收盘不破 5 日线；
    # 反包日 q：阳线实体 ≥3%、量比前一天放大 1.3 倍、收盘 ≥5 日线。收盘买（收在涨停的买不到，剔除）。
    cond_day = (C > O * 1.03) & (V > V.shift(1) * 1.3) & (C >= ma5)
    E = pd.DataFrame(False, index=C.index, columns=C.columns)
    for j in (1, 2, 3):                                           # 断板日在反包日前 j 天
        b_ok = (streak.shift(j + 1) >= 3) & (~zt.shift(j, fill_value=False)) & (pct.shift(j) >= -0.03) & (C.shift(j) >= ma5.shift(j))
        calm = pd.DataFrame(True, index=C.index, columns=C.columns)
        for i in range(1, j):                                     # b 之后、q 之前的调整日
            calm &= (V.shift(i) <= V.shift(j) * 0.7) & (C.shift(i) >= ma5.shift(i))
        E |= b_ok & calm
    report("E 断板反包（高标≥3板，断板温和，调整≤3天缩量，反包阳线放量站上5日线）收盘买", can_close & E & cond_day, "close", X_close, years)

    # ---- F. 强势股回档（「强势股的回档买法」）：趋势向上、回调 1~3 天、缩量、收小 K 线、踩 5 日线 ----
    trend = (C > ma20) & (ma20 > ma20.shift(5)) & (C.shift(5) / C.shift(25) - 1 >= 0.10)
    d1 = (C < C.shift(1)) & ~(C.shift(1) < C.shift(2))                          # 回调第 1 天
    d2 = (C < C.shift(1)) & (C.shift(1) < C.shift(2)) & ~(C.shift(2) < C.shift(3))
    d3 = (C < C.shift(1)) & (C.shift(1) < C.shift(2)) & (C.shift(2) < C.shift(3)) & ~(C.shift(3) < C.shift(4))
    pull = d1 | d2 | d3
    small = ((C - O).abs() / pre <= 0.02)
    touch5 = (L <= ma5 * 1.01) & (C >= ma5 * 0.99)
    shrinkv = V < V.rolling(5).mean().shift(1) * 0.8
    F = trend & pull & small & touch5 & shrinkv
    report("F 强势股回档（趋势向上、回调1~3天、缩量、小K线、踩5日线）收盘买", can_close & F, "close", X_close, years)

    # ---- G. 对照：全市场任意一天收盘买 ----
    report("G 对照：任意票任意天，收盘买", can_close, "close", X_close, years)


if __name__ == "__main__":
    main()
