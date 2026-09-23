"""扫首板封板时段统计（只验证经验，不参与交易）：spec §4.5。"""
from datetime import date, time

from jevq.models import Quote

_BOUNDS = [
    (time(10, 30), "早盘"),
    (time(11, 30), "上午"),
    (time(13, 30), "午盘"),
    (time(14, 0), "下午"),
]


def bucket_of(first_seal: str) -> str:
    """"HHMMSS" -> 时段。09:25 集合竞价封板算早盘（早盘桶没有下限）。"""
    t = time(int(first_seal[0:2]), int(first_seal[2:4]), int(first_seal[4:6]))
    for bound, name in _BOUNDS:
        if t < bound:
            return name
    return "尾盘"


def record_day(conn, day: date, zt_rows: list[dict], concept_map: dict[str, list[str]], log=print) -> int:
    """只收当日主板首板（代码 60/00 开头、连板数 == 1），写入 board_events。已存在的记录（同 date+code）
    不覆盖，避免重复调用把已回填的 next_* 字段清空。单行数据有问题只跳过这一行，数量记日志。"""
    rows, bad = [], 0
    for r in zt_rows:
        try:
            code = str(r["代码"]).zfill(6)
            if not code.startswith(("60", "00")):
                continue
            if int(r["连板数"]) != 1:
                continue
            first_seal = str(r["首次封板时间"]).zfill(6)
            concepts = concept_map.get(code) or []
            rows.append((day.isoformat(), code, str(r["名称"]), first_seal,
                        bucket_of(first_seal), concepts[0] if concepts else ""))
        except Exception:
            bad += 1
    if bad:
        log(f"封板统计跳过 {bad} 行坏数据")
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO board_events(date, code, name, first_seal, bucket, concept) "
            "VALUES (?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def fill_next_day(conn, prev_day: date, quotes: dict[str, Quote], today: date) -> int:
    """给上一交易日尚未回填的首板，用当日报价补 next_open_pct/next_close_pct/next_board。
    只用 ts 是 today 的报价；其余的（比如缓存的旧快照）跳过，留到下次再补。"""
    pending = [r[0] for r in conn.execute(
        "SELECT code FROM board_events WHERE date = ? AND next_close_pct IS NULL",
        (prev_day.isoformat(),)).fetchall()]
    updates = []
    for code in pending:
        q = quotes.get(code)
        if q is None or q.ts.date() != today:
            continue
        next_open_pct = round((q.open / q.pre_close - 1) * 100, 2)
        next_board = 1 if q.price >= q.limit_up else 0
        updates.append((next_open_pct, q.pct, next_board, prev_day.isoformat(), code))
    if not updates:
        return 0
    with conn:
        conn.executemany(
            "UPDATE board_events SET next_open_pct = ?, next_close_pct = ?, next_board = ? "
            "WHERE date = ? AND code = ?", updates)
    return len(updates)


def summary(conn) -> list[dict]:
    """按时段汇总样本数、次日平均高开、次日平均收盘涨跌、次日连板率；只统计已回填的记录。"""
    rows = conn.execute(
        "SELECT bucket, COUNT(*), AVG(next_open_pct), AVG(next_close_pct), AVG(next_board) "
        "FROM board_events WHERE next_close_pct IS NOT NULL GROUP BY bucket").fetchall()
    return [{"bucket": bucket, "count": count,
             "avg_next_open": round(avg_open, 2),
             "avg_next_close": round(avg_close, 2),
             "board_rate": round(avg_board * 100, 1)}
            for bucket, count, avg_open, avg_close, avg_board in rows]
