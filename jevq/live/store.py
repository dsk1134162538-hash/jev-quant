"""实盘跟单：live_intents / live_orders / live_positions / live_state 的读写。
表结构见 jevq/db.py 的 SCHEMA；字段含义见 docs/specs/2026-09-29-live-trading-design.md「数据」「规则」两节。
"""
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from jevq import config
from jevq.models import Fill


@dataclass(frozen=True)
class Intent:
    id: int
    ts: datetime
    source: str
    side: str
    code: str
    name: str
    sim_price: float
    sim_shares: int
    reason: str


@dataclass(frozen=True)
class LivePosition:
    code: str
    shares: int
    cost: float
    buy_price: float
    buy_date: date
    sim_buy_price: float


# ---- live_state / 模式 ----

def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM live_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _put_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    """写一个键（None = 删），不提交。"""
    if value is None:
        conn.execute("DELETE FROM live_state WHERE key=?", (key,))
    else:
        conn.execute("INSERT OR REPLACE INTO live_state(key, value) VALUES (?, ?)", (key, value))


def set_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    _put_state(conn, key, value)
    conn.commit()


VALID_MODES = ("off", "dryrun", "live")


def mode(conn: sqlite3.Connection) -> str:
    return get_state(conn, "mode") or "off"


def set_mode(conn: sqlite3.Connection, new: str) -> None:
    """切模式。dryrun → live 时清掉演练攒下的 live_positions 和减账待确认状态（reduce:*），
    不然实盘一上来就顶着演练留下的假持仓和假减账记录。"""
    if new not in VALID_MODES:
        raise ValueError(f"未知模式：{new}")
    if mode(conn) == "dryrun" and new == "live":
        conn.execute("DELETE FROM live_positions")
        keys = [k for (k,) in conn.execute("SELECT key FROM live_state WHERE key LIKE 'reduce:%'")]
        for k in keys:
            conn.execute("DELETE FROM live_state WHERE key=?", (k,))
        conn.commit()
    set_state(conn, "mode", new)


# ---- live_intents ----

def add_intents(conn: sqlite3.Connection, fills: list[Fill], names: dict[str, str]) -> int:
    """把这一轮 LIVE_ACCOUNT 的模拟成交写成待跟单意图；其它账户的成交不管。返回写入条数。"""
    rows = [(f.ts.isoformat(), "sim", f.side, f.code, names.get(f.code, f.code), f.price, f.shares,
             f.reason, "new") for f in fills if f.account == config.LIVE_ACCOUNT]
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO live_intents(ts, source, side, code, name, sim_price, sim_shares, reason, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def add_remote_sell(conn: sqlite3.Connection, now: datetime) -> int:
    """手机端「立即卖出」：写一条通配卖出意图，把程序全部持仓标成待卖。返回新意图的 id。"""
    cur = conn.execute(
        "INSERT INTO live_intents(ts, source, side, code, name, sim_price, sim_shares, reason, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now.isoformat(), "remote", "sell", "*", "", 0.0, 0, "手机立即卖出", "new"))
    conn.commit()
    return cur.lastrowid


def new_intents(conn: sqlite3.Connection) -> list[Intent]:
    rows = conn.execute(
        "SELECT id, ts, source, side, code, name, sim_price, sim_shares, reason FROM live_intents "
        "WHERE status='new' ORDER BY id").fetchall()
    return [Intent(i, datetime.fromisoformat(ts), source, side, code, name, sim_price, sim_shares, reason)
            for i, ts, source, side, code, name, sim_price, sim_shares, reason in rows]


def finish_intent(conn: sqlite3.Connection, intent_id: int, status: str, note: str, now: datetime) -> None:
    conn.execute("UPDATE live_intents SET status=?, note=?, handled_ts=? WHERE id=?",
                (status, note, now.isoformat(), intent_id))
    conn.commit()


# ---- live_positions ----

def positions(conn: sqlite3.Connection) -> dict[str, LivePosition]:
    rows = conn.execute(
        "SELECT code, shares, cost, buy_price, buy_date, sim_buy_price FROM live_positions").fetchall()
    return {code: LivePosition(code, shares, cost, buy_price, date.fromisoformat(buy_date), sim_buy_price)
            for code, shares, cost, buy_price, buy_date, sim_buy_price in rows}


def put_position(conn: sqlite3.Connection, p: LivePosition) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO live_positions(code, shares, cost, buy_price, buy_date, sim_buy_price) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (p.code, p.shares, p.cost, p.buy_price, p.buy_date.isoformat(), p.sim_buy_price))
    conn.commit()


def reduce_position(conn: sqlite3.Connection, code: str, shares: int) -> None:
    """减仓：减到 ≤0 直接删行。code 不存在时什么都不做。"""
    row = conn.execute("SELECT shares FROM live_positions WHERE code=?", (code,)).fetchone()
    if row is None:
        return
    left = row[0] - shares
    if left <= 0:
        conn.execute("DELETE FROM live_positions WHERE code=?", (code,))
    else:
        conn.execute("UPDATE live_positions SET shares=? WHERE code=?", (left, code))
    conn.commit()


# ---- live_orders ----

_ORDER_COLUMNS = ("id", "intent_id", "ts", "side", "code", "price", "shares", "filled", "avg_price",
                  "entrust_no", "status", "note", "updated_ts")
_ORDER_UPDATABLE = frozenset({"filled", "avg_price", "entrust_no", "status", "note"})


def add_order(conn: sqlite3.Connection, intent_id: int | None, now: datetime, side: str, code: str,
             price: float, shares: int, status: str, note: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO live_orders(intent_id, ts, side, code, price, shares, filled, avg_price, status, note) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 0.0, ?, ?)",
        (intent_id, now.isoformat(), side, code, price, shares, status, note))
    conn.commit()
    return cur.lastrowid


