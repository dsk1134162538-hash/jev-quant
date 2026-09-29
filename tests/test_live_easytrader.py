"""EasytraderBroker 测试：假 user + 注入 connect，永不 import easytrader。"""
import pytest

from jevq.live.broker import BrokerError, Holding
from jevq.live.easytrader_broker import EasytraderBroker


class TradeError(Exception):
    """名字必须叫 TradeError（适配器按类名判断，不 import easytrader.exceptions）。"""


class FakeUser:
    def __init__(self):
        self._balance = {"资金余额": 5000.0, "可用金额": 4000.0}
        self._position = [{"证券代码": "600001", "股票余额": 500, "可用余额": 500}]
        self._today_entrusts = []
        self.buy_result = {"entrust_no": "1"}
        self.sell_result = {"entrust_no": "1"}
        self.cancel_result = {"message": "撤单成功"}
        self.calls = []
        self.raise_on = {}   # 方法/属性名 -> 异常实例，触发一次后自动消耗

    def _check(self, name):
        if name in self.raise_on:
            raise self.raise_on.pop(name)

    @property
    def balance(self):
        self._check("balance")
        return self._balance

    @property
    def position(self):
        self._check("position")
        return self._position

    @property
    def today_entrusts(self):
        self._check("today_entrusts")
        return self._today_entrusts

    def buy(self, security, price=None, amount=None):
        self.calls.append(("buy", security, price, amount))
        self._check("buy")
        return self.buy_result

    def sell(self, security, price=None, amount=None):
        self.calls.append(("sell", security, price, amount))
        self._check("sell")
        return self.sell_result

    def cancel_entrust(self, entrust_no):
        self.calls.append(("cancel_entrust", entrust_no))
        self._check("cancel_entrust")
        return self.cancel_result


def _broker(user, exe="C:/xiadan.exe"):
    return EasytraderBroker(exe, connect=lambda: user)


# ---- balance / positions / orders_today ----

def test_balance_uses_cash_from_balance():
    assert _broker(FakeUser()).balance() == 4000.0


def test_positions_parses_into_holding_dict():
    assert _broker(FakeUser()).positions() == {"600001": Holding("600001", 500, 500)}


def test_positions_none_raises_read_failed():
    user = FakeUser()
    user._position = None
    with pytest.raises(BrokerError) as exc:
        _broker(user).positions()
    assert exc.value.code == "READ_FAILED"


def test_orders_today_parses_rows():
    user = FakeUser()
    user._today_entrusts = [{"委托时间": "093105", "证券代码": "600001", "操作": "买入",
                             "委托价格": "9.5", "委托数量": "500", "合同编号": "7"}]
    [order] = _broker(user).orders_today()
    assert order.time == "09:31:05" and order.side == "buy" and order.entrust_no == "7"


# ---- place ----

def test_place_buy_rounds_price_and_returns_entrust_no():
    user = FakeUser()
    user.buy_result = {"entrust_no": "555"}
    no = _broker(user).place("buy", "600001", 9.501, 100)
    assert no == "555"
    assert user.calls == [("buy", "600001", 9.5, 100)]


def test_place_sell_calls_sell_not_buy():
    user = FakeUser()
    _broker(user).place("sell", "600001", 9.5, 100)
    assert user.calls == [("sell", "600001", 9.5, 100)]


def test_place_no_entrust_no_in_result_returns_none():
    user = FakeUser()
    user.buy_result = {"message": "success"}
    assert _broker(user).place("buy", "600001", 9.5, 100) is None


def test_place_trade_error_maps_to_order_submit_failed():
    user = FakeUser()
    user.raise_on["buy"] = TradeError("涨跌停限制")
    with pytest.raises(BrokerError) as exc:
        _broker(user).place("buy", "600001", 9.5, 100)
    assert exc.value.code == "ORDER_SUBMIT_FAILED"
    assert "涨跌停限制" in str(exc.value)


@pytest.mark.parametrize("code", ["60000", "700001", "1234567"])
def test_place_rejects_invalid_code_without_calling_user(code):
    user = FakeUser()
    with pytest.raises(ValueError):
        _broker(user).place("buy", code, 9.5, 100)
    assert user.calls == []


@pytest.mark.parametrize("shares", [0, -100, 150])
def test_place_rejects_invalid_shares_without_calling_user(shares):
    user = FakeUser()
    with pytest.raises(ValueError):
        _broker(user).place("buy", "600001", 9.5, shares)
    assert user.calls == []


# ---- cancel_last ----

def test_cancel_last_picks_latest_time_open_order():
    user = FakeUser()
    user._today_entrusts = [
        {"委托时间": "09:31:00", "证券代码": "600001", "操作": "买入", "委托价格": "9.5",
         "委托数量": "100", "合同编号": "1", "备注": "未成交"},
        {"委托时间": "09:35:00", "证券代码": "600001", "操作": "买入", "委托价格": "9.5",
         "委托数量": "100", "合同编号": "2", "备注": "未成交"},
        {"委托时间": "09:33:00", "证券代码": "600001", "操作": "买入", "委托价格": "9.5",
         "委托数量": "100", "合同编号": "3", "备注": "已成"},
    ]
    assert _broker(user).cancel_last() is True
    assert user.calls[-1] == ("cancel_entrust", "2")


def test_cancel_last_false_when_nothing_open():
    user = FakeUser()
    user._today_entrusts = [{"委托时间": "09:31:00", "证券代码": "600001", "操作": "买入",
                             "委托价格": "9.5", "委托数量": "100", "合同编号": "1", "备注": "已成"}]
    assert _broker(user).cancel_last() is False
    assert user.calls == []


def test_cancel_last_true_when_message_equals_success_literal():
    user = FakeUser()
    user._today_entrusts = [{"委托时间": "09:31:00", "证券代码": "600001", "操作": "买入",
                             "委托价格": "9.5", "委托数量": "100", "合同编号": "1", "备注": "未成交"}]
    user.cancel_result = {"message": "success"}
    assert _broker(user).cancel_last() is True


def test_cancel_last_false_when_message_lacks_success_marker():
    user = FakeUser()
    user._today_entrusts = [{"委托时间": "09:31:00", "证券代码": "600001", "操作": "买入",
                             "委托价格": "9.5", "委托数量": "100", "合同编号": "1", "备注": "未成交"}]
    user.cancel_result = {"message": "网络异常"}
    assert _broker(user).cancel_last() is False


# ---- 连接管理 ----

def test_reconnects_after_exception():
    user = FakeUser()
    connect_calls = []

    def connect():
        connect_calls.append(1)
        return user

    broker = EasytraderBroker("C:/xiadan.exe", connect=connect)
    assert broker.balance() == 4000.0
    assert len(connect_calls) == 1

    user.raise_on["balance"] = RuntimeError("断线")
    with pytest.raises(BrokerError):
        broker.balance()

    assert broker.balance() == 4000.0
    assert len(connect_calls) == 2


def test_default_connect_not_used_when_connect_injected():
    # 注入 connect 时，构造函数不应触发默认实现（默认实现里才 import easytrader）。
    EasytraderBroker("C:/xiadan.exe", connect=lambda: FakeUser())
    import sys
    assert "easytrader" not in sys.modules


def test_never_imports_easytrader_module():
    import sys
    assert "easytrader" not in sys.modules
