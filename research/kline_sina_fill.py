"""腾讯日线被 WAF 限流（501）后，用新浪日线补 kline_long.sqlite 里缺的主板票（2019-01 ~ 2024-10）。
新浪一次请求给到 2017 至今，不复权；成交量单位是股，÷100 换成手，和腾讯一致。

    python -m research.kline_sina_fill
"""
from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from research.kline_long import DB
from research.min5_backfill import main_board_codes

URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
START, END = "2019-01-01", "2024-10-31"


def fetch(code: str, session: requests.Session) -> list[tuple]:
    sym = ("sh" if code.startswith("6") else "sz") + code
    r = session.get(URL, params={"symbol": sym, "scale": 240, "ma": "no", "datalen": 2300},
                    headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    rows = json.loads(r.text) or []
    return [(code, x["day"], float(x["open"]), float(x["close"]), float(x["high"]), float(x["low"]),
             float(x["volume"]) / 100) for x in rows if START <= x["day"] <= END]


def run(workers: int = 3, pause: float = 0.3) -> dict:
    conn = sqlite3.connect(DB)
    done = {r[0] for r in conn.execute("SELECT code FROM done")}
    todo = [c for c in main_board_codes() if c not in done]
    session = requests.Session()
    session.trust_env = False

    def one(code):
        for attempt in range(3):
            try:
                time.sleep(pause)
                return code, fetch(code, session)
            except Exception:                                  # noqa: BLE001
                time.sleep(3 * (attempt + 1))
        return code, None
    n, failed, empty = 0, [], 0
    with ThreadPoolExecutor(workers) as ex:
        for code, rows in ex.map(one, todo):
            if rows is None:
                failed.append(code)
                continue
            if rows:
                conn.executemany("INSERT OR REPLACE INTO k VALUES (?,?,?,?,?,?,?)", rows)
            else:
                empty += 1                                     # 2019~2024-10 还没上市的新股
            conn.execute("INSERT OR REPLACE INTO done VALUES (?)", (code,))
            conn.commit()
            n += 1
            if n % 200 == 0:
                print(f"{n}/{len(todo)}", flush=True)
    conn.close()
    return {"todo": len(todo), "saved": n, "no_history": empty, "failed": len(failed)}


if __name__ == "__main__":
    print(run())
