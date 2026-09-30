"""easytrader（同花顺独立下单客户端）适配器。见 research-easytrader.md（源码审查结论）。
默认连接实现只在真正被调用时才 import easytrader；测试永远走注入的假 connect，不触发这行 import。"""
import re

from jevq.live.broker import BrokerError, Holding, OrderRow, cash_from_balance, holding_from_row, orders_from_rows

_CODE_RE = re.compile(r"^(60|00)\d{4}$")


class EasytraderBroker:
    def __init__(self, exe_path: str, connect=None):
        self.exe_path = exe_path
        self._connect = connect or self._default_connect
        self._user = None

    def _default_connect(self):
        import easytrader
        u = easytrader.use("universal_client")
        u.connect(self.exe_path)
        return u

    def _call(self, fn, code_for=lambda e: "UNKNOWN"):
        """确保已连接后执行 fn(user)；任何异常都丢掉连接（下次重连）并包成 BrokerError。"""
        try:
            if self._user is None:
                self._user = self._connect()
            return fn(self._user)
        except Exception as e:
            self._user = None
            raise BrokerError(str(e), code_for(e)) from e

    def balance(self) -> float:
        return cash_from_balance(self._call(lambda u: u.balance))

    def positions(self) -> dict[str, Holding]:
        rows = self._call(lambda u: u.position)
        if rows is None:
            raise BrokerError("读持仓失败", "READ_FAILED")
        holdings = [holding_from_row(r) for r in rows]
        return {h.code: h for h in holdings}

    def place(self, side: str, code: str, price: float, shares: int) -> str | None:
        # 同 GatewayBroker 的边界校验：调用方自己保证 6 位代码、正整数 100 倍数数量、明确价格。
        if side not in ("buy", "sell"):
            raise ValueError(f"非法方向：{side}")
        if not _CODE_RE.match(code):
            raise ValueError(f"非法代码：{code}")
        if shares <= 0 or shares % 100 != 0:
            raise ValueError(f"非法股数：{shares}")
        if price <= 0:
            raise ValueError(f"非法价格：{price}")

        def do(u):
            fn = u.buy if side == "buy" else u.sell
            return fn(code, price=round(price, 2), amount=shares)

        def code_for(e):
            # easytrader 只要提示框里没有「成功」二字就抛 TradeError（有的券商成功提示是「委托已提交」），
            # 单子可能已经报出去了：不能映射成 REJECT_CODES 里的码（会被当成明确拒单、不核对）；
            # TRADE_ERROR 让执行器按「认不出的错误」先核对当日委托。
            return "TRADE_ERROR" if type(e).__name__ == "TradeError" else "UNKNOWN"

        result = self._call(do, code_for)
        return result.get("entrust_no")

    def raw_orders_today(self) -> list[dict]:
        """当日委托的原始行：不解析、不过滤撤单记录行（同 GatewayBroker.raw_orders_today）。"""
        return self._call(lambda u: u.today_entrusts)

    def orders_today(self) -> list[OrderRow]:
        return orders_from_rows(self.raw_orders_today())      # 撤单记录行不是委托，orders_from_rows 会排除

    def cancel_last(self) -> bool:
        open_orders = [o for o in self.orders_today() if o.open]
        if not open_orders:
            return False
        target = max(open_orders, key=lambda o: o.time)
        result = self._call(lambda u: u.cancel_entrust(target.entrust_no))
        msg = result.get("message", "")
        return "成功" in msg or msg == "success"
