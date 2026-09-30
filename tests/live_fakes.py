"""测试专用假对象：FakeClock（时钟注入）、FakeNotifier（推送）、FakeBroker（脚本化假券商）。
Task 3-8 的下单/对账测试都靠这个模块，保持小而全，不加没人用的能力。
"""
from dataclasses import replace
from datetime import datetime, timedelta

from jevq.live.broker import BrokerError, Holding, OrderRow


class FakeClock:
    def __init__(self, start: datetime):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self._once_keys: set[str] = set()

    def send(self, title: str, content: str) -> None:
        self.sent.append((title, content))

    def once(self, key: str, title: str, content: str) -> None:
        if key in self._once_keys:
            return
        self._once_keys.add(key)
        self.sent.append((title, content))


class FakeBroker:
    """脚本化假券商，供 live.py 相关测试注入，实现 jevq.live.broker.Broker 协议。
    构造时传入 clock（提供 .now()），orders_today() 里委托的 time 字段取下单那一刻的 clock。

    可调整的属性：
      cash：可用资金。
      holdings：code -> [shares, available]，股票余额、可用余额。
      fill_mode：'all' 下单即全部成交 | 'none' 永不成交 |
                 'partial' 按 fill_ratio 成交（股数按 100 向下取整）|
                 'late' 第 late_polls 次调用 orders_today() 时才全部成交。
      fill_ratio：'partial' 模式的成交比例。
      late_polls：'late' 模式触发全部成交的 orders_today() 调用次数。
      fail_reads：接下来多少次读取（balance/positions/orders_today）改为抛 BrokerError，每次消耗一次。
      place_error：设为 BrokerError 实例时，下一次 place() 抛出它，抛出后自动清空（一次性）。
      extra_orders：预置的「用户手动下的单」，追加在 orders_today() 结果末尾；cancel_last() 不碰它们
                   （只撤本假券商自己受理的委托）。
      cancel_delay_polls：>0 时撤单是异步的——cancel_last() 先把委托改成「已报待撤」（仍算 open），
                   之后第 cancel_delay_polls 次 orders_today() 才变成「已撤」/「部撤」。
      calls：调用记录，如 ("place", side, code, price, shares)、("cancel_last",)、
             ("balance",)、("positions",)、("orders_today",)。
    """

    def __init__(self, clock, cash: float = 0.0, holdings: dict[str, list] | None = None):
        self.clock = clock
        self.cash = cash
        self.holdings: dict[str, list] = holdings if holdings is not None else {}
        self.fill_mode = "all"
        self.fill_ratio = 1.0
        self.late_polls = 1
        self.fail_reads = 0
        self.place_error: BrokerError | None = None
        self.extra_orders: list[OrderRow] = []
        self.cancel_delay_polls = 0
        self.calls: list[tuple] = []
        self._orders: list[OrderRow] = []
        self._cancelling: dict[int, int] = {}   # 委托下标 -> 还要几次 orders_today() 才撤掉
        self._next_no = 1
        self._polls = 0

    def balance(self) -> float:
        self.calls.append(("balance",))
        self._check_fail()
        return self.cash

    def positions(self) -> dict[str, Holding]:
        self.calls.append(("positions",))
        self._check_fail()
        return {code: Holding(code, shares, available) for code, (shares, available) in self.holdings.items()}

    def place(self, side: str, code: str, price: float, shares: int) -> str | None:
        self.calls.append(("place", side, code, price, shares))
        if self.place_error is not None:
            err, self.place_error = self.place_error, None
            raise err
        entrust_no = str(self._next_no)
        self._next_no += 1
        order = OrderRow(entrust_no, code, side, price, shares, 0, 0.0, "已报",
                         self.clock.now().strftime("%H:%M:%S"))
        self._orders.append(order)
        self._sync_fill(len(self._orders) - 1)
        return entrust_no

    def orders_today(self) -> list[OrderRow]:
        self.calls.append(("orders_today",))
        self._check_fail()
        self._polls += 1
        for i in range(len(self._orders)):
            self._sync_fill(i)
        for i in list(self._cancelling):
            self._cancelling[i] -= 1
            if self._cancelling[i] <= 0:
                del self._cancelling[i]
                self._finish_cancel(i)
        return self._orders + self.extra_orders

    def cancel_last(self) -> bool:
        self.calls.append(("cancel_last",))
        for i in range(len(self._orders) - 1, -1, -1):
            o = self._orders[i]
            if o.open and i not in self._cancelling:
                if self.cancel_delay_polls > 0:
                    self._orders[i] = replace(o, status="已报待撤")
                    self._cancelling[i] = self.cancel_delay_polls
                else:
                    self._finish_cancel(i)
                return True
        return False

    def _finish_cancel(self, i: int) -> None:
        o = self._orders[i]
        self._orders[i] = replace(o, status="部撤" if o.filled > 0 else "已撤")

    def _check_fail(self) -> None:
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise BrokerError("模拟读取失败", "FAKE_READ_FAIL")

    def _target_filled(self, order: OrderRow) -> int:
        if self.fill_mode == "all":
            return order.shares
        if self.fill_mode == "partial":
            return min(int(order.shares * self.fill_ratio) // 100 * 100, order.shares)
        if self.fill_mode == "late":
            return order.shares if self._polls >= self.late_polls else 0
        return 0    # 'none'

    def _sync_fill(self, i: int) -> None:
        order = self._orders[i]
        if not order.open:
            return
        target = self._target_filled(order)
        delta = target - order.filled
        if delta <= 0:
            return
        holding = self.holdings.setdefault(order.code, [0, 0])
        if order.side == "buy":
            self.cash -= delta * order.price
            holding[0] += delta                 # 可用余额 T+1 不加
        else:
            self.cash += delta * order.price
            holding[0] -= delta
            holding[1] -= delta
        status = "已成" if target == order.shares else "部成"
        self._orders[i] = replace(order, filled=target, avg_price=order.price, status=status)
