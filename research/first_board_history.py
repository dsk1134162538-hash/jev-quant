"""打首板的历史样本（dj 2026-11-02「去执行吧 / 都试一下」）：比 board_events 的 20 天大好几倍、没有幸存者偏差。

数据：腾讯日 K（不复权，开高低收量）拉全部主板股票，缓存在 research/artifacts/first_board/kline.sqlite。
候选口径（跟扫板一致）：当天最高价摸到昨收 × 1.09、前 10 个交易日没收过涨停、不是一字板（开盘就封死买不到）。
  买入价：开盘已经 ≥ 9% 按开盘价，否则按昨收 × 1.09；不管最后封没封住都算——炸板的也是真实买进的票。
  结果：次日开盘卖的收益 ret_open%、次日收盘卖的 ret_close%（都扣 0.15% 双边费用）。
决策时点：09:26 竞价刚结束。能用的只有昨天收盘以前的统计 + 今天的竞价（昨日涨停股今天的开盘、全市场平均开盘缺口）。
  个股特征同理只用昨天以前的数据 + 今天开盘缺口；「今天同行业摸 9% 家数」是盘中逐步可见的，会有轻微前视，单独标出。
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import mean

import requests

ART = Path(__file__).resolve().parent / "artifacts" / "first_board"
KLINE_DB = ART / "kline.sqlite"
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
FEE = 0.15                       # 双边费用 %，扣在每笔收益里
LOOKBACK = 10                    # 前 10 天收过涨停的不算首板


def _sym(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def fetch_klines(codes: list[str], start: str, end: str, db: Path = KLINE_DB, workers: int = 8) -> int:
    """把日 K 拉进缓存库；已经有 end 那天数据的票跳过。返回新拉的只数。"""
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS k (code TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL, "
                 "volume REAL, PRIMARY KEY(code, date))")
    have = {r[0] for r in conn.execute("SELECT code FROM k WHERE date >= ?", (end,))}
    todo = [c for c in codes if c not in have]
    session = requests.Session()

    def one(code):
        for attempt in range(3):
            try:
                r = session.get(KLINE_URL, params={"param": f"{_sym(code)},day,{start},{end},400,"}, timeout=10)
                rows = (r.json()["data"][_sym(code)].get("day") or [])
                return code, [(code, x[0], float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]))
                              for x in rows if len(x) >= 6]
            except Exception:                                 # noqa: BLE001
                time.sleep(1 + attempt)
        return code, []
    n = 0
    with ThreadPoolExecutor(workers) as ex:
        for code, rows in ex.map(one, todo):
            if rows:
                conn.executemany("INSERT OR REPLACE INTO k VALUES (?,?,?,?,?,?,?)", rows)
                n += 1
    conn.commit()
    conn.close()
    return n


def limit_price(pre: float) -> float:
    return round(pre * 1.1 + 1e-9, 2)


def load_bars(db: Path = KLINE_DB) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(db)
    bars = defaultdict(list)
    for row in conn.execute("SELECT code, date, open, close, high, low, volume FROM k ORDER BY code, date"):
        bars[row[0]].append(row[1:])
    conn.close()
    return bars


def build(bars: dict[str, list[tuple]], industries: dict[str, str], holders: dict[str, int],
          float_shares: dict[str, float]) -> dict:
    """返回 {"days": [...], "market": {day: 竞价时可见的市场统计}, "cands": {day: [候选]}}。"""
    per_day = defaultdict(lambda: {"n": 0, "up": 0, "down": 0, "zt": 0, "dt": 0, "touch": 0, "amount": 0.0, "gap": []})
    zt_on = defaultdict(set)              # day -> 收涨停的票
    streak_on = defaultdict(dict)         # day -> {code: 连板数}
    cands = defaultdict(list)
    for code, rows in bars.items():
        streak = 0
        for i in range(1, len(rows)):
            d, o, c, h, lo, v = rows[i]
            pre = rows[i - 1][2]
            if pre <= 0 or o <= 0:
                continue
            lim, dlim = limit_price(pre), round(pre * 0.9 + 1e-9, 2)
            m = per_day[d]
            m["n"] += 1
            m["up"] += c > pre
            m["down"] += c < pre
            m["amount"] += c * v * 100
            m["gap"].append(o / pre - 1)
            is_zt = c >= lim
            streak = streak + 1 if is_zt else 0
            if is_zt:
                m["zt"] += 1
                zt_on[d].add(code)
                streak_on[d][code] = streak
            if c <= dlim:
                m["dt"] += 1
            if h >= lim:
                m["touch"] += 1
            # 候选：摸到 9%、前 10 天没收过涨停、不是一字板、有次日、至少 21 天历史
            if i < 21 or i + 1 >= len(rows) or h < pre * 1.09:
                continue
            if any(rows[j][2] >= limit_price(rows[j - 1][2]) for j in range(max(1, i - LOOKBACK), i)):
                continue
            if o >= lim and lo >= lim:
                continue
            entry = o if o >= pre * 1.09 else round(pre * 1.09, 2)
            nxt = rows[i + 1]
            prev_amount = rows[i - 1][2] * rows[i - 1][5] * 100
            fs = float_shares.get(code)
            cands[d].append({
                "code": code, "industry": industries.get(code) or "未知",
                "openGap%": round((o / pre - 1) * 100, 2),
                "pos5%": round((pre / rows[i - 6][2] - 1) * 100, 1),
                "pos20%": round((pre / rows[i - 21][2] - 1) * 100, 1),
                "prevAmount亿": round(prev_amount / 1e8, 2),
                "prevTurnover%": round(rows[i - 1][5] * 100 / fs * 100, 2) if fs else None,
                "floatMcap亿": round(fs * pre / 1e8, 1) if fs else None,
                "holders万": round(holders[code] / 1e4, 1) if holders.get(code) else None,
                "sealed": c >= lim,
                "ret_open%": round((nxt[1] / entry - 1) * 100 - FEE, 2),
                "ret_close%": round((nxt[2] / entry - 1) * 100 - FEE, 2),
            })
    days = sorted(d for d in cands if per_day[d]["n"] > 1000)
    all_days = sorted(per_day)
    market = {}
    for d in days:
        k = all_days.index(d)
        if k < 2:
            continue
        y, yy = all_days[k - 1], all_days[k - 2]
        my, myy = per_day[y], per_day[yy]
        # 昨日涨停（前天收涨停的）昨天表现 → 前一天接力情绪；前天的涨停今天开盘 → 竞价接力
        yzt = zt_on[yy]
        ychg = [b[2] / prev[2] - 1 for code in yzt if (b := _bar_on(bars[code], y)) and (prev := _bar_on(bars[code], yy))]
        tzt = zt_on[y]
        topen = [b[1] / prev[2] - 1 for code in tzt if (b := _bar_on(bars[code], d)) and (prev := _bar_on(bars[code], y))]
        market[d] = {
            "昨日涨停": my["zt"], "昨日跌停": my["dt"], "昨日上涨": my["up"], "昨日下跌": my["down"],
            "昨日炸板率%": round((my["touch"] - my["zt"]) / my["touch"] * 100, 1) if my["touch"] else None,
            "昨日最高连板": max(streak_on[y].values(), default=0),
            "昨日成交额较前日%": round((my["amount"] / myy["amount"] - 1) * 100, 1) if myy["amount"] else None,
            "前日涨停股昨日均涨%": round(mean(ychg) * 100, 2) if ychg else None,
            "前日涨停股昨日红盘率%": round(sum(x > 0 for x in ychg) / len(ychg) * 100, 1) if ychg else None,
            "昨日涨停股今日竞价均开%": round(mean(topen) * 100, 2) if topen else None,
            "全市场今日竞价均开%": round(mean(per_day[d]["gap"]) * 100, 2),
        }
    days = [d for d in days if d in market]
    out_cands = {}
    for d in days:
        by_ind = defaultdict(int)
        for c in cands[d]:
            by_ind[c["industry"]] += 1
        out_cands[d] = [{**c, "同行业今日摸9%家数(盘中逐步可见)": by_ind[c["industry"]]} for c in cands[d]]
    return {"days": days, "market": market, "cands": out_cands}


def _bar_on(rows, day):
    for r in reversed(rows):
        if r[0] == day:
            return r
        if r[0] < day:
            return None
    return None


def day_result(cands: list[dict], picks: list[str] | None, key: str = "ret_open%") -> float:
    """当天的收益：开闸且点名了票 → 点名票等权平均；否则 0。"""
    if not picks:
        return 0.0
    rets = [c[key] for c in cands if c["code"] in picks]
    return round(mean(rets), 3) if rets else 0.0


def equity_stats(daily: list[float]) -> dict:
    """按天收益（%）→ 累计、交易天数、胜率、最大回撤（单利累加，够比较用）。"""
    cum, peak, dd = 0.0, 0.0, 0.0
    for r in daily:
        cum += r
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    traded = [r for r in daily if r != 0]
    return {"total%": round(cum, 2), "tradeDays": len(traded), "days": len(daily),
            "winRate": round(sum(r > 0 for r in traded) / len(traded), 3) if traded else None,
            "avgPerTrade%": round(mean(traded), 3) if traded else None, "maxDD%": round(dd, 2)}


def save(data: dict, path: Path = ART / "dataset.json") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load(path: Path = ART / "dataset.json") -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
