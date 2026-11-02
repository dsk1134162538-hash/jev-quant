"""hi189 的通达信指标（dj 2026-11-02 贴的源码）翻成 Python 并回测。

函数口径按通达信：MA 简单均线；EMA(X,N) Y=(2X+(N-1)Y')/(N+1)；SMA(X,N,M) Y=(MX+(N-M)Y')/N；
KDJ(9,3,3)；MACD(12,26,9) MACD=2*(DIF-DEA)；CROSS(A,B)=昨天 A<B 且今天 A>B；FILTER(X,N)=信号后 N 根内不再出；
REVERSE(X)=-X。近似：AMOUNT = VOL × (H+L+C)/3（腾讯日 K 没有成交额），所以 AMOUNT/VOL/100 ≈ 当日均价。
样本：research/artifacts/first_board 的日 K（2024-11 ~ 2026-10，主板去 ST），信号收盘才确定 → 按收盘价买。
"""
from __future__ import annotations

from statistics import mean

import numpy as np

FEE = 0.15


# ---------- 通达信函数 ----------

def MA(x, n):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def EMA(x, n):
    out = np.empty(len(x))
    a = 2 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def SMA(x, n, m):
    out = np.empty(len(x))
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = (m * x[i] + (n - m) * out[i - 1]) / n
    return out


def HHV(x, n):
    return np.array([np.max(x[max(0, i - n + 1):i + 1]) for i in range(len(x))])


def LLV(x, n):
    return np.array([np.min(x[max(0, i - n + 1):i + 1]) for i in range(len(x))])


def REF(x, n):
    out = np.full(len(x), np.nan)
    out[n:] = x[:-n] if n else x
    return out


def CROSS(a, b):
    out = np.zeros(len(a), bool)
    out[1:] = (a[:-1] < b[:-1]) & (a[1:] > b[1:])
    return out


def FILTER(x, n):
    out = np.zeros(len(x), bool)
    block = -1
    for i, v in enumerate(x):
        if v and i > block:
            out[i] = True
            block = i + n
    return out


def EVERY(x, n):
    return np.array([bool(np.all(x[max(0, i - n + 1):i + 1])) and i >= n - 1 for i in range(len(x))])


def COUNT(x, n):
    c = np.cumsum(np.insert(x.astype(int), 0, 0))
    return np.array([c[i + 1] - c[max(0, i - n + 1)] for i in range(len(x))])


def KDJ(c, h, l, n=9):
    rsv = (c - LLV(l, n)) / np.where(HHV(h, n) - LLV(l, n) == 0, np.nan, HHV(h, n) - LLV(l, n)) * 100
    rsv = np.nan_to_num(rsv, nan=50.0)
    k = SMA(rsv, 3, 1)
    d = SMA(k, 3, 1)
    return k, d, 3 * k - 2 * d


# ---------- 指标 ----------

def youzi(o, c, h, l, v) -> dict[str, np.ndarray]:
    """游资突破共振（选股公式版）。"""
    tp = (h + l + c) / 3
    g12 = REF(EMA(tp, 10), 1)
    g14 = EMA(LLV(l, 10), 3)
    qk = EMA(np.nan_to_num(g12 * 2 - g14, nan=c[0]), 8)
    pct = (c / REF(c, 1) - 1) * 100
    g16 = (o < qk) & (c > qk) & (pct > 7)
    g31 = CROSS(c, EMA(h, 3) * 1.03) & ((c - REF(c, 1)) / REF(c, 1) > 0.04)
    ambush = g31 & g16
    newhigh = (c >= HHV(h, 60)) & (v > MA(v, 10) * 1.5)
    bull = (MA(c, 5) > MA(c, 10)) & (MA(c, 10) > MA(c, 20)) & (MA(c, 20) > MA(c, 60))
    return {"游资·选股信号": ambush & newhigh & bull & (v > MA(v, 20) * 1.8),
            "游资·伏击": ambush, "游资·共振(伏击+新高)": ambush & newhigh}


def leiting(o, c, h, l, v) -> dict[str, np.ndarray]:
    """雷霆超短（低吸抄底，尾盘买、次日冲高走）。"""
    ma10 = MA(c, 10)
    qj = 100 * (l / ma10) - 100
    tp = (h + l + c) / 3
    amount = v * tp
    m1r = tp
    m10r = MA(amount, 10) / MA(v, 10)
    cd = (l - m10r) / m10r * 100
    cd2 = (m1r - m10r) / m10r * 100
    jf = (m1r - ma10) / ma10 * 100
    fc = (jf - HHV(np.nan_to_num(jf, nan=0.0), 10)) / 2
    bbb = (l - MA(l, 10)) / MA(l, 10) * 100
    hhh = (bbb + MA(np.nan_to_num(bbb, nan=0.0), 10)) / 4
    return {"雷霆超短": (cd < qj) & (qj < cd2) & (cd2 < jf) & (cd < -2) & (hhh < -4) & (fc < -8) & (jf < -8)}


