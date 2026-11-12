"""同花顺涨停揭秘历史（dj 2026-11-03「龙头识别、题材梯队、上车点、补涨怎么做」）：每个交易日每只涨停股的原因题材和连板描述。

接口 jevq.data.ztpool.fetch_reasons(d)，能回溯到约 2025-11 初（再早返回空）。拉下来存 research/artifacts/first_board/theme_zt.sqlite，
后面的题材梯队、龙头、补涨回测用真实的当时题材（不像概念表是 2026-10-29 的快照，有前视）。请求间隔 0.7 秒、单线程、出错就停。

    python -m research.theme_history
"""
from __future__ import annotations

import sqlite3
import time
from datetime import date

import requests

from jevq.data import ztpool
from research.dragon_relay import ART

DB = ART / "theme_zt.sqlite"
START = "2025-10-20"


def trading_days() -> list[str]:
    days = set()
    for name in ("kline_2025.sqlite", "kline.sqlite"):
        conn = sqlite3.connect(ART / name)
        days |= {r[0] for r in conn.execute("SELECT DISTINCT date FROM k WHERE date >= ?", (START,))}
        conn.close()
    return sorted(days)


def run(pause: float = 0.7) -> dict:
    conn = sqlite3.connect(DB)
    conn.executescript("""CREATE TABLE IF NOT EXISTS zt (date TEXT, code TEXT, name TEXT, reason TEXT, high_days TEXT,
                                                         PRIMARY KEY(date, code));
                          CREATE TABLE IF NOT EXISTS fetched (date TEXT PRIMARY KEY, n INTEGER);""")
    done = {r[0] for r in conn.execute("SELECT date FROM fetched")}
    session = requests.Session()
    session.trust_env = False
    n_days = n_rows = 0
    for d in trading_days():
        if d in done:
            continue
        rows = None
        for attempt in range(3):                                  # 读超时偶发：隔几秒重试两次，还不行就停
            try:
                rows = ztpool.fetch_reasons(date.fromisoformat(d), session=session)
                break
            except Exception as e:                                # noqa: BLE001
                err = e
                time.sleep(5 * (attempt + 1))
        if rows is None:
            print(f"{d} 失败，停止：{err!r}")
            break
        conn.executemany("INSERT OR REPLACE INTO zt VALUES (?,?,?,?,?)",
                         [(d, r["code"], r["name"], r["reason"], r["high_days"]) for r in rows if r.get("code")])
        conn.execute("INSERT OR REPLACE INTO fetched VALUES (?, ?)", (d, len(rows)))
        conn.commit()
        n_days += 1
        n_rows += len(rows)
        time.sleep(pause)
    conn.close()
    return {"days": n_days, "rows": n_rows}


if __name__ == "__main__":
    print(run())
