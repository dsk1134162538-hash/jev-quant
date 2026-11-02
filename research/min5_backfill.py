"""历史 5 分钟线回填（dj 2026-11-02：Kimi 没额度，iFinD 用不了）：新浪 5 分钟 K 线一次最多 5000 根 ≈ 最近 104 个交易日。

只留「盘中最高 ≥ 前收 6%」的那天和它的下一天（次日卖法要用），其余丢掉，库保持小。
前收 = 前一交易日最后一根 5 分钟的收盘价（不复权，除权日附近会偏，回测里按涨停价校验剔掉）。

    python -m research.min5_backfill          # 主板全部代码，已存过的跳过，可中断续跑
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ART = Path(__file__).resolve().parent / "artifacts" / "first_board"
DB = ART / "min5.sqlite"
URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
KEEP_PCT = 1.06

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (date TEXT, code TEXT, hhmm TEXT, open REAL, high REAL, low REAL, close REAL,
                                 volume INTEGER, PRIMARY KEY(date, code, hhmm));
CREATE TABLE IF NOT EXISTS days (date TEXT, code TEXT, pre_close REAL, kind TEXT, PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS done (code TEXT PRIMARY KEY, n INTEGER);
"""


def _sym(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def fetch(code: str, session: requests.Session) -> list[dict]:
    r = session.get(URL, params={"symbol": _sym(code), "scale": 5, "ma": "no", "datalen": 10000},
                    headers={"Referer": "https://finance.sina.com.cn"}, timeout=20)
    r.raise_for_status()
    return json.loads(r.text) or []


def keep_days(rows: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """5 分钟线 → (要存的 bars, days)。days.kind：touch = 当天最高 ≥6%，next = 前一天 touch 的次日。"""
    by_day = defaultdict(list)
    for r in rows:
        d, t = r["day"][:10], r["day"][11:16]
        by_day[d].append((t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                          int(float(r["volume"]))))
    dates = sorted(by_day)
    kinds: dict[str, tuple[float, str]] = {}
    for i in range(1, len(dates)):
        d, pre = dates[i], by_day[dates[i - 1]][-1][4]
        if pre > 0 and max(b[2] for b in by_day[d]) >= pre * KEEP_PCT:
            kinds[d] = (pre, "touch")
            if i + 1 < len(dates) and dates[i + 1] not in kinds:
                kinds[dates[i + 1]] = (by_day[d][-1][4], "next")
    bars = [(d, t, *rest) for d in kinds for t, *rest in by_day[d]]
    return bars, [(d, pre, kind) for d, (pre, kind) in kinds.items()]


def main_board_codes() -> list[str]:
    codes = set()
    for name in ("kline.sqlite", "kline_2025.sqlite"):
        p = ART / name
        if p.exists():
            conn = sqlite3.connect(p)
            codes |= {r[0] for r in conn.execute("SELECT DISTINCT code FROM k")}
            conn.close()
    return sorted(c for c in codes if c.startswith(("60", "00")))


def run(codes: list[str], db: Path = DB, workers: int = 4, pause: float = 0.3) -> dict:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    done = {r[0] for r in conn.execute("SELECT code FROM done")}
    todo = [c for c in codes if c not in done]
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"

    def one(code):
        for attempt in range(3):
            try:
                time.sleep(pause)
                return code, fetch(code, session)
            except Exception:                                  # noqa: BLE001
                time.sleep(5 * (attempt + 1))
        return code, None
    failed, n = [], 0
    with ThreadPoolExecutor(workers) as ex:
        for code, rows in ex.map(one, todo):
            if rows is None:
                failed.append(code)
                continue
            bars, days = keep_days(rows)
            conn.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)",
                             [(d, code, t, o, h, lo, c, v) for d, t, o, h, lo, c, v in bars])
            conn.executemany("INSERT OR REPLACE INTO days VALUES (?,?,?,?)", [(d, code, p, k) for d, p, k in days])
            conn.execute("INSERT OR REPLACE INTO done VALUES (?, ?)", (code, len(rows)))
            conn.commit()
            n += 1
            if n % 200 == 0:
                print(f"{n}/{len(todo)}", flush=True)
    conn.close()
    return {"todo": len(todo), "saved": n, "failed": failed}


if __name__ == "__main__":
    print(run(main_board_codes()))
