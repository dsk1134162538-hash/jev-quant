"""家里实测脚本，用户本人手动运行，不写单元测试。
用法：
  .venv/Scripts/python scripts/live_probe.py read --broker gateway|easytrader [--n 20] [--url ...] [--exe ...]
  .venv/Scripts/python scripts/live_probe.py order-test --broker ... --code 601398 --i-understand
read 只读，循环调用资金/持仓/当日委托统计成功率与耗时。
order-test 会真实下一笔跌停价买单再撤单，仅限连续竞价时段、需要 --i-understand 和键盘输入 yes 双重确认。
"""
import argparse
import os
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jevq.config import LIVE_EASYTRADER_EXE, LIVE_GATEWAY_URL  # noqa: E402
from jevq.data.tencent import fetch_quotes  # noqa: E402
from jevq.live.broker import BrokerError  # noqa: E402
from jevq.live.easytrader_broker import EasytraderBroker  # noqa: E402
from jevq.live.gateway import GatewayBroker  # noqa: E402
from jevq.notify import _windows_env_fallback  # noqa: E402

TRADING_WINDOWS = ((dtime(9, 30), dtime(11, 30)), (dtime(13, 0), dtime(14, 57)))


def _in_trading_window(now: dtime) -> bool:
    return any(lo <= now <= hi for lo, hi in TRADING_WINDOWS)


def _gateway_token() -> str | None:
    # 绝不打印：只取来构造 broker，任何 print/log 都不能带上它。
    return os.environ.get("XIADAN_GATEWAY_TOKEN") or _windows_env_fallback("XIADAN_GATEWAY_TOKEN")


def _make_broker(args):
    if args.broker == "gateway":
        return GatewayBroker(args.url, _gateway_token())
    return EasytraderBroker(args.exe)


def cmd_read(args) -> None:
    broker = _make_broker(args)

    if args.broker == "gateway":
        try:
            print(f"/health: {broker.health()}")
        except BrokerError as e:
            print(f"/health 失败：{e.code} {e}")

    stats = {name: {"ok": 0, "errors": [], "durations": []}
             for name in ("balance", "positions", "orders_today")}
    last = {"balance": None, "positions": None, "orders_today": None}

    for _ in range(args.n):
        for name in ("balance", "positions", "orders_today"):
            fn = getattr(broker, name)
            t0 = time.monotonic()
            try:
                last[name] = fn()
                stats[name]["ok"] += 1
            except BrokerError as e:
                stats[name]["errors"].append(e.code)
            stats[name]["durations"].append(time.monotonic() - t0)

    print()
    print(f"{'接口':<14}{'成功':>6}{'失败':>6}{'平均耗时':>10}{'最大耗时':>10}")
    for name, s in stats.items():
        avg = sum(s["durations"]) / len(s["durations"]) if s["durations"] else 0.0
        mx = max(s["durations"]) if s["durations"] else 0.0
        print(f"{name:<14}{s['ok']:>6}{len(s['errors']):>6}{avg:>9.2f}s{mx:>9.2f}s")
        if s["errors"]:
            print(f"  错误码：{s['errors']}")

    print()
    if last["balance"] is not None:
        print(f"可用资金：{last['balance']}")
    if last["positions"]:
        print("持仓：代码 股数 可卖")
        for h in last["positions"].values():
            print(f"  {h.code} {h.shares} {h.available}")
    if last["orders_today"]:
        print("委托：时间 代码 方向 价格 数量 成交 撤消 备注")
        for o in last["orders_today"]:
            print(f"  {o.time} {o.code} {o.side} {o.price} {o.shares} {o.filled} {o.cancelled} {o.status}")


