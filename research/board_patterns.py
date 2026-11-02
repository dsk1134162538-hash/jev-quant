"""二板（1 进 2）和龙回头的日线回测（dj 2026-11-02「我们可以做二板，或者龙回头」）。

样本同 first_board_history（腾讯日 K，主板、去 ST，2026-05 ~ 10）。所有买点只用当时能看到的价格：
二板：昨天首板封死（前 10 天没收过涨停），今天
    E1 竞价买 —— 开盘价买（一字开盘买不到，跳过）
    E2 扫二板 —— 盘中摸到 +9% 时买（开盘已 ≥9% 按开盘价），跟现在扫首板同口径
    E3 排二板 —— 摸到涨停价才买（买在涨停价；摸到后又炸开的也算买进）
龙回头：近 20 天里有过 ≥N 连板的龙头，从连板高点回撤 R% 以上（截至昨天收盘），今天
    L1 开盘买；L2 盘中涨到 +5% 买（反包确认）；L3 摸涨停才买（反包板）
卖出：次日开盘（主口径），另报次日收盘、持有 3 天收盘；都扣 0.15% 双边费用。
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean

from research import first_board_history as fb

FEE = fb.FEE


def _is_zt(rows, i) -> bool:
    return i >= 1 and rows[i][2] >= fb.limit_price(rows[i - 1][2])


def _exit(rows, i, entry) -> dict:
    """i = 买入当天的下标。"""
    out = {"next_open": round((rows[i + 1][1] / entry - 1) * 100 - FEE, 2),
           "next_close": round((rows[i + 1][2] / entry - 1) * 100 - FEE, 2)}
    if i + 3 < len(rows):
        out["hold3_close"] = round((rows[i + 3][2] / entry - 1) * 100 - FEE, 2)
    return out


def second_board(bars) -> dict[str, list[dict]]:
    trades = defaultdict(list)
    for code, rows in bars.items():
        for i in range(12, len(rows) - 1):
            if not _is_zt(rows, i - 1) or any(_is_zt(rows, j) for j in range(i - 11, i - 1)):
                continue
            d, o, c, h, lo, v = rows[i]
            pre = rows[i - 1][2]
            lim = fb.limit_price(pre)
            if o >= lim and lo >= lim:
                continue                                      # 一字二板买不到
            gap = round((o / pre - 1) * 100, 2)
            base = {"day": d, "code": code, "gap%": gap, "sealed": c >= lim}
            if o < lim:
                trades["E1 竞价买"].append({**base, **_exit(rows, i, o)})
            if h >= pre * 1.09:
                entry = o if o >= pre * 1.09 else round(pre * 1.09, 2)
                if entry < lim:
                    trades["E2 扫二板(9%)"].append({**base, **_exit(rows, i, entry)})
            if h >= lim and o < lim:
                trades["E3 排二板(涨停价)"].append({**base, **_exit(rows, i, lim)})
    return trades


def dragon_pullback(bars, min_streak: int = 3, min_drop: float = 15.0, window: int = 20) -> dict[str, list[dict]]:
    trades = defaultdict(list)
    for code, rows in bars.items():
        streak = [0] * len(rows)
        for i in range(1, len(rows)):
            streak[i] = streak[i - 1] + 1 if _is_zt(rows, i) else 0
        for i in range(window + 1, len(rows) - 1):
            lo_i = i - window
            peaks = [k for k in range(lo_i, i - 1) if streak[k] >= min_streak]
            if not peaks:
                continue
            top = max(rows[k][2] for k in range(peaks[0], i))
            prev_close = rows[i - 1][2]
            drop = (prev_close / top - 1) * 100
            if drop > -min_drop or _is_zt(rows, i - 1):
                continue
            d, o, c, h, lo, v = rows[i]
            pre = prev_close
            lim = fb.limit_price(pre)
            base = {"day": d, "code": code, "drop%": round(drop, 1), "maxStreak": max(streak[k] for k in peaks),
                    "gap%": round((o / pre - 1) * 100, 2)}
            if o < lim:
                trades["L1 开盘买"].append({**base, **_exit(rows, i, o)})
            if h >= pre * 1.05:
                entry = o if o >= pre * 1.05 else round(pre * 1.05, 2)
                if entry < lim:
                    trades["L2 涨到5%买"].append({**base, **_exit(rows, i, entry)})
            if h >= lim and o < lim:
                trades["L3 反包板"].append({**base, **_exit(rows, i, lim)})
    return trades


def summarize(trades: list[dict], days_split: str) -> dict:
    def seg(rows):
        if not rows:
            return None
        out = {"n": len(rows), "perDay": round(len(rows) / len({r["day"] for r in rows}), 1)}
        for k in ("next_open", "next_close", "hold3_close"):
            vals = [r[k] for r in rows if k in r]
            if vals:
                out[k] = round(mean(vals), 2)
                out[k + "_win"] = round(sum(x > 0 for x in vals) / len(vals), 2)
        return out
    return {"all": seg(trades), "train": seg([t for t in trades if t["day"] <= days_split]),
            "test": seg([t for t in trades if t["day"] > days_split])}
