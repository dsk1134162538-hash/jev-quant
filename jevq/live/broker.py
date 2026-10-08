"""下单层接口。真实实现在 jevq.live.gateway / jevq.live.easytrader_broker（后续任务），测试用 tests/live_fakes.FakeBroker。"""
import re
from dataclasses import dataclass
from typing import Protocol

# 券商明确拒单、委托肯定没报出去的错误码：记 failed，不自动暂停
REJECT_CODES = frozenset({"INSUFFICIENT_BALANCE", "INSUFFICIENT_SHARES", "SHORT_SELLING_FORBIDDEN",
                          "T1_RESTRICTION", "PRICE_OUT_OF_RANGE", "OUTSIDE_TRADING_HOURS", "SERVER_CLEARING",
                          "ORDER_SUBMIT_FAILED", "VALIDATION_ERROR", "ORDER_PRICE_REQUIRED",
                          "INPUT_VERIFY_FAILED"})

# 结果不明、单子可能稍后才报出去的错误码：不能当拒单，也不能当成功，靠对账确认
INFLIGHT_CODES = frozenset({"QUEUE_TIMEOUT", "TASK_TIMEOUT", "TASK_TIMEOUT_RECOVERY_FAILED", "TIMEOUT"})


class BrokerError(Exception):
    def __init__(self, message: str, code: str = "UNKNOWN"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Holding:
    code: str
    shares: int      # 股票余额
    available: int   # 可用余额


_CLOSED = ("已成", "全部成交", "已撤", "部撤", "全部撤单", "部分撤单", "废")   # 「已报待撤」「待撤」仍算 open


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
    cancelled: int = 0   # 撤消数量

    @property
    def open(self) -> bool:
        return self.filled + self.cancelled < self.shares and not any(s in self.status for s in _CLOSED)


class Broker(Protocol):
    def balance(self) -> float: ...
    def positions(self) -> dict[str, Holding]: ...
    def place(self, side: str, code: str, price: float, shares: int) -> str | None: ...
    def orders_today(self) -> list[OrderRow]: ...
    def cancel_last(self) -> bool: ...


# ---- 表头映射：gateway、easytrader 两个适配器共用 ----

def _num(v) -> float:
    """数字转 float：字符串允许逗号、首尾空白，空串按 0；已经是数字原样转。"""
    if v is None:
        return 0.0
    s = str(v).replace(",", "").strip()
    return float(s) if s else 0.0


def _pick(row: dict, *keys: str) -> str | None:
    for k in keys:
        if k in row:
            return k
    return None


def _unknown_header(row: dict) -> BrokerError:
    return BrokerError(f"无法识别的表头：{list(row.keys())}", "UNKNOWN_HEADER")


def _code(row: dict) -> str:
    key = _pick(row, "证券代码")
    if key is None:
        raise _unknown_header(row)
    return str(row[key]).strip()[-6:].zfill(6)


def holding_from_row(row: dict) -> Holding:
    shares_key = _pick(row, "股票余额", "当前持仓", "证券数量", "实际数量")
    available_key = _pick(row, "可用余额", "可卖数量", "可用股份", "可用数量")
    if shares_key is None or available_key is None:
        raise _unknown_header(row)
    return Holding(code=_code(row), shares=int(_num(row[shares_key])), available=int(_num(row[available_key])))


_SIDE_KEYS = ("操作", "买卖标志", "买卖方向", "委托类别")
_CANCEL_STATUS_IN_SIDE = ("全部撤单", "部分撤单")   # 「操作」列里带的是原委托的撤单状态
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")


def is_cancel_row(row: dict) -> bool:
    """当日委托里的撤单记录行（操作/买卖标志列是「撤买」「撤卖」「撤单」之类）：那是一条撤单动作，不是委托。
    它含「买」「卖」字，不排除的话会被解析成一笔四要素和原委托完全相同的委托，
    没有委托号时「取最后一条」就可能匹配到它。gateway、easytrader 两个适配器的 orders_today 都要先排除。
    但有的客户端（2026-10-08 家里实测）不单列撤单行，而是把原委托的「操作」写成「买入全部撤单」「卖出部分撤单」：
    那是委托本身（状态已撤/部撤），必须保留，否则撤单后就找不到这笔委托。"""
    key = _pick(row, *_SIDE_KEYS)
    if key is None:
        return False
    text = str(row[key])
    return "撤" in text and not any(s in text for s in _CANCEL_STATUS_IN_SIDE)


def _normalize_time(raw) -> str:
    """委托时间统一成 "HH:MM:SS"（字符串比较才等于时间先后，「取最后一笔」和 ≥ 下单时刻都靠它）：
    "093105" 和已经规范的照旧；"93105"（easytrader 按整数读掉了前导 0）、"9:31:05"（小时不补零）补零。
    认不出的原样返回。"""
    t = str(raw).strip()
    if t.isdigit() and len(t) in (5, 6):
        t = t.zfill(6)
        return f"{t[0:2]}:{t[2:4]}:{t[4:6]}"
    m = _TIME_RE.fullmatch(t)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3)}"
    return t


def order_from_row(row: dict) -> OrderRow:
    entrust_key = _pick(row, "合同编号", "委托编号")
    side_key = _pick(row, *_SIDE_KEYS)
    price_key = _pick(row, "委托价格")
    shares_key = _pick(row, "委托数量")
    time_key = _pick(row, "委托时间")
    if None in (entrust_key, side_key, price_key, shares_key, time_key):
        raise _unknown_header(row)

    side_text = str(row[side_key])
    if "买" in side_text:
        side = "buy"
    elif "卖" in side_text:
        side = "sell"
    else:
        raise _unknown_header(row)

    filled_key = _pick(row, "成交数量")
    avg_key = _pick(row, "成交均价", "成交价格")
    cancelled_key = _pick(row, "撤消数量", "撤单数量")
    status_key = _pick(row, "备注", "委托状态", "状态说明")

    t = _normalize_time(row[time_key])

    return OrderRow(entrust_no=str(row[entrust_key]).strip(), code=_code(row), side=side,
                    price=_num(row[price_key]), shares=int(_num(row[shares_key])),
                    filled=int(_num(row[filled_key])) if filled_key else 0,
                    avg_price=_num(row[avg_key]) if avg_key else 0.0,
                    status=str(row[status_key]) if status_key else "",
                    time=t, cancelled=int(_num(row[cancelled_key])) if cancelled_key else 0)


def orders_from_rows(rows: list[dict]) -> list[OrderRow]:
    """当日委托原始行 → OrderRow，撤单记录行先排除（不是委托）。两个适配器的 orders_today 和家里实测脚本共用。"""
    return [order_from_row(r) for r in rows if not is_cancel_row(r)]


def cash_from_balance(d: dict) -> float:
    key = _pick(d, "可用金额", "可用资金", "可用余额")
    if key is None:
        raise _unknown_header(d)
    return _num(d[key])
