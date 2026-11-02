"""买点研究（dj 2026-11-02）：用 data/minutes.sqlite 里攒下的分时 + 9% 触线快照，看「在 9% 那一刻能看到的东西」
能不能分出会封住的板和会破的板，以及次日各种卖法（开盘 / 10 点 / 保本挂单 / +3% 挂单 / 收盘）的真实收益。

    python -m research.board_touch_study            # 打印分档报告

样本 = 某天摸到 9% 的主板票；买价 = 第一次 ≥9% 那一分钟的价格（不低于前收×1.09）。
次日分时要等第二天收盘存档后才有，所以最近一天的样本只有「封没封」没有收益。
分钟级：第一分钟就已封死的样本单独算（实盘买不到），不进分档。
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
MINUTES_DB = ROOT / "data" / "minutes.sqlite"
FEE = 0.15
TOUCH = 1.09


def _bars(conn) -> dict[tuple[str, str], list[tuple[str, float, int]]]:
    out = defaultdict(list)
    for d, code, hhmm, price, vol in conn.execute(
            "SELECT date, code, hhmm, price, cum_volume FROM minute_bars ORDER BY date, code, hhmm"):
        out[(d, code)].append((hhmm, price, vol))
    return out


def next_day_exits(entry: float, bars: list[tuple[str, float, int]]) -> dict[str, float]:
    """持有那天的分时 → 各卖法收益 %（扣费）。限价单按「之后某分钟价格 ≥ 目标价」成交。"""
    def r(px):
        return round((px / entry - 1) * 100 - FEE, 2)
    first, last = bars[0][1], bars[-1][1]
    at10 = next((p for h, p, _ in bars if h >= "10:00"), last)

    def limit(target):
        if first >= target:
            return first
        return target if any(p >= target for _, p, _ in bars) else last
    return {"开盘": r(first), "10点": r(at10), "收盘": r(last), "保本挂单": r(limit(entry)),
            "+3%挂单": r(limit(entry * 1.03))}


def samples(db: Path = MINUTES_DB) -> list[dict]:
    conn = sqlite3.connect(f"{Path(db).as_uri()}?mode=ro", uri=True)
    try:
        days = {(d, c): (pre, lim, high, close) for d, c, pre, lim, high, close in conn.execute(
            "SELECT date, code, pre_close, limit_up, high, close FROM minute_days")}
        snaps = {}
        try:
            for row in conn.execute("SELECT date, code, hhmm, sealed, price, ask_vols, bid_vols, turnover, "
                                    "float_mcap, market_sealed FROM touch_snaps"):
                snaps[(row[0], row[1])] = row
        except sqlite3.OperationalError:
            pass
        bars = _bars(conn)
    finally:
        conn.close()
    dates = sorted({d for d, _ in days})
    nxt = {d: dates[i + 1] for i, d in enumerate(dates[:-1])}
    out = []
    for (d, code), (pre, lim, high, close) in days.items():
        b = bars.get((d, code))
        if not b or pre <= 0 or high < pre * TOUCH - 1e-9:
            continue
        i9 = next((i for i, (_, p, _) in enumerate(b) if p >= pre * TOUCH - 1e-9), None)
        if i9 is None:
            continue
        i7 = next(i for i, (_, p, _) in enumerate(b) if p >= pre * 1.07 - 1e-9)
        hhmm, p9, vol9 = b[i9]
        entry = max(p9, round(pre * TOUCH, 2))
        s = {"date": d, "code": code, "touch": hhmm, "speed7to9": i9 - i7,
             "instantSeal": p9 >= lim, "sealed": close >= lim, "volAtTouch": vol9,
             "volShare": round(vol9 / b[-1][2], 3) if b[-1][2] else None}
        if (snap := snaps.get((d, code))) is not None:
            asks = json.loads(snap[5] or "[]")
            bids = json.loads(snap[6] or "[]")
            s.update(askAmt万=round(sum(asks) * 100 * snap[4] / 1e4, 1),
                     bidAskRatio=round(sum(bids) / sum(asks), 2) if sum(asks) else None,
                     turnover=snap[7], floatMcap=snap[8], marketSealed=snap[9], instantSeal=bool(snap[3]))
        follow = bars.get((nxt.get(d), code)) if d in nxt else None
        if follow:
            s["exits"] = next_day_exits(entry, follow)
        out.append(s)
    return out


def _bucket_line(label: str, xs: list[dict]) -> str:
    if not xs:
        return f"{label:<14} n=   0"
    seal = sum(x["sealed"] for x in xs) / len(xs) * 100
    with_exit = [x for x in xs if "exits" in x]
    rets = "  ".join(f"{k}{mean(x['exits'][k] for x in with_exit):+.2f}" for k in with_exit[0]["exits"]) \
        if with_exit else "（还没有次日分时）"
    return f"{label:<14} n={len(xs):4}  封住率{seal:5.1f}%  {rets}"


def report(rows: list[dict]) -> str:
    buyable = [x for x in rows if not x["instantSeal"]]
    lines = [f"样本 {len(rows)}（{len({x['date'] for x in rows})} 天），一分钟内直接封死买不到 {len(rows) - len(buyable)}",
             _bucket_line("能买到的全部", buyable)]
    groups = {
        "触线时间": lambda x: ("09:30-09:45" if x["touch"] < "09:45" else "09:45-10:00" if x["touch"] < "10:00"
                           else "10:00-10:30" if x["touch"] < "10:30" else "10:30 以后"),
        "7%→9% 用时": lambda x: "≤1 分钟" if x["speed7to9"] <= 1 else "2-5 分钟" if x["speed7to9"] <= 5 else "5 分钟以上",
        "卖五档金额": lambda x: None if x.get("askAmt万") is None else
        ("<300万" if x["askAmt万"] < 300 else "300-1000万" if x["askAmt万"] < 1000 else "≥1000万"),
        "买/卖五档比": lambda x: None if x.get("bidAskRatio") is None else
        ("<1" if x["bidAskRatio"] < 1 else "1-3" if x["bidAskRatio"] < 3 else "≥3"),
        "当时全市场封板数": lambda x: None if x.get("marketSealed") is None else
        ("<20" if x["marketSealed"] < 20 else "20-50" if x["marketSealed"] < 50 else "≥50"),
    }
    for title, key in groups.items():
        lines.append(f"\n— {title} —")
        by = defaultdict(list)
        for x in buyable:
            if (k := key(x)) is not None:
                by[k].append(x)
        lines += [_bucket_line(k, v) for k, v in sorted(by.items())]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(samples()))
