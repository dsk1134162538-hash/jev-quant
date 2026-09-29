"""下单层接口。真实实现在 jevq.live.gateway / jevq.live.easytrader_broker（后续任务），测试用 tests/live_fakes.FakeBroker。"""
from dataclasses import dataclass
from typing import Protocol

# 券商明确拒单、委托肯定没报出去的错误码：记 failed，不自动暂停
REJECT_CODES = frozenset({"INSUFFICIENT_BALANCE", "INSUFFICIENT_SHARES", "SHORT_SELLING_FORBIDDEN",
                          "T1_RESTRICTION", "PRICE_OUT_OF_RANGE", "OUTSIDE_TRADING_HOURS",
                          "SERVER_CLEARING", "ORDER_SUBMIT_FAILED"})


class BrokerError(Exception):
    def __init__(self, message: str, code: str = "UNKNOWN"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Holding:
    code: str
    shares: int      # 股票余额
    available: int   # 可用余额


_CLOSED = ("已成", "已撤", "部撤", "废")


@dataclass(frozen=True)
class OrderRow:
    entrust_no: str
    code: str
    side: str         # "buy" / "sell"
    price: float
    shares: int       # 委托数量
    filled: int       # 成交数量
    avg_price: float  # 成交均价，没有为 0.0
    status: str       # 客户端原文：未报/已报/部成/已成/已撤/部撤/废单…
    time: str         # "HH:MM:SS"

    @property
    def open(self) -> bool:
        return self.filled < self.shares and not any(s in self.status for s in _CLOSED)


class Broker(Protocol):
    def balance(self) -> float: ...
    def positions(self) -> dict[str, Holding]: ...
    def place(self, side: str, code: str, price: float, shares: int) -> str | None: ...
    def orders_today(self) -> list[OrderRow]: ...
    def cancel_last(self) -> bool: ...
