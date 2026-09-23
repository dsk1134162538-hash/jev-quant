import threading
import time as _time
from datetime import date, datetime

import akshare as ak

from jevq.data.timeout import call_with_timeout


def fetch_concept_list() -> list[tuple[str, str]]:
    df = call_with_timeout(ak.stock_sector_spot, indicator="概念")
    return [(str(r["label"]), str(r["板块"])) for _, r in df.iterrows()]


def fetch_concept_members(label: str) -> list[str]:
    df = call_with_timeout(ak.stock_sector_detail, sector=label)
    return [str(c).zfill(6) for c in df["code"]]


def needs_refresh(conn, today: date, max_age_days: int = 7) -> bool:
    row = conn.execute("SELECT MAX(updated) FROM concepts").fetchone()
    if not row or not row[0]:
        return True
    return (today - date.fromisoformat(row[0])).days >= max_age_days


def refresh_concepts(conn, today: date, fetch_list=fetch_concept_list, fetch_members=fetch_concept_members,
                     pause: float = 0.5, max_consecutive_failures: int = 10,
                     deadline: datetime | None = None, stop: threading.Event | None = None,
                     log=print) -> int:
    """deadline/stop 到点就中断（不算失败），跑了多少算多少——凑不够一半就照旧保留旧数据。"""
    if deadline is not None and datetime.now() >= deadline:
        log("题材刷新时间已过，跳过")
        return 0
    if stop is not None and stop.is_set():
        log("收到停止指令，跳过题材刷新")
        return 0
    try:
        boards = fetch_list()
    except Exception as e:
        log(f"题材列表获取失败：{e!r}")
        return 0
    rows, ok, fails = [], 0, 0
    for label, name in boards:
        if deadline is not None and datetime.now() >= deadline:
            log("题材刷新到时间上限，中断")
            break
        if stop is not None and stop.is_set():
            log("收到停止指令，题材刷新中断")
            break
        try:
            members = fetch_members(label)
            rows += [(code, name) for code in members]
            ok += 1
            fails = 0
        except Exception as e:
            fails += 1
            log(f"题材 {name} 成分获取失败：{e!r}")
            if fails >= max_consecutive_failures:
                log(f"题材成分连续失败 {fails} 次，停止刷新")
                break
        _time.sleep(pause)
    if not boards or ok * 2 < len(boards):
        log(f"题材刷新成功 {ok}/{len(boards)}，不足一半，保留旧数据")
        return 0
    with conn:
        conn.execute("DELETE FROM concepts")
        conn.executemany("INSERT OR IGNORE INTO concepts(code, concept, updated) VALUES (?, ?, ?)",
                         [(c, n, today.isoformat()) for c, n in rows])
    log(f"题材刷新完成：{ok} 个题材，{len(rows)} 条成分")
    return len(rows)


def load_concepts(conn) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for code, concept in conn.execute("SELECT code, concept FROM concepts ORDER BY concept"):
        out.setdefault(code, []).append(concept)
    return out
