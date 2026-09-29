"""GatewayBroker 测试：假 session 记录请求（method/url/json/headers），返回可设定的 JSON / 非 JSON 响应。"""
import pytest
import requests

from jevq.live.broker import BrokerError, Holding
from jevq.live.gateway import GatewayBroker


class FakeResponse:
    def __init__(self, status_code=200, data=None, raise_on_json=None):
        self.status_code = status_code
        self._data = data
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise self._raise_on_json
        return self._data


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.calls = []
        self._response = response
        self._exc = exc

    def request(self, method, url, headers=None, timeout=None, json=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "timeout": timeout, "json": json})
        if self._exc:
            raise self._exc
        return self._response


def _ok(data):
    return FakeResponse(200, {"status": "success", "data": data})


def _err(error_code, message="出错了"):
    return FakeResponse(200, {"status": "error", "error_code": error_code, "message": message})


def _broker(session, token="tok123"):
    return GatewayBroker("http://127.0.0.1:5000", token, session=session)


# ---- 正常解析 ----

def test_health_returns_data_as_is_without_raising_on_logged_in_false():
    session = FakeSession(_ok({"service": "xiadan-gateway", "logged_in": False}))
    assert _broker(session).health() == {"service": "xiadan-gateway", "logged_in": False}


def test_balance_parses_via_cash_from_balance():
    session = FakeSession(_ok({"资金余额": "10000.00", "可用金额": "9,500.00"}))
    assert _broker(session).balance() == 9500.0


def test_positions_parses_rows_into_holding_dict():
    row = {"证券代码": "600001", "股票余额": "500", "可用余额": "500"}
    session = FakeSession(_ok([row]))
    assert _broker(session).positions() == {"600001": Holding("600001", 500, 500)}


def test_orders_today_parses_rows_into_order_rows():
    row = {"委托时间": "093105", "证券代码": "600001", "操作": "买入", "委托价格": "9.5",
           "委托数量": "500", "成交数量": "0", "成交均价": "0", "撤消数量": "0",
           "合同编号": "123", "备注": "未成交"}
    session = FakeSession(_ok([row]))
    [order] = _broker(session).orders_today()
    assert order.entrust_no == "123" and order.time == "09:31:05" and order.side == "buy"


# ---- place：JSON 与请求头 ----

def test_place_sends_expected_json_and_headers():
    session = FakeSession(_ok({"entrust_no": "999"}))
    no = _broker(session).place("buy", "600001", 9.5, 100)
    assert no == "999"
    [call] = session.calls
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:5000/orders"
    assert call["json"] == {"code": "600001", "status": "1", "amount": "100",
                            "price": "9.50", "price_type": "limit"}
    assert call["headers"]["Authorization"] == "Bearer tok123"
    assert "Idempotency-Key" in call["headers"]
    assert "Origin" not in call["headers"]


def test_place_sell_status_is_2():
    session = FakeSession(_ok({"entrust_no": "1"}))
    _broker(session).place("sell", "600001", 9.5, 100)
    assert session.calls[0]["json"]["status"] == "2"


def test_place_two_calls_use_different_idempotency_keys():
    session = FakeSession(_ok({"entrust_no": "1"}))
    broker = _broker(session)
    broker.place("buy", "600001", 9.5, 100)
    broker.place("buy", "600001", 9.5, 100)
    keys = [c["headers"]["Idempotency-Key"] for c in session.calls]
    assert keys[0] != keys[1]


def test_place_returns_none_when_entrust_no_not_captured():
    session = FakeSession(_ok({}))
    assert _broker(session).place("buy", "600001", 9.5, 100) is None


def test_place_no_token_omits_authorization_header():
    session = FakeSession(_ok({"entrust_no": "1"}))
    _broker(session, token=None).place("buy", "600001", 9.5, 100)
    assert "Authorization" not in session.calls[0]["headers"]


# ---- place：边界校验，非法输入不发请求 ----

@pytest.mark.parametrize("code", ["60000", "700001", "abcdef", "1234567"])
def test_place_rejects_invalid_code_without_request(code):
    session = FakeSession()
    with pytest.raises(ValueError):
        _broker(session).place("buy", code, 9.5, 100)
    assert session.calls == []


@pytest.mark.parametrize("shares", [0, -100, 150])
def test_place_rejects_invalid_shares_without_request(shares):
    session = FakeSession()
    with pytest.raises(ValueError):
        _broker(session).place("buy", "600001", 9.5, shares)
    assert session.calls == []


@pytest.mark.parametrize("price", [0, -1.0])
def test_place_rejects_invalid_price_without_request(price):
    session = FakeSession()
    with pytest.raises(ValueError):
        _broker(session).place("buy", "600001", price, 100)
    assert session.calls == []


# ---- cancel_last ----

def test_cancel_last_sends_type_l_and_returns_success_bool():
    session = FakeSession(_ok({"success": True, "cancelled_count": 1}))
    broker = _broker(session)
    assert broker.cancel_last() is True
    assert session.calls[0]["json"] == {"type": "L"}
    assert session.calls[0]["url"] == "http://127.0.0.1:5000/orders/cancel-all"


def test_cancel_last_returns_false_when_nothing_to_cancel():
    session = FakeSession(_ok({"success": False, "reason": "无可撤委托"}))
    assert _broker(session).cancel_last() is False


# ---- 错误处理 ----

def test_error_envelope_raises_broker_error_with_code():
    session = FakeSession(_err("INSUFFICIENT_BALANCE", "资金不足"))
    with pytest.raises(BrokerError) as exc:
        _broker(session).balance()
    assert exc.value.code == "INSUFFICIENT_BALANCE"
    assert "资金不足" in str(exc.value)


def test_timeout_raises_broker_error_timeout():
    session = FakeSession(exc=requests.Timeout("超时"))
    with pytest.raises(BrokerError) as exc:
        _broker(session).balance()
    assert exc.value.code == "TIMEOUT"


def test_connection_error_raises_broker_error_unreachable():
    session = FakeSession(exc=requests.ConnectionError("连不上"))
    with pytest.raises(BrokerError) as exc:
        _broker(session).balance()
    assert exc.value.code == "UNREACHABLE"


def test_non_json_response_raises_broker_error_bad_response_with_status_code():
    response = FakeResponse(502, raise_on_json=ValueError("not json"))
    session = FakeSession(response)
    with pytest.raises(BrokerError) as exc:
        _broker(session).balance()
    assert exc.value.code == "BAD_RESPONSE"
    assert "502" in str(exc.value)
