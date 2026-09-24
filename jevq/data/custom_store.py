"""自定义策略的版本化存储。

设计要点（见 docs/specs/2026-09-24-rule-builder-design.md §4-5）：
- 每次保存都是一条新版本记录（PRIMARY KEY(id, version)），不覆盖旧版本；
- 新版本从「下一个交易日」才生效——today 之后按 trade_days（调用方传入的交易日列表，
  不联网查询）挑出的第一个交易日；
- 停用不是删除，是再写一条 enabled=0 的新版本，同样次日生效；
- active_for() 只返回「今天在跑」的、已启用的版本，同时启用的最多 5 个；
- 已停用但仍持仓的策略不会出现在 active_for() 里（不再开新仓），但要靠 holders() 找回来，
  否则它的仓位永远没人负责卖出。
"""
import json
import sqlite3
from datetime import date

from jevq.config import MAX_CUSTOM_ENABLED
from jevq.rules.custom import validate


def next_id(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT DISTINCT id FROM custom_strategies").fetchall()
    nums = [int(sid[1:]) for (sid,) in rows if sid.startswith("c") and sid[1:].isdigit()]
    return f"c{max(nums, default=0) + 1}"


def _next_trade_day(today: date, trade_days) -> date:
    days = sorted(d for d in trade_days if d > today)
    if not days:
        raise ValueError("交易日列表里没有 today 之后的日子")
    return days[0]


def _latest_row(conn: sqlite3.Connection, sid: str, day: date):
    return conn.execute(
        "SELECT version, name, spec, enabled FROM custom_strategies "
        "WHERE id=? AND active_from<=? ORDER BY version DESC LIMIT 1",
        (sid, day.isoformat())).fetchone()


def _all_ids(conn: sqlite3.Connection) -> list[str]:
    return sorted({sid for (sid,) in conn.execute("SELECT DISTINCT id FROM custom_strategies")})


def _as_dict(sid: str, row, can_buy: bool) -> dict:
    version, name, spec_json, _enabled = row
    return {"id": sid, "version": version, "name": name, "spec": json.loads(spec_json), "can_buy": can_buy}


def active_for(conn: sqlite3.Connection, day: date) -> list[dict]:
    """今天在跑、已启用的自定义策略最新版本，最多 5 个，按 id 排序。"""
    out = []
    for sid in _all_ids(conn):
        if len(out) >= MAX_CUSTOM_ENABLED:
            break
        row = _latest_row(conn, sid, day)
        if row and row[3]:      # enabled
            out.append(_as_dict(sid, row, can_buy=True))
    return out


def holders(conn: sqlite3.Connection, day: date) -> list[dict]:
    """已停用（不在 active_for 里）但账本上仍有持仓的自定义策略，can_buy=False，
    只用来让引擎照旧按该版本的卖出规则清仓。"""
    rows = conn.execute(
        "SELECT DISTINCT account FROM positions WHERE account LIKE 'c%\\_main' ESCAPE '\\' "
        "OR account LIKE 'c%\\_shadow' ESCAPE '\\'").fetchall()
    held_ids = sorted({a.rsplit("_", 1)[0] for (a,) in rows})
    active_ids = {s["id"] for s in active_for(conn, day)}
    out = []
    for sid in held_ids:
        if sid in active_ids:
            continue
        row = _latest_row(conn, sid, day)
        if row is None:
            continue
        out.append(_as_dict(sid, row, can_buy=bool(row[3])))
    return out


def history(conn: sqlite3.Connection, sid: str) -> list[dict]:
    rows = conn.execute(
        "SELECT version, name, spec, created, active_from, enabled FROM custom_strategies "
        "WHERE id=? ORDER BY version", (sid,)).fetchall()
    return [{"version": v, "name": name, "spec": json.loads(spec_json), "created": created,
             "active_from": active_from, "enabled": bool(enabled)}
            for v, name, spec_json, created, active_from, enabled in rows]


def _enabled_count_on(conn: sqlite3.Connection, day: date, exclude_id: str | None) -> int:
    return sum(1 for sid in _all_ids(conn) if sid != exclude_id
              and (row := _latest_row(conn, sid, day)) and row[3])


def save(conn: sqlite3.Connection, spec: dict, today: date, trade_days, sid: str | None = None):
    """校验通过才保存。失败返回 (None, errors)；成功返回 (sid, version)。"""
    errors, _warnings = validate(spec)
    if errors:
        return None, errors
    active_from = _next_trade_day(today, trade_days)
    if _enabled_count_on(conn, active_from, exclude_id=sid) >= MAX_CUSTOM_ENABLED:
        return None, [f"同时启用的自定义策略最多 {MAX_CUSTOM_ENABLED} 个"]
    if sid is None:
        sid = next_id(conn)
        version = 1
    else:
        row = conn.execute("SELECT MAX(version) FROM custom_strategies WHERE id=?", (sid,)).fetchone()
        version = (row[0] or 0) + 1
    conn.execute(
        "INSERT INTO custom_strategies(id, version, name, spec, created, active_from, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (sid, version, spec["name"], json.dumps(spec, ensure_ascii=False), today.isoformat(),
         active_from.isoformat()))
    conn.commit()
    return sid, version


def disable(conn: sqlite3.Connection, sid: str, today: date, trade_days) -> int:
    """写一条 enabled=0 的新版本，次日生效；已有持仓由 holders() 找回来按原规则卖出。"""
    row = conn.execute("SELECT version, name, spec FROM custom_strategies WHERE id=? "
                       "ORDER BY version DESC LIMIT 1", (sid,)).fetchone()
    if row is None:
        raise ValueError(f"未知策略 {sid}")
    version, name, spec_json = row
    active_from = _next_trade_day(today, trade_days)
    new_version = version + 1
    conn.execute(
        "INSERT INTO custom_strategies(id, version, name, spec, created, active_from, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        (sid, new_version, name, spec_json, today.isoformat(), active_from.isoformat()))
    conn.commit()
    return new_version
