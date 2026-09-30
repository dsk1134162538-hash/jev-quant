"""家里实测脚本，用户本人手动运行，不写单元测试。
用法：
  .venv/Scripts/python scripts/live_probe.py read --broker gateway|easytrader [--n 20] [--url ...] [--exe ...]
  .venv/Scripts/python scripts/live_probe.py order-test --broker ... --code 601398 --i-understand
read 只读，循环调用资金/持仓/当日委托统计成功率与耗时；最后再打印一次当日委托的原始行（未经程序解析，含撤单记录行），
  用来看委托时间的原文格式、合同编号，以及撤单会不会单独成一行（撤买/撤卖）。
order-test 会真实下两笔跌停价附近的买单再撤单，验证 cancel_last「只撤最后一笔」：
  A = 跌停价 100 股，B = 跌停价 +0.01 100 股（都远低于市价，不会成交）→ cancel_last() 必须只撤 B、A 仍挂着 → 再 cancel_last() 撤掉 A。
  A 也被撤了，说明「撤最后一笔」被网关按「全部撤单」执行了，绝不能上实盘。
  仅限连续竞价时段、需要 --i-understand 和键盘输入 yes 双重确认。
"""
import argparse
import os
import sys
import time
import traceback
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jevq.config import LIVE_EASYTRADER_EXE, LIVE_GATEWAY_URL, LIVE_MATCH_TOLERANCE  # noqa: E402
from jevq.data.tencent import fetch_quotes  # noqa: E402
from jevq.live.broker import BrokerError, is_cancel_row, orders_from_rows  # noqa: E402
from jevq.live.easytrader_broker import EasytraderBroker  # noqa: E402
from jevq.live.gateway import GatewayBroker  # noqa: E402
from jevq.notify import _windows_env_fallback  # noqa: E402

TRADING_WINDOWS = ((dtime(9, 30), dtime(11, 30)), (dtime(13, 0), dtime(14, 57)))
ORDER_TEST_MIN_LEFT = 120     # 秒：order-test 全程约 1 分钟（网关下单/读委托每次约 6 秒），离连续竞价结束不足这么久不跑
SETTLE_SECONDS = 3            # 下单/撤单之后等多久再读当日委托
CLOCK_SKEW_WARN = 5           # 秒：时钟偏差超过这个数就提醒校时
# 执行器没有委托号时按「委托时间 ≥ 下单时刻 − LIVE_MATCH_TOLERANCE 秒」匹配：电脑时钟比券商快过这个数就会匹配失败
ORDER_SHARES = 100


def _seconds_left_in_window(now: datetime) -> float:
    """now 在连续竞价时段内 → 离这一段结束还有几秒；不在 → 0。"""
    for lo, hi in TRADING_WINDOWS:
        if lo <= now.time() <= hi:
            return (datetime.combine(now.date(), hi) - now).total_seconds()
    return 0.0


def _require_trading_window(when: str) -> None:
    """不在连续竞价时段、或离结束不足 ORDER_TEST_MIN_LEFT 秒就退出（撤单必须在连续竞价内发出）。
    when：提示里说明是哪一次检查。yes 之前和 yes 之后各查一次——用户可能在提示前停了很久。"""
    now = datetime.now()
    if now.weekday() >= 5:
        print(f"{when}：今天是周末，不是交易日，拒绝运行")
        sys.exit(1)
    if _seconds_left_in_window(now) < ORDER_TEST_MIN_LEFT:
        print(f"{when}：当前 {now.strftime('%H:%M:%S')} 不在连续竞价时段（09:30–11:30、13:00–14:57），"
              f"或离结束不足 {ORDER_TEST_MIN_LEFT} 秒（全程约 1 分钟，撤单必须在连续竞价内发出），拒绝运行")
        sys.exit(1)


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

    print()
    try:
        raw = broker.raw_orders_today()
    except BrokerError as e:
        print(f"读当日委托原始行失败：{e.code} {e}")
        return
    cancel_rows = [r for r in raw if is_cancel_row(r)]
    print(f"当日委托原始行（未经程序解析）共 {len(raw)} 条，其中撤单记录行 {len(cancel_rows)} 条（程序会忽略它们）。")
    print("看：委托时间的原文格式（补不补零、是不是整数）、合同编号，撤过单的话有没有「撤买/撤卖」这样的行：")
    for row in raw:
        print(f"  {_safe_row(row)}")


# ---- order-test ----

