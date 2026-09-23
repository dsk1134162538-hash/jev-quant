from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: float
    pre_close: float
    open: float
    high: float
    low: float
    volume: int        # 手
    amount: float      # 元
    limit_up: float
    limit_down: float
    ts: datetime

    def _pct(self, p: float) -> float:
        return round((p / self.pre_close - 1) * 100, 2)

    @property
    def pct(self) -> float:
        return self._pct(self.price)

    @property
    def open_pct(self) -> float:
        return self._pct(self.open)

    @property
    def low_pct(self) -> float:
        return self._pct(self.low)

    @property
    def suspended(self) -> bool:
        return self.volume <= 0 or self.price <= 0

    @property
    def is_st(self) -> bool:
        return "ST" in self.name.upper()


@dataclass(frozen=True)
class Position:
    account: str
    code: str
    shares: int
    cost: float        # 含买入费用
    buy_price: float
    buy_date: date


@dataclass(frozen=True)
class Fill:
    account: str
    code: str
    side: str          # "buy" / "sell"
    price: float
    shares: int
    fee: float
    ts: datetime
    reason: str
