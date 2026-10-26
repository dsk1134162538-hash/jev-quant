"""AI 模拟成交到跟单意图的幂等桥接。

当前只支持 dryrun：桥接本身不访问券商，也绝不提供切换 live 的入口。
游标和意图在同一个 SQLite 事务里提交，重复调用不会重复生成意图。
"""
import sqlite3

from jevq import config
from jevq.live import store as live_store

CURSOR_KEY = "ai_bridge:last_fill_id"


def sync_ai_fills(conn: sqlite3.Connection, names: dict[str, str] | None = None) -> int:
    """把尚未桥接的 AI 模拟成交写成 source='ai' 的意图，返回新增条数。"""
    if live_store.mode(conn) != "dryrun":
        raise RuntimeError("AI 桥接当前仅允许 dryrun，拒绝在非演练模式同步")
    if conn.in_transaction:
        raise RuntimeError("AI 桥接要求独立事务，调用方请先提交或回滚")
    names = names or {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        raw_cursor = live_store.get_state(conn, CURSOR_KEY)
        try:
            cursor = int(raw_cursor or 0)
        except ValueError as e:
            raise RuntimeError("AI 桥接游标损坏，拒绝猜测并重复同步") from e
        rows = conn.execute(
            "SELECT id, ts, side, code, price, shares, reason FROM fills "
            "WHERE account=? AND id>? ORDER BY id", (config.AI_ACCOUNT, cursor)).fetchall()
        for fill_id, ts, side, code, price, shares, reason in rows:
            conn.execute(
                "INSERT INTO live_intents(ts, source, side, code, name, sim_price, sim_shares, reason, status) "
                "VALUES (?, 'ai', ?, ?, ?, ?, ?, ?, 'new')",
                (ts, side, code, names.get(code, code), price, shares, reason))
        if rows:
            live_store._put_state(conn, CURSOR_KEY, str(rows[-1][0]))
        conn.commit()
        return len(rows)
    except BaseException:
        conn.rollback()
        raise