def _safe_row(row: dict) -> dict:
    """打印原始行前遮掉账号类的列（股东代码、资金账号等）：输出要发给别人看。"""
    return {k: ("***" if any(w in str(k) for w in ("账号", "账户", "股东")) else v) for k, v in row.items()}


def _raw_row(raw_rows: list[dict], entrust_no: str) -> dict | None:
    for r in raw_rows:
        for key in ("合同编号", "委托编号"):
            if key in r and str(r[key]).strip() == entrust_no:
                return r
    return None


def _identify_order(orders, entrust_no, code, price, before_nos):
    """在当日委托里确认「刚下的这笔」：place 截到了委托号就先按号匹配；没截到、或号和当日委托里的合同编号对不上时，
    按（代码、买入、100 股、价格）找新出现的委托——A、B 价格差 0.01，能互相区分（号对不上本身就是要发现的问题，
    _describe 会指出来）。不能唯一确定就返回 None（拒绝撤单，而不是猜）。"""
    if entrust_no:
        hit = next((o for o in orders if o.entrust_no == entrust_no), None)
        if hit is not None:
            return hit
    candidates = [o for o in orders if o.code == code and o.side == "buy" and o.shares == ORDER_SHARES
                  and abs(o.price - price) < 0.005 and o.entrust_no not in before_nos]
    return candidates[0] if len(candidates) == 1 else None


def _by_no(orders, entrust_no):
    return next((o for o in orders if o.entrust_no == entrust_no), None)


def _read_orders(broker):
    """(解析后的当日委托, 原始行)：一次读取，解析用和适配器 orders_today 同一个 orders_from_rows（撤单记录行已排除）。"""
    raw = broker.raw_orders_today()
    return orders_from_rows(raw), raw


def _fail(msg: str, leftover: list[str] | None = None, note: str | None = None) -> None:
    """打印 FAIL 并退出。leftover：此刻可能还挂在券商那边、没撤掉的单子，提醒用户手动处理；note：补充说明。"""
    print(f"FAIL：{msg}")
    if note:
        print(note)
    if leftover:
        print(f"注意：{'、'.join(leftover)} 可能还挂着，去 App 里核对并手动撤单")
    sys.exit(1)


def _describe(label, place_no, order, raw_rows, t0: datetime, t1: datetime) -> None:
    """打印一笔委托：place 返回的委托号、当日委托里对应行的合同编号和委托时间原文（核对 capture_entrust_no、时间格式），
    以及下单调用前后的电脑时间（核对电脑时钟和券商时钟的偏差）。"""
    raw = _raw_row(raw_rows, order.entrust_no)
    raw_time = repr(raw.get("委托时间")) if raw else "（原始行没找到）"
    print(f"  {label}：place 返回委托号={place_no or '(空)'}；当日委托 合同编号={order.entrust_no} "
          f"委托时间原文={raw_time}（解析成 {order.time}）状态={order.status or '(空)'}")
    if place_no and place_no != order.entrust_no:
        print(f"  注意：{label} 的 place 返回委托号和当日委托的合同编号不一致，capture_entrust_no 抓到的号不能拿来匹配")
    print(f"  {label}：下单调用前电脑时间 {t0:%H:%M:%S}，返回后 {t1:%H:%M:%S}")
    try:
        h, m, s = (int(x) for x in order.time.split(":"))
        placed = t0.replace(hour=h, minute=m, second=s, microsecond=0)
    except ValueError:
        print(f"  注意：认不出这个委托时间格式：{order.time!r}，时间比较和「最后一笔」排序会出问题")
        return
    ahead = (t0 - placed).total_seconds()      # 电脑比券商快多少（委托时间早于下单调用前）
    behind = (placed - t1).total_seconds()     # 电脑比券商慢多少（委托时间晚于下单调用返回后）
    if ahead > LIVE_MATCH_TOLERANCE:
        print(f"  警告：电脑时钟比券商快约 {ahead:.0f} 秒（超过 {LIVE_MATCH_TOLERANCE} 秒跟单就会匹配失败），"
              f"上线前必须先 w32tm /resync")
    elif ahead > CLOCK_SKEW_WARN:
        print(f"  提示：电脑时钟比券商快约 {ahead:.0f} 秒，上线前先 w32tm /resync 校一下")
    elif behind > CLOCK_SKEW_WARN:
        print(f"  警告：电脑时钟比券商慢约 {behind:.0f} 秒，上线前先 w32tm /resync")


