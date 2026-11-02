"""把主板日线往前拉到 2019 年（dj 2026-11-02：高标接力只有两年样本，拉长历史再回测）。

腾讯 fqkline 一次最多回几百根，按年分段拉；不复权（和 kline*.sqlite 一致，涨停判断按「收盘 ≥ 昨收×1.1」）。
代码表用现在的主板（research/artifacts/first_board/kline*.sqlite 里的），已退市的票拉不到——有幸存者偏差，
对「买连板高标」这类策略偏乐观，结论要打折。

    python -m research.kline_long            # 写 research/artifacts/first_board/kline_long.sqlite，可中断续跑
"""
from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from research.min5_backfill import main_board_codes

ART = Path(__file__).resolve().parent / "artifacts" / "first_board"
DB = ART / "kline_long.sqlite"
URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
YEARS = [(f"{y}-01-01", f"{y}-12-31") for y in range(2019, 2024)] + [("2024-01-01", "2024-11-30")]


def _sym(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def fetch(code: str, start: str, end: str, session: requests.Session) -> list[tuple]:
    r = session.get(URL, params={"param": f"{_sym(code)},day,{start},{end},640,"}, timeout=15)
    data = r.json()["data"][_sym(code)]
    rows = data.get("day") or data.get("qfqday") or []
    return [(code, x[0], float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows if len(x) >= 6]


def run(codes: list[str], workers: int = 8) -> dict:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS k (code TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL, volume REAL,
                                      PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS done (code TEXT PRIMARY KEY);
    """)
    done = {r[0] for r in conn.execute("SELECT code FROM done")}
    todo = [c for c in codes if c not in done]
    session = requests.Session()

    def one(code):
        out = []
        for start, end in YEARS:
            for attempt in range(3):
                try:
                    out += fetch(code, start, end, session)
                    break
                except Exception:                              # noqa: BLE001
                    time.sleep(2 * (attempt + 1))
            else:
                return code, None
        return code, out
    n, failed = 0, []
    with ThreadPoolExecutor(workers) as ex:
        for code, rows in ex.map(one, todo):
            if rows is None:
                failed.append(code)
                continue
            conn.executemany("INSERT OR REPLACE INTO k VALUES (?,?,?,?,?,?,?)", rows)
            conn.execute("INSERT OR REPLACE INTO done VALUES (?)", (code,))
            conn.commit()
            n += 1
            if n % 300 == 0:
                print(f"{n}/{len(todo)}", flush=True)
    conn.close()
    return {"todo": len(todo), "saved": n, "failed": failed}


if __name__ == "__main__":
    print(run(main_board_codes()))