def _identify_new_order(orders, entrust_no, code, before_open_nos):
    """在当日委托里确认「刚下的这笔」：截到号就按号精确匹配且必须 open；
    没截到号时，退而找新出现的、属于该代码的 open 委托——不能唯一确定就返回 None（拒绝撤单，而不是猜）。
    """
    if entrust_no:
        for o in orders:
            if o.entrust_no == entrust_no:
                return o if o.open else None
        return None
    candidates = [o for o in orders if o.code == code and o.open and o.entrust_no not in before_open_nos]
    return candidates[0] if len(candidates) == 1 else None


def cmd_order_test(args) -> None:
    if not args.i_understand:
        print("拒绝运行：必须带 --i-understand")
        sys.exit(1)

    now = datetime.now().time()
    if not _in_trading_window(now):
        print(f"当前 {now.strftime('%H:%M:%S')} 不在连续竞价时段（09:30–11:30、13:00–14:57），拒绝运行")
        sys.exit(1)

    quotes = fetch_quotes([args.code])
    quote = quotes.get(args.code)
    if quote is None:
        print(f"取不到 {args.code} 的行情，拒绝运行")
        sys.exit(1)
    price = quote.limit_down

    broker = _make_broker(args)
    try:
        before = broker.orders_today()
    except BrokerError as e:
        print(f"FAIL：下单前读委托失败 {e.code} {e}")
        sys.exit(1)
    before_open_nos = {o.entrust_no for o in before if o.open}

    print(f"将以跌停价 {price} 买 100 股 {args.code}，然后立刻撤单")
    answer = input("输入 yes 确认：")
    if answer.strip() != "yes":
        print("未确认，退出")
        sys.exit(1)

    try:
        entrust_no = broker.place("buy", args.code, price, 100)
        print(f"PASS：下单完成，entrust_no={entrust_no or '(未截到)'}")
    except BrokerError as e:
        print(f"FAIL：下单失败 {e.code} {e}")
        sys.exit(1)

    time.sleep(3)

    try:
        orders = broker.orders_today()
    except BrokerError as e:
        print(f"FAIL：下单后读委托失败 {e.code} {e}")
        sys.exit(1)

    placed = _identify_new_order(orders, entrust_no, args.code, before_open_nos)
    if placed is None:
        print("FAIL：没能在当日委托里唯一确认这笔新委托，拒绝撤单")
        sys.exit(1)
    print(f"PASS：确认新委托 {placed.entrust_no} {placed.time}，状态未成交")

    if not broker.cancel_last():
        print("FAIL：cancel_last 返回 False")
        sys.exit(1)

    time.sleep(3)

    try:
        orders = broker.orders_today()
    except BrokerError as e:
        print(f"FAIL：撤单后读委托失败 {e.code} {e}")
        sys.exit(1)
    after = next((o for o in orders if o.entrust_no == placed.entrust_no), None)
    if after is not None and after.open:
        print("FAIL：撤单后该笔委托仍是 open")
        sys.exit(1)
    print("PASS：撤单已确认")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="实盘 broker 适配器手动探测脚本（家里运行，不联网测试用）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_read = sub.add_parser("read", help="循环读取资金/持仓/当日委托，统计成功率与耗时")
    p_read.add_argument("--broker", choices=("gateway", "easytrader"), required=True)
    p_read.add_argument("--n", type=int, default=20)
    p_read.add_argument("--url", default=LIVE_GATEWAY_URL)
    p_read.add_argument("--exe", default=LIVE_EASYTRADER_EXE)
    p_read.set_defaults(func=cmd_read)

    p_order = sub.add_parser("order-test", help="跌停价买 100 股再撤单——真实下单，仅限交易时段手动运行")
    p_order.add_argument("--broker", choices=("gateway", "easytrader"), required=True)
    p_order.add_argument("--code", required=True)
    p_order.add_argument("--url", default=LIVE_GATEWAY_URL)
    p_order.add_argument("--exe", default=LIVE_EASYTRADER_EXE)
    p_order.add_argument("--i-understand", action="store_true", dest="i_understand")
    p_order.set_defaults(func=cmd_order_test)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
