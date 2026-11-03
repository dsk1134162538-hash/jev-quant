"""题材梯队、龙头、补涨、题材阶段（dj 2026-11-03）：用同花顺涨停揭秘一年真实的涨停原因题材（theme_history.py）做回测。

买点统一：昨天（d）涨停的票，今天（d1）竞价开盘价买（开在涨停买不到），次日收盘卖 / 次日开盘卖，扣 0.15%。只买沪深主板，
但题材统计用全部板块的涨停（创业板、科创板的高度也是题材高度的一部分）。每只票可以同时属于多个题材（reason 用 + 分隔），
取「当天涨停家数最多的那个题材」作为它的主题材。训练 = 2026-06-01 之前，检验 = 之后；样本只有一年，分到各格子都不大，结论只当线索。

    python -m research.theme_ladder
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from statistics import mean, median

from jevq.data.ztpool import _NOT_THEME, _SPLIT
from research.dragon_relay import ART, FEE, lim, load

SPLIT = "2026-06-01"


def parse_boards(high_days: str) -> int:
    m = re.match(r"(\d+)天(\d+)板", high_days or "")
    return int(m.group(2)) if m else 1


def themes_of(reason: str) -> list[str]:
    return [t.strip() for t in _SPLIT.split(reason or "") if t.strip() and not _NOT_THEME.search(t)]


def fmt(v: list[float]) -> str:
    return "n=0" if not v else (f"n={len(v):4}  均{mean(v):+6.2f}  中位{median(v):+6.2f}  胜{sum(x > 0 for x in v) / len(v) * 100:3.0f}%")


def main():
    conn = sqlite3.connect(ART / "theme_zt.sqlite")
    rows = conn.execute("SELECT date, code, reason, high_days FROM zt").fetchall()
    conn.close()
    by_date = defaultdict(list)
    for d, code, reason, hd in rows:
        by_date[d].append({"code": code, "boards": parse_boards(hd), "themes": themes_of(reason)})
    dates_zt = sorted(by_date)

    n_theme = {d: defaultdict(int) for d in dates_zt}                 # 题材当天涨停家数
    max_b = {d: defaultdict(int) for d in dates_zt}                   # 题材当天最高连板
    levels = {d: defaultdict(set) for d in dates_zt}                  # 题材当天出现的连板层级（1 / 2 / 3+）
    for d in dates_zt:
        for r in by_date[d]:
            for t in r["themes"]:
                n_theme[d][t] += 1
                max_b[d][t] = max(max_b[d][t], r["boards"])
                levels[d][t].add(min(r["boards"], 3))

    bars = load()
    idx = {code: {r[0]: i for i, r in enumerate(rows_)} for code, rows_ in bars.items()}
    cal = sorted({r[0] for rows_ in bars.values() for r in rows_})
    nxt = {d: cal[i + 1] for i, d in enumerate(cal[:-1])}
    prev_zt = {d: dates_zt[i - 1] for i, d in enumerate(dates_zt) if i}

    samples = []
    for d in dates_zt:
        d1 = nxt.get(d)
        if d1 is None or nxt.get(d1) is None:
            continue
        for r in by_date[d]:
            code = r["code"]
            if not code.startswith(("60", "00")) or code not in idx or d not in idx[code] or d1 not in idx[code]:
                continue
            b = bars[code]
            i0, i1 = idx[code][d], idx[code][d1]
            if i1 + 1 >= len(b):
                continue
            c0, o1 = b[i0][2], b[i1][1]
            if c0 <= 0 or o1 <= 0 or o1 >= lim(c0):
                continue
            ts = [t for t in r["themes"] if t in n_theme[d]]
            if not ts:
                continue
            main = max(ts, key=lambda t: (n_theme[d][t], max_b[d][t]))
            pd_ = prev_zt.get(d)
            samples.append({
                "date": d1, "code": code, "boards": r["boards"], "gap": (o1 / c0 - 1) * 100,
                "n": n_theme[d][main], "maxb": max_b[d][main], "levels": len(levels[d][main]),
                "n_prev": n_theme[pd_].get(main, 0) if pd_ else 0,
                "leader": r["boards"] >= max_b[d][main], "x_close": (b[i1 + 1][2] / o1 - 1) * 100 - FEE,
                "x_open": (b[i1 + 1][1] / o1 - 1) * 100 - FEE})
    print(f"涨停揭秘 {len(dates_zt)} 个交易日（{dates_zt[0]} ~ {dates_zt[-1]}），主板可买样本 {len(samples)}；训练 < {SPLIT} ≤ 检验")

    def show(title, pick):
        sel = [s for s in samples if pick(s)]
        tr, te = [s for s in sel if s["date"] < SPLIT], [s for s in sel if s["date"] >= SPLIT]
        print(f"  {title:<44} 训练 {fmt([s['x_close'] for s in tr])} | 检验 {fmt([s['x_close'] for s in te])}   （次开：训练 {mean([s['x_open'] for s in tr]) if tr else float('nan'):+.2f} 检验 {mean([s['x_open'] for s in te]) if te else float('nan'):+.2f}）")

    print("\n== 1. 题材梯队：一进二（昨天首板、竞价高开 3~7%），按主题材的板块效应分 ==   次日收盘卖")
    base = lambda s: s["boards"] == 1 and 3 <= s["gap"] < 7                      # noqa: E731
    show("不加题材过滤", base)
    for k in (2, 3, 5, 8):
        show(f"主题材昨日涨停 ≥{k} 家", lambda s, k=k: base(s) and s["n"] >= k)
    show("主题材有二板以上（有先锋）", lambda s: base(s) and s["maxb"] >= 2)
    show("主题材 ≥3 家且有二板以上", lambda s: base(s) and s["n"] >= 3 and s["maxb"] >= 2)
    show("主题材 ≥5 家、梯队 ≥3 层（首板/二板/三板+）", lambda s: base(s) and s["n"] >= 5 and s["levels"] >= 3)
    show("主题材涨停家数比前一天增加（发酵）", lambda s: base(s) and s["n"] > s["n_prev"] >= 1)
    show("主题材涨停家数比前一天减少（分歧）", lambda s: base(s) and 0 < s["n"] < s["n_prev"])

    print("\n== 2. 龙头识别：同样连板数，是不是题材里的最高板 ==   竞价高开 3~6%，次日收盘卖")
    for b in (2, 3, 4):
        show(f"{b} 板 · 是题材最高板（龙头）", lambda s, b=b: s["boards"] == b and 3 <= s["gap"] < 6 and s["leader"])
        show(f"{b} 板 · 不是最高板（跟风/夹在中间）", lambda s, b=b: s["boards"] == b and 3 <= s["gap"] < 6 and not s["leader"])
    show("5 板以上 · 龙头", lambda s: s["boards"] >= 5 and 3 <= s["gap"] < 6 and s["leader"])

    print("\n== 3. 补涨：主题材龙头已 ≥5 板，同题材的首板，次日竞价买 ==   次日收盘卖")
    for lo, hi in ((0, 3), (3, 7), (0, 7)):
        show(f"首板 · 龙头≥5板的题材 · 高开 {lo}~{hi}%", lambda s, lo=lo, hi=hi: s["boards"] == 1 and s["maxb"] >= 5 and lo <= s["gap"] < hi)
    show("首板 · 龙头 3~4 板的题材（对照）· 高开 0~7%", lambda s: s["boards"] == 1 and 3 <= s["maxb"] <= 4 and 0 <= s["gap"] < 7)
    show("首板 · 龙头 ≤2 板的题材（对照）· 高开 0~7%", lambda s: s["boards"] == 1 and s["maxb"] <= 2 and 0 <= s["gap"] < 7)

    print("\n== 4. 题材阶段：同一类买法（昨日涨停、竞价高开 3~7%）在不同阶段 ==   次日收盘卖")
    gap_ok = lambda s: 3 <= s["gap"] < 7                                        # noqa: E731
    show("启动：题材昨日涨停 ≥3 家，前一天 <3 家", lambda s: gap_ok(s) and s["n"] >= 3 and s["n_prev"] < 3)
    show("发酵：≥5 家且比前一天多、最高板 2~4", lambda s: gap_ok(s) and s["n"] >= 5 and s["n"] > s["n_prev"] and 2 <= s["maxb"] <= 4)
    show("高潮：最高板 ≥5", lambda s: gap_ok(s) and s["maxb"] >= 5)
    show("退潮：涨停家数降到前一天的 2/3 以下", lambda s: gap_ok(s) and s["n_prev"] >= 3 and s["n"] <= s["n_prev"] * 2 / 3)
    show("没有板块效应：题材昨日涨停 ≤2 家", lambda s: gap_ok(s) and s["n"] <= 2)


if __name__ == "__main__":
    main()
