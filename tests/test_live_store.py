import sqlite3
import threading
import time
from datetime import date, datetime

import pytest

from jevq import db
from jevq.live import store as live_store
from jevq.live.store import Intent, LivePosition
from jevq.models import Fill


def conn():
    return db.connect(":memory:")


# ---- live_state / mode ----

def test_get_state_missing_key_returns_none():
    c = conn()
    assert live_store.get_state(c, "nope") is None


def test_set_state_roundtrip_and_none_deletes_key():
    c = conn()
    live_store.set_state(c, "k", "v")
    assert live_store.get_state(c, "k") == "v"
    live_store.set_state(c, "k", None)
    assert live_store.get_state(c, "k") is None


def test_mode_defaults_to_off():
    c = conn()
    assert live_store.mode(c) == "off"


def test_set_mode_rejects_invalid_value_and_leaves_mode_unchanged():
    c = conn()
    with pytest.raises(ValueError):
        live_store.set_mode(c, "bogus")
    assert live_store.mode(c) == "off"


def test_set_mode_roundtrip():
    c = conn()
    live_store.set_mode(c, "dryrun")
    assert live_store.mode(c) == "dryrun"


def test_set_mode_dryrun_to_live_clears_positions_and_reduce_state():
    c = conn()
    live_store.set_mode(c, "dryrun")
    live_store.put_position(c, LivePosition("600001", 500, 4755.0, 9.5, date(2026, 9, 29), 9.48))
    live_store.set_state(c, "reduce:600001", '{"shares": 300, "ts": "2026-09-29T10:00:00"}')
    live_store.set_state(c, "pid", "12345")     # 不相关的状态键，不该被动到

    live_store.set_mode(c, "live")

    assert live_store.positions(c) == {}
    assert live_store.get_state(c, "reduce:600001") is None
    assert live_store.get_state(c, "pid") == "12345"
    assert live_store.mode(c) == "live"


def test_set_mode_off_to_live_does_not_clear_positions():
    c = conn()
    live_store.put_position(c, LivePosition("600001", 500, 4755.0, 9.5, date(2026, 9, 29), 9.48))
    live_store.set_mode(c, "live")
    assert live_store.positions(c) != {}


# ---- live_intents ----

def test_add_intents_only_writes_rebound_main():
    c = conn()
    fills = [
        Fill("rebound_main", "600001", "buy", 9.5, 500, 5.0, datetime(2026, 9, 29, 9, 31), "超跌反弹买入"),
        Fill("main", "600002", "buy", 8.0, 100, 5.0, datetime(2026, 9, 29, 9, 31), "低吸买入"),
        Fill("rebound_shadow", "600003", "buy", 7.0, 100, 5.0, datetime(2026, 9, 29, 9, 31), "影子买入"),
    ]
    n = live_store.add_intents(c, fills, {"600001": "甲"})
    assert n == 1
    rows = c.execute("SELECT source, side, code, name, sim_price, sim_shares, reason, status, ts "
                     "FROM live_intents").fetchall()
    assert rows == [("sim", "buy", "600001", "甲", 9.5, 500, "超跌反弹买入", "new", "2026-09-29T09:31:00")]


def test_add_intents_falls_back_to_code_when_name_missing():
    c = conn()
    fills = [Fill("rebound_main", "600001", "sell", 9.0, 500, 5.0, datetime(2026, 9, 29, 10, 0), "止损")]
    live_store.add_intents(c, fills, {})
    assert c.execute("SELECT name FROM live_intents").fetchone()[0] == "600001"


def test_add_intents_empty_list_returns_zero_and_writes_nothing():
    c = conn()
    assert live_store.add_intents(c, [], {}) == 0
    assert c.execute("SELECT COUNT(*) FROM live_intents").fetchone()[0] == 0


def test_add_remote_sell_writes_wildcard_sell_intent():
    c = conn()
    live_store.add_remote_sell(c, datetime(2026, 9, 29, 10, 30))
    [i] = live_store.new_intents(c)
    assert (i.source, i.side, i.code) == ("remote", "sell", "*")
    assert i.ts == datetime(2026, 9, 29, 10, 30)