def p5463(o, c, h, l, v) -> dict[str, np.ndarray]:
    """涨停拉升（P5463）：拉升 + 关注。"""
    up2 = (c / REF(c, 1) > 1.05) & (h / c < 1.01) & (c > REF(c, 1))
    up3 = FILTER(up2, 45)
    _, _, j = KDJ(c, h, l)
    sign1 = CROSS(j, -j) & (REF(c, 10) > REF(MA(c, 60), 10))
    return {"P5463·拉升关注": up3 & sign1, "P5463·拉升": up3, "P5463·关注": sign1,
            "P5463·关注第4次(SIGN2)": sign1 & (COUNT(sign1, 60) == 4)}


def BARSLAST(x):
    out = np.full(len(x), np.nan)
    last = None
    for i, v in enumerate(x):
        if v:
            last = i
        if last is not None:
            out[i] = i - last
    return out


def p5488(o, c, h, l, v) -> dict[str, np.ndarray]:
    """2026 精品龙回头（P5488 副图，主板 10%）。"""
    pre = REF(c, 1)
    zt = (c >= np.round(pre * 1.1 + 1e-9, 2)) & (c >= o)
    brk = BARSLAST(zt)
    n = len(c)
    start_open = np.array([o[i - int(brk[i])] if not np.isnan(brk[i]) else np.nan for i in range(n)])
    xrzl = REF((o > c).astype(float), 2).astype(bool) & (REF(o, 2) > REF(c, 3)) & (REF(o / c, 2) < 1.05)
    wash = REF((o > c).astype(float), 1).astype(bool)
    gap = h / pre >= 1.005
    shrink = v < REF(v, 1)
    below8 = REF(c, 1) < MA(c, 8)
    floor = REF(np.minimum(o, c), 1) >= REF(start_open, 1) * 1.02
    sig = (brk >= 2) & (brk <= 10) & xrzl & wash & gap & shrink & below8 & floor
    hot = sig & (c > MA(c, 5))
    return {"P5488·龙回头": sig, "P5488·情绪高潮(收>5日线)": hot, "P5488·情绪冰点(收<=5日线)": sig & ~hot}


INDICATORS = (youzi, leiting, p5463, p5488)


def exits(rows, i) -> dict:
    """收盘价买 → 几种 T+1 卖法（扣 0.15% 费用）。"""
    c = rows[i][2]
    nx = rows[i + 1]
    out = {"次日开盘": (nx[1] / c - 1) * 100 - FEE, "次日收盘": (nx[2] / c - 1) * 100 - FEE}
    for t in (2, 3):
        target = c * (1 + t / 100)
        px = max(nx[1], target) if nx[3] >= target else nx[2]       # 高开过目标按开盘价卖
        out[f"冲高{t}%走"] = (px / c - 1) * 100 - FEE
    out["作者口径·次日最高"] = (nx[3] / c - 1) * 100
    if i + 3 < len(rows):
        out["持3天收盘"] = (rows[i + 3][2] / c - 1) * 100 - FEE
    return out


def run(bars) -> dict[str, list[dict]]:
    trades: dict[str, list[dict]] = {}
    for code, rows in bars.items():
        if len(rows) < 80:
            continue
        o, c, h, l, v = (np.array([r[k] for r in rows], float) for k in (1, 2, 3, 4, 5))
        for fn in INDICATORS:
            with np.errstate(all="ignore"):
                sigs = fn(o, c, h, l, v)
            for name, s in sigs.items():
                for i in np.nonzero(np.nan_to_num(s, nan=0).astype(bool))[0]:
                    if i < 61 or i + 1 >= len(rows):
                        continue
                    trades.setdefault(name, []).append({"day": rows[i][0], "code": code, **exits(rows, i)})
    return trades


def summary(trades: list[dict], split: str) -> dict:
    def seg(rs):
        if not rs:
            return None
        out = {"n": len(rs), "天数": len({r["day"] for r in rs})}
        for k in ("作者口径·次日最高", "次日开盘", "次日收盘", "冲高2%走", "冲高3%走", "持3天收盘"):
            vals = [r[k] for r in rs if k in r]
            if vals:
                out[k] = f"{mean(vals):+.2f}% 胜{sum(x > 0 for x in vals) / len(vals):.0%}"
        return out
    return {"训练": seg([t for t in trades if t["day"] <= split]), "验证": seg([t for t in trades if t["day"] > split])}