def update_order(conn: sqlite3.Connection, order_id: int, now: datetime, **fields) -> None:
    """允许字段：filled avg_price entrust_no status note；其它 key 直接忽略。"""
    cols = [k for k in fields if k in _ORDER_UPDATABLE]
    if not cols:
        return
    assignments = ", ".join(f"{k}=?" for k in cols)
    params = [fields[k] for k in cols] + [now.isoformat(), order_id]
    conn.execute(f"UPDATE live_orders SET {assignments}, updated_ts=? WHERE id=?", params)
    conn.commit()


def _order_rows(conn: sqlite3.Connection, where: str, params: tuple) -> list[dict]:
    rows = conn.execute(
        f"SELECT {', '.join(_ORDER_COLUMNS)} FROM live_orders WHERE {where} ORDER BY id", params).fetchall()
    return [dict(zip(_ORDER_COLUMNS, row)) for row in rows]


def orders_on(conn: sqlite3.Connection, day: date) -> list[dict]:
    return _order_rows(conn, "substr(ts, 1, 10)=?", (day.isoformat(),))


_INTENT_COLUMNS = ("id", "ts", "source", "side", "code", "name", "sim_price", "sim_shares", "reason",
                   "status", "note", "handled_ts")


def intents_on(conn: sqlite3.Connection, day: date) -> list[dict]:
    """当天全部意图（不限 status），供手机页显示「今天被跳过的信号」等只读汇总用。"""
    rows = conn.execute(
        f"SELECT {', '.join(_INTENT_COLUMNS)} FROM live_intents "
        "WHERE substr(ts, 1, 10)=? ORDER BY id", (day.isoformat(),)).fetchall()
    return [dict(zip(_INTENT_COLUMNS, row)) for row in rows]


def sending_orders(conn: sqlite3.Connection) -> list[dict]:
    return _order_rows(conn, "status='sending'", ())


def open_orders(conn: sqlite3.Connection) -> list[dict]:
    """启动核对用：sending（还没确认下没下出去）+ submitted（下出去了但可能没等到最终成交）都要核对。"""
    return _order_rows(conn, "status IN ('sending', 'submitted')", ())


_BUY_COUNTED_STATUSES = ("submitted", "filled", "partial", "cancelled", "dryrun")


def buys_today(conn: sqlite3.Connection, day: date) -> int:
    """当天程序自己下出去的买入委托数：failed 不算（没真下出去），其余（含 dryrun）都算。"""
    marks = ", ".join("?" * len(_BUY_COUNTED_STATUSES))
    row = conn.execute(
        f"SELECT COUNT(*) FROM live_orders WHERE side='buy' AND substr(ts, 1, 10)=? AND status IN ({marks})",
        (day.isoformat(), *_BUY_COUNTED_STATUSES)).fetchone()
    return row[0]


# ---- 暂停 ----

def pause(conn: sqlite3.Connection, until: datetime | None, reason: str, kind: str) -> None:
    # 四个键写完只提交一次：页面线程和执行器共用同一个连接，不能让对方读到写了一半的暂停状态
    _put_state(conn, "paused", "1")
    _put_state(conn, "paused_until", until.isoformat() if until else None)
    _put_state(conn, "pause_reason", reason)
    _put_state(conn, "pause_kind", kind)
    conn.commit()


def clear_pause(conn: sqlite3.Connection) -> None:
    for key in ("paused", "paused_until", "pause_reason", "pause_kind"):
        _put_state(conn, key, None)
    conn.commit()


def paused(conn: sqlite3.Connection) -> dict | None:
    if get_state(conn, "paused") != "1":
        return None
    until = get_state(conn, "paused_until")
    return {"until": datetime.fromisoformat(until) if until else None,
            "reason": get_state(conn, "pause_reason"), "kind": get_state(conn, "pause_kind")}