def test_new_intents_ordered_by_id_and_finish_intent_removes_it():
    c = conn()
    fills = [
        Fill("rebound_main", "600001", "buy", 9.5, 500, 5.0, datetime(2026, 9, 29, 9, 31), "r1"),
        Fill("rebound_main", "600002", "sell", 8.0, 300, 5.0, datetime(2026, 9, 29, 9, 32), "r2"),
    ]
    live_store.add_intents(c, fills, {"600001": "甲", "600002": "乙"})
    intents = live_store.new_intents(c)
    assert [i.code for i in intents] == ["600001", "600002"]
    assert [i.id for i in intents] == sorted(i.id for i in intents)

    first = intents[0]
    assert isinstance(first, Intent)
    assert (first.source, first.side, first.sim_shares) == ("sim", "buy", 500)

    live_store.finish_intent(c, first.id, "done", "已跟单", datetime(2026, 9, 29, 9, 31, 40))
    remaining = live_store.new_intents(c)
    assert [i.id for i in remaining] == [intents[1].id]
    status, note, handled = c.execute(
        "SELECT status, note, handled_ts FROM live_intents WHERE id=?", (first.id,)).fetchone()
    assert (status, note, handled) == ("done", "已跟单", "2026-09-29T09:31:40")


# ---- live_positions ----

def test_put_position_and_positions_roundtrip():
    c = conn()
    p = LivePosition("600001", 500, 4755.0, 9.5, date(2026, 9, 29), 9.48)
    live_store.put_position(c, p)
    assert live_store.positions(c) == {"600001": p}


def test_put_position_replaces_existing_row():
    c = conn()
    live_store.put_position(c, LivePosition("600001", 500, 4755.0, 9.5, date(2026, 9, 29), 9.48))
    live_store.put_position(c, LivePosition("600001", 600, 5700.0, 9.5, date(2026, 9, 29), 9.48))
    assert live_store.positions(c)["600001"].shares == 600


def test_reduce_position_to_zero_deletes_row():
    c = conn()
    live_store.put_position(c, LivePosition("600001", 500, 4755.0, 9.5, date(2026, 9, 29), 9.48))
    live_store.reduce_position(c, "600001", 500)
    assert live_store.positions(c) == {}


def test_reduce_position_below_zero_also_deletes_row():
    c = conn()
    live_store.put_position(c, LivePosition("600001", 500, 4755.0, 9.5, date(2026, 9, 29), 9.48))
    live_store.reduce_position(c, "600001", 600)
    assert live_store.positions(c) == {}


def test_reduce_position_partial_keeps_row_with_remaining_shares():
    c = conn()
    live_store.put_position(c, LivePosition("600001", 500, 4755.0, 9.5, date(2026, 9, 29), 9.48))
    live_store.reduce_position(c, "600001", 200)
    assert live_store.positions(c)["600001"].shares == 300


# ---- live_orders ----

def test_add_order_returns_id_and_sending_orders_filters_status():
    c = conn()
    oid1 = live_store.add_order(c, None, datetime(2026, 9, 29, 9, 31), "buy", "600001", 9.5, 500, "sending")
    oid2 = live_store.add_order(c, None, datetime(2026, 9, 29, 9, 32), "buy", "600002", 8.0, 100, "submitted")
    assert oid1 != oid2
    sending = live_store.sending_orders(c)
    assert [o["id"] for o in sending] == [oid1]
    assert sending[0]["code"] == "600001" and sending[0]["status"] == "sending"


def test_open_orders_includes_sending_and_submitted_but_not_terminal_statuses():
    c = conn()
    oid1 = live_store.add_order(c, None, datetime(2026, 9, 29, 9, 31), "buy", "600001", 9.5, 500, "sending")
    oid2 = live_store.add_order(c, None, datetime(2026, 9, 29, 9, 32), "buy", "600002", 8.0, 100, "submitted")
    live_store.add_order(c, None, datetime(2026, 9, 29, 9, 33), "buy", "600003", 7.0, 100, "filled")
    assert {o["id"] for o in live_store.open_orders(c)} == {oid1, oid2}


