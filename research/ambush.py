"""提前埋伏（dj 2026-11-02「找最有概率封板的标的，根据新闻、市场热度、风口」）：竞价开盘价买，看当天封板率和次日收益。

A. 风口：前一天涨停家数最多的概念（≥3 只涨停、成员 ≤150 只，剔除融资融券这类大杂烩），今天买它还没涨停的成员，
   按成员前一天涨幅分档（次强 5~9.9% / 0~5% / 收跌），再按「连续第几天热」分。
B. 公告：按公告类型，公告日的下一个交易日开盘买（公告多在盘后发，按下一天开盘算不会偷看）。

概念成员是 2026-10-29 的快照，用在两年前有轻微前视（后来被归进热门概念的票），结论要打折看。
数据：research/artifacts/first_board/kline*.sqlite；概念、公告读 E:/jev-live/data/jevq.sqlite（只读）。

    python -m research.ambush
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import mean

from research.dragon_relay import FEE, SPLIT, lim, load

LIVE_DB = Path("E:/jev-live/data/jevq.sqlite")
MAX_MEMBERS = 150
HOT_MIN_ZT = 3
TOP_CONCEPTS = 3


def _ro(db: Path):
    return sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)


def concept_members(db: Path = LIVE_DB) -> dict[str, set[str]]:
    conn = _ro(db)
    m = defaultdict(set)
    for code, concept in conn.execute("SELECT code, concept FROM concepts"):
        m[concept].add(code)
    conn.close()
    return {k: v for k, v in m.items() if len(v) <= MAX_MEMBERS}


def daily_index(bars):
    """(code, date) → (前收, 开, 收, 高)，以及交易日历。"""
    idx, cal = {}, set()
    for code, rows in bars.items():
        for i in range(1, len(rows)):
            d, o, c, h, lo, v = rows[i]
            idx[(code, d)] = (rows[i - 1][2], o, c, h)
            cal.add(d)
    return idx, sorted(cal)


def outcome(idx, code, d1, d2):
    """d1 开盘买（开在涨停买不到 → None），d2 开盘 / 收盘卖。"""
    a, b = idx.get((code, d1)), idx.get((code, d2))
    if not a or not b or a[0] <= 0:
        return None
    pre, o, c, h = a
    if o >= lim(pre):
        return None
    return {"gap": (o / pre - 1) * 100, "sealed": c >= lim(pre), "touched": h >= lim(pre),
            "x_open": (b[1] / o - 1) * 100 - FEE, "x_close": (b[2] / o - 1) * 100 - FEE}


def theme_samples(bars, members):
    idx, cal = daily_index(bars)
    zt = defaultdict(set)
    pct = {}
    for (code, d), (pre, o, c, h) in idx.items():
        if pre > 0:
            pct[(code, d)] = (c / pre - 1) * 100
            if c >= lim(pre):
                zt[d].add(code)
    out, prev_hot = [], set()
    for i, d in enumerate(cal[:-2]):
        heat = sorted(((len(zt[d] & mem), k) for k, mem in members.items()), reverse=True)
        hot = [k for n, k in heat[:TOP_CONCEPTS] if n >= HOT_MIN_ZT]
        d1, d2 = cal[i + 1], cal[i + 2]
        for k in hot:
            for code in members[k] - zt[d]:
                p = pct.get((code, d))
                r = outcome(idx, code, d1, d2)
                if p is None or r is None:
                    continue
                out.append({"date": d1, "code": code, "concept": k, "prev_pct": p,
                            "streak_hot": "连续热" if k in prev_hot else "首日热", **r})
        prev_hot = set(hot)
    return out


def notice_samples(bars, db: Path = LIVE_DB):
    idx, cal = daily_index(bars)
    nxt = {d: cal[i + 1] for i, d in enumerate(cal[:-1])}
    after = {}
    for d in cal:                                  # 公告日（含周末）→ 之后的第一个交易日
        after[d] = nxt.get(d)
    conn = _ro(db)
    rows = conn.execute("SELECT DISTINCT code, date, type FROM notices").fetchall()
    conn.close()
    out = []
    for code, d, typ in rows:
        if not code.startswith(("60", "00")):
            continue
        d1 = next((x for x in cal if x > d), None)
        if d1 is None or d1 not in nxt:
            continue
        r = outcome(idx, code, d1, nxt[d1])
        if r is not None:
            out.append({"date": d1, "code": code, "type": typ, **r})
    return out


def fmt(xs):
    if not xs:
        return "n=0"
    return (f"n={len(xs):5}  当天封{sum(x['sealed'] for x in xs) / len(xs) * 100:4.1f}%  "
            f"摸板{sum(x['touched'] for x in xs) / len(xs) * 100:4.1f}%  "
            f"次开{mean(x['x_open'] for x in xs):+.2f}  次收{mean(x['x_close'] for x in xs):+.2f}")


def table(rows, key, split, min_n=50):
    groups = defaultdict(lambda: ([], []))
    for x in rows:
        k = key(x)
        if k is not None:
            groups[k][0 if x["date"] < split else 1].append(x)
    lines = []
    for k, (a, b) in sorted(groups.items(), key=lambda kv: -len(kv[1][0]) - len(kv[1][1])):
        if len(a) >= min_n and len(b) >= min_n // 2:
            lines += [f"{str(k):<28} 前段 {fmt(a)}", f"{'':<28} 后段 {fmt(b)}"]
    return lines


def main():
    bars = load()
    members = concept_members()
    th = theme_samples(bars, members)
    base = [x for x in th]
    print(f"A. 风口埋伏：{len(th)} 笔（概念 {len(members)} 个），训练 < {SPLIT} ≤ 验证")
    print("\n".join(table(th, lambda x: "全部", SPLIT)))
    print("\n".join(table(th, lambda x: ("次强5~9.9%" if x["prev_pct"] >= 5 else "0~5%" if x["prev_pct"] >= 0
                                          else "收跌", x["streak_hot"]), SPLIT)))
    print("\n".join(table(th, lambda x: ("次强5~9.9%" if x["prev_pct"] >= 5 else "其他",
                                          "高开≥3%" if x["gap"] >= 3 else "平开0~3%" if x["gap"] >= 0 else "低开"), SPLIT)))
    no = notice_samples(bars)
    mid = "2026-08-28"
    print(f"\nB. 公告：{len(no)} 笔（2026-06-25 起），前段 < {mid} ≤ 后段；按类型，只列样本够的")
    print("\n".join(table(no, lambda x: x["type"], mid, min_n=80)))


if __name__ == "__main__":
    main()
