"""xiadan-gateway HTTP 适配器。见 research-gateway.md（源码审查结论）。"""
import re
import uuid

import requests

from jevq.live.broker import (BrokerError, Holding, OrderRow, cash_from_balance, holding_from_row, is_cancel_row,
                              order_from_row)

_CODE_RE = re.compile(r"^(60|00)\d{4}$")


class GatewayBroker:
    def __init__(self, base_url: str, token: str | None, session=None, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, path: str, *, json=None, extra_headers=None) -> dict:
        try:
            resp = self.session.request(method, self.base_url + path, headers=self._headers(extra_headers),
                                        timeout=self.timeout, json=json)
        except requests.Timeout as e:
            raise BrokerError(str(e), "TIMEOUT") from e
        except requests.RequestException as e:
            raise BrokerError(str(e), "UNREACHABLE") from e
        try:
            body = resp.json()
        except ValueError as e:
            raise BrokerError(f"响应不是 JSON（HTTP {resp.status_code}）", "BAD_RESPONSE") from e
        if body.get("status") != "success":
            raise BrokerError(body.get("message", "网关返回失败"), body.get("error_code", "UNKNOWN"))
        return body.get("data", {})

    def health(self) -> dict:
        return self._request("GET", "/health")

    def balance(self) -> float:
        return cash_from_balance(self._request("GET", "/account/balance"))

    def positions(self) -> dict[str, Holding]:
        rows = self._request("GET", "/positions")
        holdings = [holding_from_row(r) for r in rows]
        return {h.code: h for h in holdings}

    def place(self, side: str, code: str, price: float, shares: int) -> str | None:
        # 网关不校验，直接当按键敲：调用方自己保证 6 位代码、正整数 100 倍数数量、明确价格。
        if side not in ("buy", "sell"):
            raise ValueError(f"非法方向：{side}")
        if not _CODE_RE.match(code):
            raise ValueError(f"非法代码：{code}")
        if shares <= 0 or shares % 100 != 0:
            raise ValueError(f"非法股数：{shares}")
        if price <= 0:
            raise ValueError(f"非法价格：{price}")
        payload = {"code": code, "status": "1" if side == "buy" else "2",
                   "amount": str(shares), "price": f"{price:.2f}", "price_type": "limit"}
        data = self._request("POST", "/orders", json=payload,
                             extra_headers={"Idempotency-Key": uuid.uuid4().hex})
        return data.get("entrust_no")

    def orders_today(self) -> list[OrderRow]:
        rows = self._request("GET", "/orders/pending")
        return [order_from_row(r) for r in rows if not is_cancel_row(r)]      # 撤单记录行不是委托

    def cancel_last(self) -> bool:
        data = self._request("POST", "/orders/cancel-all", json={"type": "L"})
        return bool(data.get("success"))