def test_update_order_writes_allowed_fields_and_updated_ts():
    c = conn()
    oid = live_store.add_order(c, 7, datetime(2026, 9, 29, 9, 31), "buy", "600001", 9.5, 500, "sending")
    live_store.update_order(c, oid, datetime(2026, 9, 29, 9, 31, 30),
                            filled=500, avg_price=9.48, entrust_no="12345", status="filled", note="全部成交")
    row = c.execute("SELECT intent_id, filled, avg_price, entrust_no, status, note, updated_ts "
                    "FROM live_orders WHERE id=?", (oid,)).fetchone()
    assert row == (7, 500, 9.48, "12345", "filled", "全部成交", "2026-09-29T09:31:30")


def test_update_order_ignores_unknown_fields():
    c = conn()
    oid = live_store.add_order(c, None, datetime(2026, 9, 29, 9, 31), "buy", "600001", 9.5, 500, "sending")
    live_store.update_order(c, oid, datetime(2026, 9, 29, 9, 31, 30), status="submitted", bogus="x")
    assert c.execute("SELECT status FROM live_orders WHERE id=?", (oid,)).fetchone() == ("submitted",)


def test_orders_on_returns_that_days_orders_ordered_by_id():
    c = conn()
    live_store.add_order(c, None, datetime(2026, 9, 29, 9, 31), "buy", "600001", 9.5, 500, "submitted")
    live_store.add_order(c, None, datetime(2026, 9, 29, 9, 40), "sell", "600002", 8.0, 100, "filled")
    live_store.add_order(c, None, datetime(2026, 9, 28, 9, 31), "buy", "600003", 5.0, 100, "filled")
    rows = live_store.orders_on(c, date(2026, 9, 29))
    assert [r["code"] for r in rows] == ["600001", "600002"]


# ---- buys_today ----

def test_buys_today_counts_per_spec_excludes_sending_failed_and_other_days():
    c = conn()
    day = date(2026, 9, 29)
    ts = datetime(2026, 9, 29, 9, 31)
    for i, status in enumerate(["sending", "submitted", "filled", "partial", "cancelled", "failed", "dryrun"]):
        live_store.add_order(c, None, ts, "buy", f"60000{i}", 9.5, 100, status)
    live_store.add_order(c, None, ts, "sell", "600009", 9.5, 100, "filled")               # 卖出不算
    live_store.add_order(c, None, datetime(2026, 9, 28, 9, 31), "buy", "600010", 9.5, 100, "filled")  # 前一天不算
    assert live_store.buys_today(c, day) == 5    # submitted/filled/partial/cancelled/dryrun


# ---- 暂停 ----

def test_pause_with_no_until_and_paused_roundtrip():
    c = conn()
    live_store.pause(c, None, "连续读取失败", "auto")
    assert live_store.paused(c) == {"until": None, "reason": "连续读取失败", "kind": "auto"}


def test_pause_with_until_roundtrips_datetime():
    c = conn()
    until = datetime(2026, 9, 29, 14, 0)
    live_store.pause(c, until, "手动暂停 30 分钟", "manual")
    assert live_store.paused(c) == {"until": until, "reason": "手动暂停 30 分钟", "kind": "manual"}


def test_clear_pause_makes_paused_return_none():
    c = conn()
    live_store.pause(c, None, "x", "auto")
    live_store.clear_pause(c)
    assert live_store.paused(c) is None


def test_paused_returns_none_when_never_paused():
    c = conn()
    assert live_store.paused(c) is None


# ---- pause_unless_auto：页面手动暂停的「检查 + 写入」在同一个 BEGIN IMMEDIATE 事务里 ----

def _file_conns(tmp_path, n=2):
    path = str(tmp_path / "jevq.sqlite")
    first = db.connect(path)
    return [first] + [sqlite3.connect(path, timeout=5, check_same_thread=False) for _ in range(n - 1)]


def test_pause_unless_auto_writes_manual_pause_and_returns_none():
    c = conn()
    until = datetime(2026, 9, 29, 14, 0)
    assert live_store.pause_unless_auto(c, until, "手机暂停") is None
    assert live_store.paused(c) == {"until": until, "reason": "手机暂停", "kind": "manual"}
    assert not c.in_transaction


