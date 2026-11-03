"""盘中状态能不能决定「今天该不该追板」（dj 2026-11-03「根据盘中实时更换策略才是最好」）的检验。

数据：research/min5_backfill.py 存的新浪 5 分钟线（2026-01 起，主板，只存「当天最高 ≥6%」的票和它的次日）。
每天 T 时刻（默认 10:00）的盘中状态（只用 ≤T 的 5 分钟线，不看未来）：
  封板数   T 时最新一根收盘在涨停价的票数
  炸板率   T 前摸过涨停但 T 时没封住 / (封住 + 炸板)
  摸6%家数 T 前最高 ≥ 前收×1.06 的票数（全市场摸 6% 的票都在库里）
策略：T 之后第一次摸到 9% 时追（按 max(那根开盘, 前收×1.09) 买；那根 5 分钟收在涨停的算「快板买不到」剔除），
      次日开盘 / 次日收盘卖，同 research/entry_points.py。按天汇总（每天等权），再按状态分三档看每天的平均收益。
分档阈值只用 SPLIT 之前的天数定，再套到 SPLIT 之后的天；样本只有约 100 天，每档三十来天，结论只能当线索。

    python -m research.intraday_regime
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean, pstdev

from research.entry_points import exits, lim, load

SPLIT = "2026-08-20"          # 之前定阈值，之后检验（数据从 2026-06-01 起才完整，约 104 天）


def day_state(bars_by_code: dict, T: str) -> dict:
    sealed = broke = n6 = n9 = 0
    for code, (pre, rows) in bars_by_code.items():
        seen = [r for r in rows if r[0] <= T]
        if not seen:
            continue
        hi = max(r[2] for r in seen)
        L = lim(pre)
        n6 += hi >= pre * 1.06 - 1e-9
        n9 += hi >= pre * 1.09 - 1e-9
        if seen[-1][4] >= L:
            sealed += 1
        elif hi >= L:
            broke += 1
    return {"sealed": sealed, "broke_rate": broke / (sealed + broke) if sealed + broke else 0.0, "n6": n6, "n9": n9}


def entries_after(bars, f_bars, pre, T, level=1.09):
    px = round(pre * level, 2)
    rows = bars
    if any(r[2] >= px for r in rows if r[0] <= T):
        return None
    for t, o, h, lo, c, v in rows:
        if t <= T or h < px:
            continue
        if o >= lim(pre) or c >= lim(pre):
            return None                                  # 开在涨停 / 这一根收在涨停：快板，买不到
        return max(o, px)
    return None


def main(T: str = "10:00"):
    bars, days, nxt = load()
    by_day = defaultdict(dict)                           # day -> code -> (pre, rows)
    for d, code, pre in days:
        if (d, code) in bars:
            by_day[d][code] = (pre, bars[(d, code)])
    rows = []
    for d in sorted(by_day):
        if d < "2026-06-01" or d not in nxt:
            continue
        st = day_state(by_day[d], T)
        outs = []
        for code, (pre, b) in by_day[d].items():
            if not 0.85 < b[0][1] / pre < 1.11:
                continue
            entry = entries_after(b, None, pre, T)
            f = bars.get((nxt[d], code))
            if entry is None or not f:
                continue
            outs.append(exits(entry, f))
        if outs:
            rows.append({"date": d, **st, "n": len(outs),
                         "open": mean(o["开盘"] for o in outs), "close": mean(o["收盘"] for o in outs)})
    print(f"T={T}  有追板样本的天数 {len(rows)}，每天平均 {mean(r['n'] for r in rows):.1f} 笔；全体天均：次开 {mean(r['open'] for r in rows):+.2f}  次收 {mean(r['close'] for r in rows):+.2f}")
    train = [r for r in rows if r["date"] < SPLIT]
    test = [r for r in rows if r["date"] >= SPLIT]
    print(f"阈值天数 {len(train)}，检验天数 {len(test)}\n")
    for feat, label in (("sealed", "封板数"), ("broke_rate", "炸板率"), ("n6", "摸6%家数")):
        vals = sorted(r[feat] for r in train)
        lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
        print(f"== {label}（阈值 {lo:.2f} / {hi:.2f}）==   每天平均收益 %，天数，括号里是日度标准误")
        for seg_name, seg in (("阈值段", train), ("检验段", test)):
            line = []
            for name, pick in (("低档", lambda r: r[feat] < lo), ("中档", lambda r: lo <= r[feat] < hi), ("高档", lambda r: r[feat] >= hi)):
                g = [r for r in seg if pick(r)]
                if len(g) >= 3:
                    se = pstdev([r["open"] for r in g]) / len(g) ** 0.5
                    line.append(f"{name} n={len(g):2} 次开{mean(r['open'] for r in g):+6.2f}±{se:.2f} 次收{mean(r['close'] for r in g):+6.2f}")
                else:
                    line.append(f"{name} n={len(g)}")
            print(f"  {seg_name}  " + "  |  ".join(line))
        print()


if __name__ == "__main__":
    import sys
    main(*sys.argv[1:2])
