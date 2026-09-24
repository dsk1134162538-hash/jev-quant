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
    turnover: float = 0.0      # 换手率 %
    volume_ratio: float = 0.0  # 量比
    float_mcap: float = 0.0    # 流通市值，亿
    total_mcap: float = 0.0    # 总市值，亿
    pe_ttm: float = 0.0        # 市盈(TTM)
    pe_dyn: float = 0.0        # 市盈(动)
    pe_static: float = 0.0     # 市盈(静)
    pb: float = 0.0            # 市净率
    amplitude: float = 0.0     # 振幅 %
    outer_vol: float = 0.0     # 外盘，手
    inner_vol: float = 0.0     # 内盘，手
    avg_price: float = 0.0     # 均价
    ytd_pct: float = 0.0       # 今年来涨幅 %
    float_shares: float = 0.0  # 流通股本，股
    total_shares: float = 0.0  # 总股本，股
    bid_vols: tuple = ()       # 买一~买五量，手
    ask_vols: tuple = ()       # 卖一~卖五量，手
    bid1: float = 0.0          # 买一价

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
