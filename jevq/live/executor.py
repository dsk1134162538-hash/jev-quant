"""实盘执行器：live.py 每秒调 Executor.step()。本任务（Task 3）实现骨架、心跳、模式/交易日门槛、
买入全流程（含等成交、核对撤单、sending 恢复）；卖出对齐（Task 4）、暂停/对账/自检（Task 5）接着扩展。
"""
import os
import time as time_mod
from datetime import datetime, timedelta

from jevq import config, ledger
from jevq.data import tencent
from jevq.live import store as live_store
from jevq.live.broker import BrokerError, INFLIGHT_CODES, REJECT_CODES
from jevq.live.store import Intent, LivePosition


class Executor:
    def __init__(self, conn, broker, notifier, clock=datetime.now, sleep=time_mod.sleep,
                quotes=tencent.fetch_quotes):
        self.conn = conn
        self.broker = broker
        self.notifier = notifier
        self.clock = clock
        self.sleep = sleep
        self.quotes = quotes
        self.read_fail_streak = int(live_store.get_state(conn, "read_fail_streak") or 0)

    # ---- 主循环 ----

    def startup(self) -> None:
        """程序重启后先核对 sending 委托（防重复下单，spec「防重复下单」）。
        对账（有持仓/空仓间隔对账）留给 Task 5 接在这后面。"""
        self._reconcile_sending_orders()

    def step(self) -> None:
        now = self.clock()
        live_store.set_state(self.conn, "heartbeat", now.isoformat())
        live_store.set_state(self.conn, "pid", str(os.getpid()))

        if live_store.mode(self.conn) == "off":
            self._skip_new_intents("模式关闭")
            return
        if live_store.get_state(self.conn, "trading_day") != now.date().isoformat():
            self._skip_new_intents("非交易日")
            return

        for intent in live_store.new_intents(self.conn):
            if intent.side == "buy":
                self._buy(intent)
            else:
                live_store.finish_intent(self.conn, intent.id, "done", "按持仓对齐处理", self.clock())

    def _skip_new_intents(self, reason: str) -> None:
        now = self.clock()
        for intent in live_store.new_intents(self.conn):
            live_store.finish_intent(self.conn, intent.id, "skipped", reason, now)

    # ---- 买入 ----

    def _buy(self, intent: Intent) -> None:
        conn, now = self.conn, self.clock()

        if now - intent.ts > timedelta(seconds=config.LIVE_INTENT_TTL):
            live_store.finish_intent(conn, intent.id, "expired", "超过 60 秒未处理", now)
            return
        if not self._in_buy_window(now):
            live_store.finish_intent(conn, intent.id, "skipped", "不在买入时段", now)
            return
        if live_store.paused(conn) is not None:
            live_store.finish_intent(conn, intent.id, "skipped", "暂停中", now)
            return
        if len(live_store.positions(conn)) >= config.LIVE_MAX_POSITIONS:
            live_store.finish_intent(conn, intent.id, "skipped", "程序已有持仓", now)
            return
        try:
            client_positions = self._read(self.broker.positions)
        except BrokerError:
            return   # 读失败：_read 内部按连续失败次数处理，这里保留 new，下一秒重试
        if intent.code in client_positions and intent.code not in live_store.positions(conn):
            live_store.finish_intent(conn, intent.id, "skipped", "你持有这只", now)
            return
        if live_store.buys_today(conn, now.date()) >= config.LIVE_MAX_BUYS_PER_DAY:
            live_store.finish_intent(conn, intent.id, "skipped", "今日买入已达上限", now)
            return
        if not intent.code.startswith(("60", "00")):
            live_store.finish_intent(conn, intent.id, "skipped", "非主板", now)
            return

        price = round(intent.sim_price * (1 + config.LIVE_BUY_SLIP), 2)
        try:
            q = self.quotes([intent.code]).get(intent.code)
            if q is not None:
                price = min(price, q.limit_up)
        except Exception:
            pass   # 取不到行情就只用前半（不封涨停价）

        try:
            balance = self._read(self.broker.balance)
        except BrokerError:
            return
        budget = min(balance, config.LIVE_BUDGET) * (1 - config.LIVE_CASH_BUFFER)
        shares = ledger.affordable_shares(budget, price)
        if shares == 0:
            live_store.finish_intent(conn, intent.id, "skipped", "买不起一手", now)
            return

        if live_store.mode(conn) == "dryrun":
            self._buy_dryrun(intent, now, price, shares)
        else:
            self._buy_live(intent, now, price, shares)

    def _in_buy_window(self, now: datetime) -> bool:
        t = now.time()
        return any(a <= t < b for a, b in config.LIVE_BUY_WINDOWS)

    def _buy_dryrun(self, intent: Intent, now: datetime, price: float, shares: int) -> None:
        conn = self.conn
        live_store.add_order(conn, intent.id, now, "buy", intent.code, price, shares, "dryrun")
        cost = shares * price + ledger.buy_fee(shares * price)
        live_store.put_position(conn, LivePosition(intent.code, shares, cost, price, now.date(), intent.sim_price))
        live_store.finish_intent(conn, intent.id, "done", "演练", now)

    def _buy_live(self, intent: Intent, now: datetime, price: float, shares: int) -> None:
        conn = self.conn
        order_id = live_store.add_order(conn, intent.id, now, "buy", intent.code, price, shares, "sending")
        try:
            entrust_no = self.broker.place("buy", intent.code, price, shares)
        except BrokerError as e:
            if e.code in REJECT_CODES:
                live_store.update_order(conn, order_id, self.clock(), status="failed", note=str(e))
                live_store.finish_intent(conn, intent.id, "done", f"券商拒单：{e}", self.clock())
                self._notify("买入失败", f"{intent.name} 券商拒单：{e}")
                return
            if e.code in INFLIGHT_CODES:
                self._auto_pause(f"下单结果不明：{e}，可能稍后才报出，恢复前请在 App 里核对")
                live_store.finish_intent(conn, intent.id, "done", "结果不明", self.clock())
                return
            self._reconcile_unclear_order(order_id, intent, now, price, shares)
            return
        live_store.update_order(conn, order_id, self.clock(), status="submitted", entrust_no=entrust_no or "")
        matched = self._wait_fill(intent.code, "buy", price, shares, entrust_no, now, config.LIVE_BUY_WAIT)
        self._finalize_buy_order(order_id, intent, matched, price, shares)

    def _reconcile_unclear_order(self, order_id: int, intent: Intent, order_ts: datetime,
                                 price: float, shares: int) -> None:
        """place() 抛出既不是拒单也不是网关超时的异常：单笔核对，同 startup 的 sending 核对。"""
        conn = self.conn
        try:
            orders = self._read(self.broker.orders_today)
        except BrokerError:
            self._auto_pause("下单结果不明")
            live_store.finish_intent(conn, intent.id, "done", "结果不明", self.clock())
            return
        matched = self._match_order(orders, intent.code, "buy", price, shares, None, order_ts)
        if matched is None:
            live_store.update_order(conn, order_id, self.clock(), status="failed",
                                    note="启动核对：没找到这笔委托")
            live_store.finish_intent(conn, intent.id, "done", "券商拒单：核对没找到这笔委托", self.clock())
            return
        live_store.update_order(conn, order_id, self.clock(), status="submitted", entrust_no=matched.entrust_no)
        result = self._wait_fill(intent.code, "buy", price, shares, matched.entrust_no, order_ts,
                                 config.LIVE_BUY_WAIT)
        self._finalize_buy_order(order_id, intent, result, price, shares)

    def _finalize_buy_order(self, order_id: int, intent: Intent, matched, price: float, shares: int) -> None:
        conn, now = self.conn, self.clock()
        filled = matched.filled if matched else 0
        if filled > 0:
            avg_price = matched.avg_price if matched.avg_price else price
            cost = filled * avg_price + ledger.buy_fee(filled * avg_price)
            live_store.put_position(conn, LivePosition(intent.code, filled, cost, avg_price, now.date(),
                                                        intent.sim_price))
            status = "filled" if filled >= shares else "partial"
            note = "已成交" if status == "filled" else "部分成交，已撤单"
            live_store.update_order(conn, order_id, now, status=status, filled=filled, avg_price=avg_price)
            live_store.finish_intent(conn, intent.id, "done", note, now)
            diff_pct = round((avg_price / intent.sim_price - 1) * 100, 2) if intent.sim_price else 0.0
            self._notify("买入", f"{intent.name} {filled}股 @{avg_price}（模拟 @{intent.sim_price}，"
                                f"差 {diff_pct}%）")
        else:
            live_store.update_order(conn, order_id, now, status="cancelled", filled=0)
            live_store.finish_intent(conn, intent.id, "done", "未成交，已撤单", now)
            self._notify("买入", "没成交，已撤单")

    def _wait_fill(self, code: str, side: str, price: float, shares: int, entrust_no, since_ts: datetime,
                   wait: float):
        """每 LIVE_POLL 秒读一次当日委托，直到全部成交或等够 wait 秒；没成交完就核对撤单，
        再读一次拿最终成交数。返回匹配到的 OrderRow（可能是 None，从没匹配上）。"""
        polls = max(1, int(wait // config.LIVE_POLL))
        matched = None
        for _ in range(polls):
            self.sleep(config.LIVE_POLL)
            try:
                orders = self._read(self.broker.orders_today)
            except BrokerError:
                continue
            matched = self._match_order(orders, code, side, price, shares, entrust_no, since_ts)
            if matched is not None and matched.filled >= matched.shares:
                return matched
        row = {"entrust_no": entrust_no, "code": code, "side": side, "price": price, "shares": shares}
        self._cancel_last_checked(row)
        try:
            orders = self._read(self.broker.orders_today)
        except BrokerError:
            return matched
        return self._match_order(orders, code, side, price, shares, entrust_no, since_ts) or matched

    def _match_order(self, orders, code: str, side: str, price: float, shares: int, entrust_no, since_ts):
        if entrust_no:
            return next((o for o in orders if o.entrust_no == entrust_no), None)
        since = since_ts.strftime("%H:%M:%S")
        candidates = [o for o in orders if o.code == code and o.side == side and o.price == price
                     and o.shares == shares and o.time >= since]
        return candidates[-1] if candidates else None

    def _cancel_last_checked(self, row: dict) -> bool:
        """撤单前核对：最后一笔 open 的委托必须是程序自己这笔（委托号，或代码/方向/价格/数量都一致），
        对不上就不撤，自动暂停。"""
        try:
            orders = self._read(self.broker.orders_today)
        except BrokerError:
            return False
        open_orders = [o for o in orders if o.open]
        last = open_orders[-1] if open_orders else None
        matches = last is not None and (
            (row.get("entrust_no") and last.entrust_no == row["entrust_no"])
            or (last.code == row["code"] and last.side == row["side"]
                and last.price == row["price"] and last.shares == row["shares"]))
        if not matches:
            self._auto_pause("撤单前核对不上：最后一笔不是程序的委托")
            return False
        return self.broker.cancel_last()

    # ---- sending 恢复（启动核对）----

    def _reconcile_sending_orders(self) -> None:
        rows = live_store.sending_orders(self.conn)
        if not rows:
            return
        try:
            orders = self._read(self.broker.orders_today)
        except BrokerError:
            self._auto_pause("启动核对读不到当日委托")
            return
        for row in rows:
            self._reconcile_sending_row(row, orders)

    def _reconcile_sending_row(self, row: dict, orders: list) -> None:
        conn = self.conn
        since = datetime.fromisoformat(row["ts"])
        matched = self._match_order(orders, row["code"], row["side"], row["price"], row["shares"], None, since)
        if matched is None:
            live_store.update_order(conn, row["id"], self.clock(), status="failed",
                                    note="启动核对：没找到这笔委托")
            return
        live_store.update_order(conn, row["id"], self.clock(), status="submitted", entrust_no=matched.entrust_no)
        if matched.filled < matched.shares:
            check_row = {"entrust_no": matched.entrust_no, "code": row["code"], "side": row["side"],
                        "price": row["price"], "shares": row["shares"]}
            self._cancel_last_checked(check_row)
            try:
                orders2 = self._read(self.broker.orders_today)
                matched = self._match_order(orders2, row["code"], row["side"], row["price"], row["shares"],
                                           matched.entrust_no, since) or matched
            except BrokerError:
                pass
        filled = matched.filled
        avg_price = matched.avg_price if matched.avg_price else row["price"]
        status = "filled" if filled >= row["shares"] else ("partial" if filled > 0 else "cancelled")
        live_store.update_order(conn, row["id"], self.clock(), status=status, filled=filled, avg_price=avg_price)
        if row["side"] == "buy" and filled > 0:
            cost = filled * avg_price + ledger.buy_fee(filled * avg_price)
            sim_price = self._intent_sim_price(row["intent_id"])
            live_store.put_position(conn, LivePosition(row["code"], filled, cost, avg_price,
                                                        self.clock().date(), sim_price))

    def _intent_sim_price(self, intent_id) -> float:
        if intent_id is None:
            return 0.0
        row = self.conn.execute("SELECT sim_price FROM live_intents WHERE id=?", (intent_id,)).fetchone()
        return row[0] if row else 0.0

    # ---- 读取 / 通知 / 自动暂停（公共基础设施）----

    def _read(self, fn):
        """统一读取：成功重置 read_fail_streak、写 last_read_ok；失败 +1，到 LIVE_READ_FAIL_PAUSE
        自动暂停，然后抛出（调用方自己决定这次失败要不要紧急处理）。"""
        try:
            result = fn()
        except BrokerError:
            self.read_fail_streak += 1
            live_store.set_state(self.conn, "read_fail_streak", str(self.read_fail_streak))
            if self.read_fail_streak >= config.LIVE_READ_FAIL_PAUSE:
                self._auto_pause("连续读取失败，客户端可能掉线")
            raise
        self.read_fail_streak = 0
        live_store.set_state(self.conn, "read_fail_streak", "0")
        live_store.set_state(self.conn, "last_read_ok", self.clock().isoformat())
        return result

    def _notify(self, title: str, content: str, key: str | None = None) -> None:
        if key is not None:
            state_key = f"notified:{key}:{self.clock().date().isoformat()}"
            if live_store.get_state(self.conn, state_key) is not None:
                return
            live_store.set_state(self.conn, state_key, "1")
        self.notifier.send(f"💰 实盘 {title}", content)

    def _auto_pause(self, reason: str) -> None:
        live_store.pause(self.conn, None, reason, "auto")
        self._notify("自动暂停", f"{reason}，需要手动恢复")
