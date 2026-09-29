"""OrderRow.open 的判定逻辑，以及 FakeBroker 关键行为的最小验证。"""
from datetime import datetime

import pytest

from jevq.live.broker import (BrokerError, Holding, OrderRow, REJECT_CODES, INFLIGHT_CODES,
                              holding_from_row, order_from_row, cash_from_balance)
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


def test_order_row_open_true_when_status_text_unfilled():
    o = OrderRow("1", "600001", "buy", 9.5, 500, 0, 0.0, "未成交", "09:31:00")
    assert o.open is True


def test_order_row_open_false_when_status_all_cancelled_variant():
    o = OrderRow("1", "600001", "buy", 9.5, 500, 0, 0.0, "全部撤单", "09:31:00")
    assert o.open is False


def test_order_row_open_false_when_filled_plus_cancelled_equals_shares():
    o = OrderRow("1", "600001", "buy", 9.5, 500, 300, 9.5, "部成部撤", "09:31:00", cancelled=200)
    assert o.open is False


def test_order_row_open_true_when_status_reported_pending_cancel():
    o = OrderRow("1", "600001", "buy", 9.5, 500, 0, 0.0, "已报待撤", "09:31:00")
    assert o.open is True


def test_order_row_cancelled_defaults_to_zero():
    o = OrderRow("1", "600001", "buy", 9.5, 500, 0, 0.0, "已报", "09:31:00")
    assert o.cancelled == 0


# ---- BrokerError / REJECT_CODES / INFLIGHT_CODES ----

def test_broker_error_defaults_to_unknown_code():
    assert BrokerError("出错了").code == "UNKNOWN"


def test_reject_codes_contains_definite_rejections():
    assert {"INSUFFICIENT_BALANCE", "ORDER_SUBMIT_FAILED", "VALIDATION_ERROR",
            "ORDER_PRICE_REQUIRED", "INPUT_VERIFY_FAILED"} <= REJECT_CODES


def test_inflight_codes_contains_timeout_variants():
    assert INFLIGHT_CODES == {"QUEUE_TIMEOUT", "TASK_TIMEOUT", "TASK_TIMEOUT_RECOVERY_FAILED", "TIMEOUT"}


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


# ---- holding_from_row ----

def test_holding_from_row_gateway_sample_header():
    row = {"序号": "1", "证券代码": "600001", "证券名称": "平安银行", "股票余额": "500", "可用余额": "500",
           "冻结数量": "0", "成本价": "9.50", "市价": "9.60", "市值": "4800", "盈亏": "50",
           "盈亏比例(%)": "1.05", "当日盈亏": "50", "当日盈亏比(%)": "1.05", "当日买入": "0",
           "当日卖出": "0", "仓位占比(%)": "10", "交易市场": "深A", "操作": ""}
    assert holding_from_row(row) == Holding("600001", 500, 500)


def test_holding_from_row_easytrader_variant_header():
    # research-easytrader.md：另一种表头变体，用「当前持仓」「可卖数量」
    row = {"证券代码": "600001", "当前持仓": "500", "可卖数量": "300", "参考成本价": "9.50",
           "盈亏比例(%)": "1.05"}
    assert holding_from_row(row) == Holding("600001", 500, 300)


def test_holding_from_row_code_padded_to_six_digits():
    row = {"证券代码": "1216", "股票余额": "100", "可用余额": "100"}
    assert holding_from_row(row).code == "001216"


def test_holding_from_row_code_stripped_and_last_six_digits():
    row = {"证券代码": " SH600001 ", "股票余额": "100", "可用余额": "100"}
    assert holding_from_row(row).code == "600001"


def test_holding_from_row_accepts_comma_and_blank_numbers():
    row = {"证券代码": "600001", "股票余额": "1,000", "可用余额": ""}
    h = holding_from_row(row)
    assert h.shares == 1000 and h.available == 0


def test_holding_from_row_unknown_header_raises_and_lists_actual_keys():
    row = {"foo": "1", "bar": "2"}
    with pytest.raises(BrokerError) as exc:
        holding_from_row(row)
    assert exc.value.code == "UNKNOWN_HEADER"
    assert "foo" in str(exc.value) and "bar" in str(exc.value)


# ---- order_from_row ----

def test_order_from_row_gateway_sample_header():
    row = {"委托时间": "093105", "证券代码": "600001", "证券名称": "平安银行", "操作": "买入",
           "委托价格": "9.50", "委托数量": "500", "成交数量": "0", "成交均价": "0.00",
           "撤消数量": "0", "合同编号": "1234567", "备注": "未成交", "交易市场": "深A"}
    o = order_from_row(row)
    assert o == OrderRow("1234567", "600001", "buy", 9.5, 500, 0, 0.0, "未成交", "09:31:05", cancelled=0)


def test_order_from_row_time_hhmmss_normalized():
    row = {"委托时间": "093105", "证券代码": "600001", "操作": "卖出", "委托价格": "9.5",
           "委托数量": "100", "合同编号": "7"}
    assert order_from_row(row).time == "09:31:05"


def test_order_from_row_time_already_normalized_passes_through():
    row = {"委托时间": "09:31:05", "证券代码": "600001", "操作": "卖出", "委托价格": "9.5",
           "委托数量": "100", "合同编号": "7"}
    assert order_from_row(row).time == "09:31:05"


def test_order_from_row_side_from_category_keyword():
    row = {"委托时间": "10:00:00", "证券代码": "600001", "委托类别": "卖出", "委托价格": "9.5",
           "委托数量": "100", "委托编号": "7"}
    assert order_from_row(row).side == "sell"


def test_order_from_row_comma_number_in_shares():
    row = {"委托时间": "10:00:00", "证券代码": "600001", "操作": "买入", "委托价格": "9.5",
           "委托数量": "1,000", "合同编号": "7"}
    assert order_from_row(row).shares == 1000


def test_order_from_row_defaults_for_missing_optional_fields():
    row = {"委托时间": "10:00:00", "证券代码": "600001", "操作": "买入", "委托价格": "9.5",
           "委托数量": "100", "合同编号": "7"}
    o = order_from_row(row)
    assert o.filled == 0 and o.avg_price == 0.0 and o.cancelled == 0 and o.status == ""


def test_order_from_row_unknown_header_raises_and_lists_actual_keys():
    row = {"foo": "1"}
    with pytest.raises(BrokerError) as exc:
        order_from_row(row)
    assert exc.value.code == "UNKNOWN_HEADER"
    assert "foo" in str(exc.value)


def test_order_from_row_unrecognized_side_text_raises_unknown_header():
    row = {"委托时间": "10:00:00", "证券代码": "600001", "操作": "撤单", "委托价格": "9.5",
           "委托数量": "100", "合同编号": "7"}
    with pytest.raises(BrokerError) as exc:
        order_from_row(row)
    assert exc.value.code == "UNKNOWN_HEADER"


# ---- cash_from_balance ----

def test_cash_from_balance_gateway_key_with_comma():
    assert cash_from_balance({"资金余额": "10000.00", "可用金额": "9,500.00"}) == 9500.0


def test_cash_from_balance_easytrader_key_numeric():
    assert cash_from_balance({"可用金额": 4000.0}) == 4000.0


def test_cash_from_balance_alt_key_available_balance():
    assert cash_from_balance({"可用余额": "100"}) == 100.0


def test_cash_from_balance_unknown_header_raises():
    with pytest.raises(BrokerError) as exc:
        cash_from_balance({"foo": "1"})
    assert exc.value.code == "UNKNOWN_HEADER"
