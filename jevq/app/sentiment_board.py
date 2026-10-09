"""面板「情绪」页：每天一根情绪 K 线（用户 2026-10-09 要求）。

情绪分：Jev 四档概率的期望值，冰点 0 / 偏弱 1 / 正常 2 / 火热 3（没有概率就用标签本身）。
一天之内后台每 10 分钟判一次 → 开（第一次）、高、低、收（最后一次），再配上当天最后一次统计里的
涨停、跌停、炸板率、最高连板、红盘/绿盘家数，以及情绪拐点（verdict='turn' 已确认）的时间。只读。
"""
import json
import sqlite3

SCORE = {"冰点": 0, "偏弱": 1, "正常": 2, "火热": 3}
STAT_KEYS = {"涨停家数": "limit_up", "跌停家数": "limit_down", "炸板率%": "break_rate", "最高连板": "max_board",
             "上涨家数": "up", "下跌家数": "down", "成交额较昨日同期%": "amount_ratio"}


def score(label: str | None, probs: dict | None) -> float | None:
    if probs:
        total = sum(p for k, p in probs.items() if k in SCORE)
        if total > 0:
            return round(sum(SCORE[k] * p for k, p in probs.items() if k in SCORE) / total, 3)
    return SCORE.get(label) if label is not None else None


def _rows(conn: sqlite3.Connection, days: int):
    dates = [d for (d,) in conn.execute(
        "SELECT DISTINCT substr(ts, 1, 10) AS d FROM decisions WHERE verdict='sentiment' "
        "AND (strategy IN ('lowbuy', 'market') OR strategy IS NULL) ORDER BY d DESC LIMIT ?", (days,))]
    if not dates:
        return [], []
    first = min(dates)
    rows = conn.execute(
        "SELECT ts, detail, trace FROM decisions WHERE verdict='sentiment' "
        "AND (strategy IN ('lowbuy', 'market') OR strategy IS NULL) AND substr(ts, 1, 10) >= ? ORDER BY ts, id",
        (first,)).fetchall()
    return sorted(dates), rows


def board(conn: sqlite3.Connection, days: int = 60) -> dict:
    dates, rows = _rows(conn, days)
    by_day: dict[str, list] = {d: [] for d in dates}
    for ts, detail, trace in rows:
        day = ts[:10]
        if day not in by_day:
            continue
        try:
            d = json.loads(detail or "{}")
            t = json.loads(trace) if trace else {}
        except ValueError:
            continue
        label = d.get("label")
        by_day[day].append({"time": ts[11:16], "label": label, "score": score(label, (t or {}).get("probabilities")),
                            "stats": d.get("stats") or {}, "overseas": d.get("overseas")})
    turns = {}
    for ts, detail in conn.execute("SELECT ts, detail FROM decisions WHERE verdict='turn' AND strategy='market' "
                                   "ORDER BY ts, id"):
        try:
            ok = json.loads(detail or "{}").get("confirmed")
        except ValueError:
            ok = False
        if ok and ts[:10] not in turns:
            turns[ts[:10]] = ts[11:16]
    out = []
    for day in dates:
        pts = by_day[day]
        scored = [p for p in pts if p["score"] is not None]
        last_stats = next((p["stats"] for p in reversed(pts) if p["stats"]), {})
        entry = {"date": day, "n": len(pts), "turn": turns.get(day),
                 "label": next((p["label"] for p in reversed(pts) if p["label"]), None),
                 "overseas": next((p["overseas"] for p in pts if p["overseas"]), None),
                 "open": scored[0]["score"] if scored else None, "close": scored[-1]["score"] if scored else None,
                 "high": max(p["score"] for p in scored) if scored else None,
                 "low": min(p["score"] for p in scored) if scored else None,
                 "points": [{"time": p["time"], "label": p["label"], "score": p["score"],
                             **{v: p["stats"].get(k) for k, v in STAT_KEYS.items()}} for p in pts]}
        entry.update({v: last_stats.get(k) for k, v in STAT_KEYS.items()})
        out.append(entry)
    return {"days": out}
