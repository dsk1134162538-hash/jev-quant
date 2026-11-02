"""按行情换打法（dj 2026-11-02「真正的策略是根据行情来换，现在能做到吗」）。

484 个交易日（2024-11 ~ 2026-10，腾讯日 K，主板去 ST）：
1. 每种打法每天的收益（当天全部信号等权，一笔一个单位）：
   首板 —— 摸 9% 买、次日开盘卖；二板 —— 排涨停价买、次日开盘卖；
   龙回头 —— ≥3 连板回撤 ≥15%、开盘买、次日开盘卖；
   指标 —— dj 的龙回头指标（放宽组合 A）、尾盘收盘价买、次日收盘卖（T+1 能做到的最短）。
2. 行情特征只用前一天收盘以前的数据：全市场等权 5/20 日涨幅、昨日涨停/跌停家数、昨日炸板率、
   昨日首板次日（即今天开盘前已知的前天首板昨天）表现……
3. 训练段找「这种行情下这种打法赚不赚」，验证段检验；路由 = 每天选训练段条件均值最高且 > 0 的打法，否则空仓。
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean

from research import board_patterns as bp
from research import dragon_return as dr
from research import first_board_history as fb

FEATURES = ("mkt5%", "mkt20%", "昨日涨停", "昨日跌停", "昨日炸板率%", "昨日首板溢价%")


def market_features(bars) -> dict[str, dict]:
    """每天的行情特征（全部来自前一天收盘以前）。"""
    per = defaultdict(lambda: {"ret": [], "zt": 0, "dt": 0, "touch": 0})
    for code, rows in bars.items():
        for i in range(1, len(rows)):
            d, o, c, h, lo, v = rows[i]
            pre = rows[i - 1][2]
            if pre <= 0:
                continue
            m = per[d]
            m["ret"].append(c / pre - 1)
            lim = fb.limit_price(pre)
            m["zt"] += c >= lim
            m["dt"] += c <= round(pre * 0.9 + 1e-9, 2)
            m["touch"] += h >= lim
    days = sorted(d for d in per if len(per[d]["ret"]) > 1000)
    idx = {d: n for n, d in enumerate(days)}
    ew = {d: mean(per[d]["ret"]) for d in days}
    feats = {}
    for d in days:
        n = idx[d]
        if n < 21:
            continue
        y = days[n - 1]

        def cum(k):
            x = 1.0
            for dd in days[n - k:n]:
                x *= 1 + ew[dd]
            return round((x - 1) * 100, 2)
        my = per[y]
        feats[d] = {"mkt5%": cum(5), "mkt20%": cum(20), "昨日涨停": my["zt"], "昨日跌停": my["dt"],
                    "昨日炸板率%": round((my["touch"] - my["zt"]) / my["touch"] * 100, 1) if my["touch"] else 0}
    return feats


def strategy_days(bars, float_shares) -> dict[str, dict[str, float]]:
    """{打法: {日期: 当天等权收益%}}。"""
    out = {}
    data = fb.build(bars, {}, {}, float_shares)
    out["首板"] = {d: mean(c["ret_open%"] for c in cs) for d, cs in data["cands"].items() if cs}
    prem = {}
    for d in data["days"]:
        sealed = [c["ret_open%"] for c in data["cands"][d] if c["sealed"]]
        if sealed:
            prem[d] = mean(sealed)
    out["_首板封住次日溢价"] = prem
    sb = bp.second_board(bars)["E3 排二板(涨停价)"]
    out["二板"] = _by_day(sb, "next_open")
    lh = bp.dragon_pullback(bars)["L1 开盘买"]
    out["龙回头"] = _by_day(lh, "next_open")
    p = dr.Params(name="A", shrink=0.8, shrink_vs_prev=False, below_ma5=False, wash_all=False)
    sigs, _ = dr.run(bars, float_shares, p)
    out["指标"] = _by_day(sigs, "close→nextClose")
    return out


def _by_day(trades, key):
    g = defaultdict(list)
    for t in trades:
        if key in t:
            g[t["day"]].append(t[key])
    return {d: mean(v) for d, v in g.items()}


def add_premium_feature(feats, prem):
    """昨日首板溢价：前天封住的首板昨天开盘的平均溢价——今天开盘前已知。"""
    days = sorted(feats)
    all_days = sorted(set(days) | set(prem))
    pos = {d: n for n, d in enumerate(all_days)}
    for d in days:
        n = pos[d]
        feats[d]["昨日首板溢价%"] = round(prem[all_days[n - 2]], 2) if n >= 2 and all_days[n - 2] in prem else None


def tertiles(values):
    v = sorted(x for x in values if x is not None)
    return (v[len(v) // 3], v[2 * len(v) // 3]) if len(v) >= 3 else (0, 0)


def bucket(x, cuts):
    if x is None:
        return None
    return "低" if x < cuts[0] else ("中" if x < cuts[1] else "高")


def conditional_table(strats, feats, train_days, test_days) -> dict:
    """每种打法 × 每个特征的三分位：训练段/验证段的日均收益和天数。"""
    table = {}
    for f in FEATURES:
        cuts = tertiles([feats[d][f] for d in train_days if d in feats])
        for s, series in strats.items():
            if s.startswith("_"):
                continue
            for b in ("低", "中", "高"):
                tr = [series[d] for d in train_days if d in series and bucket(feats[d][f], cuts) == b]
                te = [series[d] for d in test_days if d in series and bucket(feats[d][f], cuts) == b]
                table[(f, b, s)] = {"cuts": cuts, "train": (round(mean(tr), 2) if tr else None, len(tr)),
                                    "test": (round(mean(te), 2) if te else None, len(te))}
    return table


def router(strats, feats, train_days, test_days, features=("mkt5%", "昨日首板溢价%"), min_days=8):
    """两个特征的三分位组合成行情格子；每个格子在训练段挑日均收益最高且 > 0 的打法，没有就空仓。"""
    cuts = {f: tertiles([feats[d][f] for d in train_days]) for f in features}

    def cell(d):
        return tuple(bucket(feats[d][f], cuts[f]) for f in features)
    names = [s for s in strats if not s.startswith("_")]
    stats = defaultdict(lambda: defaultdict(list))
    for d in train_days:
        for s in names:
            if d in strats[s]:
                stats[cell(d)][s].append(strats[s][d])
    choice = {}
    for c, by in stats.items():
        best = max(((mean(v), s) for s, v in by.items() if len(v) >= min_days), default=(0, None))
        choice[c] = best[1] if best[0] > 0 else None

    def run(days):
        daily = []
        for d in days:
            s = choice.get(cell(d))
            daily.append(strats[s].get(d, 0.0) if s else 0.0)
        return fb.equity_stats(daily)
    return {"choice": {"/".join(map(str, k)): v for k, v in choice.items()}, "train": run(train_days),
            "test": run(test_days)}
