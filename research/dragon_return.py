"""dj 的「龙回头 · 龙头版」通达信指标（2026-11-02 给的公式）翻成 Python，并做放宽版本对照。

只做沪深主板（10% 涨停）；ST 用当前名字过滤；CAPITAL（流通股本）用当前值近似；AMOUNT 用 日成交量(手)×100×收盘价 近似。
信号在当天收盘才确定（「今日转强」用收盘价）→ 两种买法：尾盘按收盘价买 / 次日开盘买。
卖出：次日开盘、次日收盘、持有 3 天收盘、持有 5 天收盘；都扣 0.15% 双边费用。
数据：research/artifacts/first_board/kline_2025.sqlite（2024-11 ~ 2026-05）+ kline.sqlite（2026-04 ~ 10），按日期合并。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean

from research import first_board_history as fb

FEE = fb.FEE


@dataclass
class Params:
    """严格版 = 公式原样；放宽版改这里。"""
    min_break: int = 2              # 断板天数下限
    max_break: int = 4              # 断板天数上限
    min_amount: float = 2e8         # 涨停日成交额
    min_turnover: float = 5.0       # 涨停日换手 %
    shrink: float = 0.6             # 昨天量 < 涨停日量 × shrink
    shrink_vs_prev: bool = True     # 还要求 昨天量 < 前天量
    wash_all: bool = True           # 断板≥3 时要求连续两根阴线（False=两根里有一根阴线即可）
    below_ma5: bool = True          # 要求昨天收盘跌破 5 日线
    pullback_lo: float = 0.6        # 回调比下限
    platform: float = 1.02          # 昨天实体下沿 ≥ 启动开盘 × platform
    name: str = "严格版"
    skip: set = field(default_factory=set)   # 直接去掉的条件名


def load_all_bars() -> dict[str, list[tuple]]:
    bars = defaultdict(dict)
    for db in (fb.ART / "kline_2025.sqlite", fb.KLINE_DB):
        conn = sqlite3.connect(db)
        for code, d, o, c, h, lo, v in conn.execute("SELECT code, date, open, close, high, low, volume FROM k"):
            bars[code][d] = (d, o, c, h, lo, v)
        conn.close()
    return {code: [rows[d] for d in sorted(rows)] for code, rows in bars.items()}


def _zt(rows, k) -> bool:
    return k >= 1 and rows[k][2] >= fb.limit_price(rows[k - 1][2]) and rows[k][2] >= rows[k][1]


def scan(rows, float_shares: float | None, p: Params, funnel: dict | None = None) -> list[dict]:
    """逐日算信号；funnel 记每一关通过的次数（看哪条最卡）。"""
    out = []
    zt = [_zt(rows, k) for k in range(len(rows))]
    last_zt = -1
    for i in range(len(rows)):
        if zt[i]:
            last_zt = i
        if i < 61 or last_zt < 0:
            continue
        k = last_zt
        brk = i - k
        checks = []
        checks.append(("断板天数", p.min_break <= brk <= p.max_break))
        d, o, c, h, lo, v = rows[i]
        ko, kc, kh, kl, kv = rows[k][1], rows[k][2], rows[k][3], rows[k][4], rows[k][5]
        amount_k = kv * 100 * kc
        turnover_k = kv * 100 / float_shares * 100 if float_shares else 0
        streak = 1
        for j in range(1, 5):
            if k - j >= 1 and zt[k - j]:
                streak += 1
            else:
                break
        checks.append(("龙头身份", amount_k > p.min_amount and turnover_k > p.min_turnover))
        y, yy = rows[i - 1], rows[i - 2]
        if brk >= 3 and p.wash_all:
            wash = yy[2] < yy[1] and y[2] < y[1]
        elif brk >= 3:
            wash = yy[2] < yy[1] or y[2] < y[1]
        else:
            wash = y[2] < y[1]
        checks.append(("洗盘形态", wash))
        shrink = y[5] < kv * p.shrink and (not p.shrink_vs_prev or y[5] < yy[5])
        checks.append(("缩量", shrink))
        ma5 = mean(r[2] for r in rows[i - 5:i])
        checks.append(("跌破5线", (not p.below_ma5) or y[2] < ma5))
        top = max(r[3] for r in rows[k:i + 1])
        ratio = y[4] / top if top else 0
        checks.append(("回调合理", p.pullback_lo < ratio < 1))
        checks.append(("守平台", min(y[1], y[2]) >= ko * p.platform and y[4] >= kl))
        checks.append(("今日转强", c > o and c >= y[2]))
        ok = True
        for name, passed in checks:
            if name in p.skip:
                continue
            if not passed:
                ok = False
                break
            if funnel is not None:
                funnel[name] = funnel.get(name, 0) + 1
        if not ok or i + 1 >= len(rows):
            continue
        sig = {"day": d, "brk": brk, "streak": streak, "amount亿": round(amount_k / 1e8, 2),
               "turnover%": round(turnover_k, 1), "pullback": round(ratio, 3), "todayPct": round((c / y[2] - 1) * 100, 2),
               "shrinkRatio": round(y[5] / kv, 2), "vsPlatform": round(min(y[1], y[2]) / ko, 3)}
        nx = rows[i + 1]
        sig["close→nextOpen"] = round((nx[1] / c - 1) * 100 - FEE, 2)
        sig["close→nextClose"] = round((nx[2] / c - 1) * 100 - FEE, 2)
        if i + 3 < len(rows):
            sig["close→hold3"] = round((rows[i + 3][2] / c - 1) * 100 - FEE, 2)
            sig["nextOpen→hold3"] = round((rows[i + 3][2] / nx[1] - 1) * 100 - FEE, 2)
        if i + 5 < len(rows):
            sig["close→hold5"] = round((rows[i + 5][2] / c - 1) * 100 - FEE, 2)
        out.append(sig)
    return out


def run(bars, float_shares, p: Params) -> tuple[list[dict], dict]:
    funnel = {}
    sigs = []
    for code, rows in bars.items():
        for s in scan(rows, float_shares.get(code), p, funnel):
            sigs.append({"code": code, **s})
    return sigs, funnel


# 「次日开盘买、次日收盘卖」T+1 做不到，不算
KEYS = ("close→nextOpen", "close→nextClose", "close→hold3", "close→hold5", "nextOpen→hold3")


def summary(sigs: list[dict]) -> dict:
    if not sigs:
        return {"n": 0}
    days = sorted({s["day"] for s in sigs})
    out = {"n": len(sigs), "signalDays": len(days)}
    for k in KEYS:
        vals = [s[k] for s in sigs if k in s]
        if vals:
            out[k] = f"{mean(vals):+.2f}% 胜{sum(x > 0 for x in vals) / len(vals):.0%}"
    return out
