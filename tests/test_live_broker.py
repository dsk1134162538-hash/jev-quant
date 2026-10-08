"""OrderRow.open 的判定逻辑，以及 FakeBroker 关键行为的最小验证。"""
from datetime import datetime

import pytest

from jevq.live.broker import (BrokerError, Holding, OrderRow, REJECT_CODES, INFLIGHT_CODES,
                              holding_from_row, is_cancel_row, order_from_row, orders_from_rows, cash_from_balance)
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


@pytest.mark.parametrize("raw, expected", [
    ("093105", "09:31:05"),
    ("93105", "09:31:05"),        # easytrader 按整数读掉了前导 0
    (93105, "09:31:05"),          # 直接就是整数
    ("9:31:05", "09:31:05"),      # 小时不补零
    ("09:31:05", "09:31:05"),
    ("145659", "14:56:59"),
    (" 93105 ", "09:31:05"),
    ("10:00:00", "10:00:00"),
])
def test_order_from_row_time_normalized_to_hh_mm_ss(raw, expected):
    row = {"委托时间": raw, "证券代码": "600001", "操作": "卖出", "委托价格": "9.5",
           "委托数量": "100", "合同编号": "7"}
    assert order_from_row(row).time == expected


def test_order_from_row_unrecognized_time_text_kept_as_is():
    row = {"委托时间": "上午", "证券代码": "600001", "操作": "卖出", "委托价格": "9.5",
           "委托数量": "100", "合同编号": "7"}
    assert order_from_row(row).time == "上午"


def test_order_from_row_time_sorts_correctly_after_normalization():
    """「最后一笔」按时间字符串排序：9:35:00 不补零会排到 10:00:00 后面；补零后顺序才对。"""
    def t(raw):
        return order_from_row({"委托时间": raw, "证券代码": "600001", "操作": "买入", "委托价格": "9.5",
                               "委托数量": "100", "合同编号": "7"}).time
    assert max([t("93500"), t("100000")]) == "10:00:00"
    assert max([t("9:35:00"), t("10:00:00")]) == "10:00:00"


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


# ---- is_cancel_row：撤单记录行不是委托 ----

@pytest.mark.parametrize("key, value", [("操作", "撤买"), ("操作", "撤卖"), ("买卖标志", "撤买"),
                                        ("买卖方向", "撤卖"), ("委托类别", "撤单"), ("操作", "证券撤买")])
def test_is_cancel_row_true_when_side_column_mentions_cancel(key, value):
    row = {"委托时间": "10:00:00", "证券代码": "600001", key: value, "委托价格": "9.5",
           "委托数量": "100", "合同编号": "7"}
    assert is_cancel_row(row) is True


@pytest.mark.parametrize("value", ["买入", "卖出", "证券买入", "证券卖出", "买"])
def test_is_cancel_row_false_for_ordinary_orders(value):
    row = {"委托时间": "10:00:00", "证券代码": "600001", "操作": value, "委托价格": "9.5",
           "委托数量": "100", "合同编号": "7", "备注": "已撤"}      # 备注里的「已撤」是状态，不是撤单行
    assert is_cancel_row(row) is False


def test_is_cancel_row_false_without_a_side_column():
    assert is_cancel_row({"foo": "撤单"}) is False


def test_orders_from_rows_parses_and_drops_cancel_records():
    rows = [{"委托时间": "93105", "证券代码": "600001", "操作": "买入", "委托价格": "9.5",
             "委托数量": "100", "合同编号": "1", "备注": "已撤"},
            {"委托时间": "93200", "证券代码": "600001", "操作": "撤买", "委托价格": "9.5",
             "委托数量": "100", "合同编号": "2", "备注": "已撤"}]
    orders = orders_from_rows(rows)
    assert [(o.entrust_no, o.time) for o in orders] == [("1", "09:31:05")]
    assert orders_from_rows([]) == []


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


# 2026-10-08 家里实盘客户端真实当日委托行：撤掉的委托不单列「撤买」行，而是原委托的「操作」列
# 直接变成「买入全部撤单」、备注「全部撤单(股票)」、撤消数量 = 委托数量。它是委托本身（已撤），不是撤单记录行；
# 当撤单记录行排除掉，撤单后就再也找不到这笔，程序会判「委托状态查不到」自动暂停。
REAL_CANCELLED = {"交易市场": "上海Ａ股", "合同编号": "2986", "备注": "全部撤单(股票)", "委托价格": "7.460",
                  "委托数量": "100", "委托时间": "10:01:49", "成交均价": "0.000", "成交数量": "0",
                  "撤消数量": "100", "操作": "买入全部撤单", "证券代码": "601398", "证券名称": "工商银行"}


@pytest.mark.parametrize("op", ["买入全部撤单", "卖出全部撤单", "买入部分撤单", "卖出部分撤单"])
def test_original_order_whose_side_column_shows_its_cancel_status_is_not_a_cancel_row(op):
    assert is_cancel_row({**REAL_CANCELLED, "操作": op}) is False


def test_real_fully_cancelled_order_parses_as_a_closed_order_with_its_contract_number():
    (o,) = orders_from_rows([REAL_CANCELLED])
    assert (o.entrust_no, o.code, o.side, o.price, o.shares, o.cancelled, o.time) ==         ("2986", "601398", "buy", 7.46, 100, 100, "10:01:49")
    assert o.open is False


def test_real_partially_cancelled_sell_parses_as_closed_with_its_fill():
    row = {**REAL_CANCELLED, "操作": "卖出部分撤单", "备注": "部分撤单(股票)", "成交数量": "300",
           "撤消数量": "200", "委托数量": "500", "成交均价": "7.470"}
    (o,) = orders_from_rows([row])
    assert o.side == "sell" and o.filled == 300 and o.cancelled == 200 and o.open is False