def test_pause_unless_auto_refuses_when_auto_paused_and_returns_it_untouched():
    c = conn()
    live_store.pause(c, None, "撤单前核对不上", "auto")
    current = live_store.pause_unless_auto(c, datetime(2026, 9, 29, 14, 0), "手机暂停")
    assert current == {"until": None, "reason": "撤单前核对不上", "kind": "auto"}
    assert live_store.paused(c) == {"until": None, "reason": "撤单前核对不上", "kind": "auto"}
    assert not c.in_transaction      # 拒绝时回滚干净，不留开着的事务占着写锁


def test_pause_unless_auto_overwrites_an_existing_manual_pause():
    c = conn()
    live_store.pause(c, datetime(2026, 9, 29, 10, 30), "手机暂停", "manual")
    assert live_store.pause_unless_auto(c, None, "手机暂停") is None
    assert live_store.paused(c) == {"until": None, "reason": "手机暂停", "kind": "manual"}


def test_pause_unless_auto_holds_write_lock_between_check_and_write(tmp_path, monkeypatch):
    """页面检查完「不是 auto」之后、写入之前，执行器（另一个连接）自动暂停：它的写必须等页面提交，
    而不是插在中间被页面覆盖——检查和写入之间另一个连接写不进去，等页面提交后自动暂停排在后面，最终仍是 auto。"""
    page, other = _file_conns(tmp_path)
    check = sqlite3.connect(str(tmp_path / "jevq.sqlite"), timeout=5, check_same_thread=False)
    seen = {}
    real_paused = live_store.paused

    def paused_then_executor_tries_to_auto_pause(c):
        result = real_paused(c)
        if "thread" not in seen:
            def executor():
                live_store.pause(other, None, "执行器自动暂停", "auto")
            seen["thread"] = threading.Thread(target=executor)
            seen["thread"].start()
            time.sleep(0.3)      # 给执行器足够时间撞上写锁
            seen["during"] = real_paused(check)      # 页面还没提交：执行器的写必须还没落库
        return result

    monkeypatch.setattr(live_store, "paused", paused_then_executor_tries_to_auto_pause)
    assert live_store.pause_unless_auto(page, datetime(2026, 9, 29, 14, 0), "手机暂停") is None
    seen["thread"].join(timeout=10)

    assert seen["during"] is None
    monkeypatch.undo()
    final = live_store.paused(check)
    assert final["kind"] == "auto" and final["reason"] == "执行器自动暂停" and final["until"] is None


def test_pause_unless_auto_waits_for_a_concurrent_writer(tmp_path):
    """别的连接正占着写锁时，BEGIN IMMEDIATE 等它提交（不是立刻报错，也不是读到旧值就写）。"""
    page, other = _file_conns(tmp_path)
    other.execute("BEGIN IMMEDIATE")
    live_store._put_state(other, "paused", "1")
    live_store._put_state(other, "pause_reason", "执行器自动暂停")
    live_store._put_state(other, "pause_kind", "auto")
    result = {}

    def page_request():
        result["value"] = live_store.pause_unless_auto(page, datetime(2026, 9, 29, 14, 0), "手机暂停")

    t = threading.Thread(target=page_request)
    t.start()
    time.sleep(0.3)
    assert t.is_alive()          # 页面在等写锁
    other.commit()
    t.join(timeout=10)

    assert result["value"]["kind"] == "auto"      # 等到了之后读到的是 auto，拒绝，不覆盖
    assert live_store.paused(page)["reason"] == "执行器自动暂停"


def test_pause_unless_auto_rolls_back_and_reraises_on_write_failure(tmp_path, monkeypatch):
    c = conn()
    live_store.pause(c, datetime(2026, 9, 29, 10, 30), "手机暂停", "manual")

    def boom(conn_, key, value):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(live_store, "_put_state", boom)
    with pytest.raises(sqlite3.OperationalError):
        live_store.pause_unless_auto(c, None, "手机暂停")
    monkeypatch.undo()
    assert not c.in_transaction
    assert live_store.paused(c) == {"until": datetime(2026, 9, 29, 10, 30), "reason": "手机暂停", "kind": "manual"}