def cmd_order_test(args) -> None:
    if not args.i_understand:
        print("拒绝运行：必须带 --i-understand")
        sys.exit(1)

    _require_trading_window("检查时段")

    quotes = fetch_quotes([args.code])
    quote = quotes.get(args.code)
    if quote is None:
        print(f"取不到 {args.code} 的行情，拒绝运行")
        sys.exit(1)
    price_a = quote.limit_down
    price_b = round(price_a + 0.01, 2)
    if quote.price < price_b * 1.02:
        print(f"{args.code} 现价 {quote.price} 离跌停价 {price_a} 太近（或已跌停），跌停价买单可能成交，拒绝运行")
        sys.exit(1)

    broker = _make_broker(args)
    try:
        before = broker.orders_today()
    except BrokerError as e:
        print(f"FAIL：下单前读委托失败 {e.code} {e}")
        sys.exit(1)
    before_nos = {o.entrust_no for o in before}
    before_open = sum(1 for o in before if o.open)
    if before_open:
        # 万一「撤最后一笔」其实按全部撤单执行，会把这些挂单一起撤掉：先清干净再测
        print(f"当前已有 {before_open} 笔挂单：先在 App 里撤掉或等它们了结，再跑 order-test，拒绝运行")
        sys.exit(1)

    print(f"将真实下两笔买单，各 {ORDER_SHARES} 股 {args.code}（{quote.name}，现价 {quote.price}）：")
    print(f"  A：跌停价 {price_a}")
    print(f"  B：跌停价 +0.01 = {price_b}")
    print("然后调用一次 cancel_last()：应该只撤 B（最后一笔），A 仍挂着；再调用一次撤掉 A。两笔都远低于市价，不会成交。")
    print("运行前确认：live.py 是「关闭」模式或已停掉，也没有别的程序、手动在下单——「撤最后一笔」会撤到别人的单。")
    answer = input("输入 yes 确认：")
    if answer.strip() != "yes":
        print("未确认，退出")
        sys.exit(1)

    # 用户可能在上面的提示前停了很久：确认之后、下单之前再查一次时段
    _require_trading_window("确认之后再次检查时段（一笔都没下）")

    placed_at: dict[str, tuple[datetime, datetime]] = {}
    place_no: dict[str, str | None] = {}
    sent: list[str] = []
    for label, price in (("A", price_a), ("B", price_b)):
        sent.append(f"{label}（{args.code} 价 {price}）")   # 先记下：下单抛异常时它也可能已经报出去了
        t0 = datetime.now()
        try:
            place_no[label] = broker.place("buy", args.code, price, ORDER_SHARES)
        except Exception as e:
            print(f"FAIL：{label} 下单抛异常 {type(e).__name__}：{e}")
            print(f"单子可能已经报出去了，去 App 里核对并手动撤单（本次可能挂着的：{'、'.join(sent)}）")
            sys.exit(1)
        placed_at[label] = (t0, datetime.now())
        print(f"{label} 下单完成，place 返回委托号={place_no[label] or '(未截到)'}")
        time.sleep(SETTLE_SECONDS)

    try:
        orders, raw = _read_orders(broker)
    except BrokerError as e:
        _fail(f"下单后读委托失败 {e.code} {e}", sent)

    a = _identify_order(orders, place_no["A"], args.code, price_a, before_nos)
    b = _identify_order(orders, place_no["B"], args.code, price_b, before_nos)
    if a is None or b is None:
        _fail("没能在当日委托里唯一确认 A、B 这两笔新委托，拒绝撤单", sent)
    if a.entrust_no == b.entrust_no:
        _fail(f"A、B 匹配到了同一笔委托 {a.entrust_no}，拒绝撤单", sent)
    print("确认两笔新委托：")
    _describe("A", place_no["A"], a, raw, *placed_at["A"])
    _describe("B", place_no["B"], b, raw, *placed_at["B"])
    if not (a.open and b.open):
        _fail(f"A、B 不都是挂单状态（A 状态={a.status!r} open={a.open}，B 状态={b.status!r} open={b.open}），拒绝撤单",
              [f"{label}（{o.entrust_no}）" for label, o in (("A", a), ("B", b)) if o.open])
    print("PASS：A、B 都是挂单状态（未成交）")

    # ---- 第一次 cancel_last：必须只撤最后一笔 B ----
    still = [f"A（{a.entrust_no}）", f"B（{b.entrust_no}）"]
    try:
        cancelled = broker.cancel_last()
    except Exception as e:
        _fail(f"cancel_last 抛异常 {type(e).__name__}：{e}", still)
    if not cancelled:
        _fail("cancel_last 返回 False（没撤）", still)
    time.sleep(SETTLE_SECONDS)

    try:
        orders, raw = _read_orders(broker)
    except BrokerError as e:
        _fail(f"撤单后读委托失败 {e.code} {e}", still)
    a2, b2 = _by_no(orders, a.entrust_no), _by_no(orders, b.entrust_no)
    if a2 is None or b2 is None:
        missing = "、".join(label for label, o in (("A", a2), ("B", b2)) if o is None)
        _fail(f"撤单后在当日委托里找不到 {missing}（不能当成撤掉了）", still)
    cancel_rows = [r for r in raw if is_cancel_row(r)]
    if cancel_rows:
        print(f"撤单后当日委托里多出 {len(cancel_rows)} 条撤单记录行（程序会忽略）：")
        for r in cancel_rows:
            print(f"  {_safe_row(r)}")
    else:
        print("撤单后当日委托里没有单独的撤单记录行")
    if a2.open and b2.open:
        _fail("cancel_last 返回成功，但 A、B 两笔都还是 open（如果状态是「已报待撤」，稍后用 read 再看一眼）", still)
    if not a2.open and not b2.open:
        # 全部撤单会连你原有的挂单一起撤掉：对照下单前的挂单，看有没有被牵连
        hit = [o.entrust_no for o in before
               if o.open and _by_no(orders, o.entrust_no) is not None and not _by_no(orders, o.entrust_no).open]
        _fail("cancel_last 撤了不止一笔（可能按全部撤单执行），绝不能上实盘",
              note=f"你原有的挂单里，合同编号 {'、'.join(hit)} 也被撤了，去 App 里核对。" if hit else None)
    if not a2.open:
        _fail(f"cancel_last 撤掉的是 A 而不是最后一笔 B（B 状态={b2.status!r} 仍是 open），撤最后一笔不可靠",
              [f"B（{b.entrust_no}）"])
    print("PASS：撤最后一笔只撤了 B（B 已不是 open，A 仍是 open）")

    # ---- 第二次 cancel_last：撤掉 A ----
    still = [f"A（{a.entrust_no}）"]
    try:
        cancelled = broker.cancel_last()
    except Exception as e:
        _fail(f"第二次 cancel_last 抛异常 {type(e).__name__}：{e}", still)
    if not cancelled:
        _fail("第二次 cancel_last 返回 False（A 没撤掉）", still)
    time.sleep(SETTLE_SECONDS)

    try:
        orders, _ = _read_orders(broker)
    except BrokerError as e:
        _fail(f"撤 A 后读委托失败 {e.code} {e}", still)
    a3 = _by_no(orders, a.entrust_no)
    if a3 is None:
        _fail("撤 A 之后在当日委托里找不到 A（不能当成撤掉了）", still)
    if a3.open:
        _fail(f"A 撤单后仍是 open（状态={a3.status!r}）", still)
    print("PASS：A 也撤掉了，两笔都已撤。cancel_last 只撤最后一笔，这一项通过")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="实盘 broker 适配器手动探测脚本（家里运行，不联网测试用）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_read = sub.add_parser("read", help="循环读取资金/持仓/当日委托，统计成功率与耗时，最后打印当日委托原始行")
    p_read.add_argument("--broker", choices=("gateway", "easytrader"), required=True)
    p_read.add_argument("--n", type=int, default=20)
    p_read.add_argument("--url", default=LIVE_GATEWAY_URL)
    p_read.add_argument("--exe", default=LIVE_EASYTRADER_EXE)
    p_read.set_defaults(func=cmd_read)

    p_order = sub.add_parser("order-test", help="跌停价买两笔 100 股，验证撤最后一笔只撤 B，再撤 A——真实下单，仅限交易时段手动运行")
    p_order.add_argument("--broker", choices=("gateway", "easytrader"), required=True)
    p_order.add_argument("--code", required=True)
    p_order.add_argument("--url", default=LIVE_GATEWAY_URL)
    p_order.add_argument("--exe", default=LIVE_EASYTRADER_EXE)
    p_order.add_argument("--i-understand", action="store_true", dest="i_understand")
    p_order.set_defaults(func=cmd_order_test)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。如果已经下了单，去 App 里核对并手动撤单")
        sys.exit(1)
    except Exception:
        traceback.print_exc()
        print("出错了。如果已经下了单，去 App 里核对并手动撤单；把上面的输出发给 Claude")
        sys.exit(1)


if __name__ == "__main__":
    main()
