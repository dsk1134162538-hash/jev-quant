"""OrderRow.open 的判定逻辑，以及 FakeBroker 关键行为的最小验证。"""
from datetime import datetime

import pytest

from jevq.live.broker import BrokerError, Holding, OrderRow, REJECT_CODES
from live_fakes import FakeBroker, FakeClock, FakeNotifier


# ---- OrderRow.open ----

def test_order_row_open_true_when_partially_filled():
    o = OrderRow("1", "600001", "buy", 9.5, 500, 200, 9.5, "部成", "09:31:00")
    assert o.open is True


def test_order_row_open_true_when_freshly_reported_and_unfilled():
    o = OrderRow("1", "600001", "buy", 9.5, 500, 0, 0.0, "已报", "09:31:00")
    assert o.open is True


def test_order_row_open_false_when_fully_filled():
    o = OrderRow("1", "600001", "buy", 9.5, 500, 500, 9.5, "已成", "09:31:00")
    assert o.open is False


def test_order_row_open_false_when_cancelled_despite_unfilled_shares():
    o = OrderRow("1", "600001", "buy", 9.5, 500, 0, 0.0, "已撤", "09:31:00")
    assert o.open is False


def test_order_row_open_false_when_rejected():
    o = OrderRow("1", "600001", "buy", 9.5, 500, 0, 0.0, "废单", "09:31:00")
    assert o.open is False


# ---- BrokerError / REJECT_CODES ----

def test_broker_error_defaults_to_unknown_code():
    assert BrokerError("出错了").code == "UNKNOWN"


def test_reject_codes_contains_definite_rejections():
    assert {"INSUFFICIENT_BALANCE", "ORDER_SUBMIT_FAILED"} <= REJECT_CODES


# ---- FakeBroker ----

def test_fake_broker_fill_all_updates_cash_and_holdings():
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    b = FakeBroker(clock, cash=10000.0)
    no = b.place("buy", "600001", 9.5, 500)
    assert no == "1"
    assert b.cash == pytest.approx(10000.0 - 500 * 9.5)
    assert b.holdings["600001"] == [500, 0]
    [order] = b.orders_today()
    assert order.status == "已成" and order.filled == 500 and order.time == "09:31:00"


def test_fake_broker_fill_none_leaves_order_open_and_cash_untouched():
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    b = FakeBroker(clock, cash=10000.0)
    b.fill_mode = "none"
    b.place("buy", "600001", 9.5, 500)
    assert b.cash == 10000.0
    [order] = b.orders_today()
    assert order.filled == 0 and order.open is True


def test_fake_broker_partial_rounds_down_to_lot():
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    b = FakeBroker(clock, cash=10000.0)
    b.fill_mode, b.fill_ratio = "partial", 0.55
    b.place("buy", "600001", 9.5, 500)
    [order] = b.orders_today()
    assert order.filled == 200 and order.status == "部成"


def test_fake_broker_late_fills_on_nth_poll_only():
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    b = FakeBroker(clock, cash=10000.0)
    b.fill_mode, b.late_polls = "late", 3
    b.place("buy", "600001", 9.5, 500)
    assert b.orders_today()[0].filled == 0
    assert b.orders_today()[0].filled == 0
    assert b.orders_today()[0].filled == 500


def test_fake_broker_sell_reduces_shares_and_available():
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    b = FakeBroker(clock, cash=0.0, holdings={"600001": [500, 500]})
    b.place("sell", "600001", 9.6, 500)
    assert b.holdings["600001"] == [0, 0]
    assert b.cash == pytest.approx(500 * 9.6)


def test_fake_broker_positions_returns_holding_dataclasses():
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    b = FakeBroker(clock, cash=0.0, holdings={"600001": [500, 300]})
    assert b.positions() == {"600001": Holding("600001", 500, 300)}


def test_fake_broker_cancel_last_marks_partial_fill_as_partial_cancel():
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    b = FakeBroker(clock, cash=10000.0)
    b.fill_mode, b.fill_ratio = "partial", 0.4
    b.place("buy", "600001", 9.5, 500)
    assert b.cancel_last() is True
    [order] = b.orders_today()
    assert order.status == "部撤" and order.open is False


def test_fake_broker_cancel_last_returns_false_when_nothing_open():
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    b = FakeBroker(clock, cash=10000.0)
    assert b.cancel_last() is False


def test_fake_broker_extra_orders_appear_in_orders_today_but_are_not_cancellable():
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    b = FakeBroker(clock, cash=10000.0)
    manual = OrderRow("99", "600002", "buy", 8.0, 100, 100, 8.0, "已成", "09:20:00")
    b.extra_orders = [manual]
    assert b.orders_today() == [manual]
    assert b.cancel_last() is False


def test_fake_broker_place_error_is_one_shot():
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    b = FakeBroker(clock, cash=10000.0)
    b.place_error = BrokerError("资金不足", "INSUFFICIENT_BALANCE")
    with pytest.raises(BrokerError) as exc:
        b.place("buy", "600001", 9.5, 500)
    assert exc.value.code == "INSUFFICIENT_BALANCE"
    assert b.place_error is None
    assert b.place("buy", "600001", 9.5, 500) == "1"    # 第二次不再抛


def test_fake_broker_fail_reads_counts_down_then_recovers():
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    b = FakeBroker(clock, cash=10000.0)
    b.fail_reads = 2
    with pytest.raises(BrokerError):
        b.balance()
    with pytest.raises(BrokerError):
        b.positions()
    assert b.balance() == 10000.0


def test_fake_broker_records_calls():
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    b = FakeBroker(clock, cash=10000.0)
    b.place("buy", "600001", 9.5, 500)
    b.balance()
    b.positions()
    b.orders_today()
    b.cancel_last()
    assert b.calls == [("place", "buy", "600001", 9.5, 500), ("balance",), ("positions",),
                       ("orders_today",), ("cancel_last",)]


# ---- FakeNotifier ----

def test_fake_notifier_once_records_same_key_only_once():
    n = FakeNotifier()
    n.once("k", "标题", "内容1")
    n.once("k", "标题", "内容2")
    assert n.sent == [("标题", "内容1")]


def test_fake_notifier_send_always_appends():
    n = FakeNotifier()
    n.send("a", "1")
    n.send("a", "1")
    assert n.sent == [("a", "1"), ("a", "1")]
