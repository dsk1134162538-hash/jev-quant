"""实盘执行器：live.py 每秒调 Executor.step()。已实现骨架、心跳、模式/交易日门槛、买入全流程（含等成交、
核对撤单、sending 恢复）、卖出对齐（待卖、重挂、跌停排队、14:57 后不撤、收盘结算）、
暂停/恢复、立即卖出收尾、对账（客户端股数少了才减账，两次读数确认）、自检、收盘小结。

真金白银的两条底线：
  1. 委托最终状态只在「成功读到 + broker 判定它不再 open」时才落成 filled/partial/cancelled 这类终态；
     读不到、或读到了但还 open，一律不许瞎猜，维持 submitted 并 _auto_pause，交给人工核对（见 _wait_fill /
     _cancel_and_confirm）。唯一的例外是收盘后结算（_settle_after_close）：收盘后成交数已定。
  2. 一个意图 / 一次卖出尝试最多只产生一次 place() 调用：add_order(sending) 后立刻 finish_intent（买），
     place() 之后不管抛什么异常都不放它跑出 _buy_live / _place_sell；_buy 顶部另外兜底一次「这个意图是不是
     已经有委托了」，_sell 在今天这只还有 sending 卖单时绝不再下。
另外：暂停中完全不碰券商（不下单、不撤单、不查询）；自动暂停只有手动恢复才会解除。
"""
import json
import os
import time as time_mod
from datetime import date, datetime, timedelta

from jevq import config, ledger
from jevq.data import tencent
from jevq.live import store as live_store
from jevq.live.broker import BrokerError, INFLIGHT_CODES, REJECT_CODES
from jevq.live.store import Intent, LivePosition


def _price_eq(a: float, b: float) -> bool:
    return abs(a - b) < 0.005


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
        self._next_sell_try: dict[str, datetime] = {}   # 内存状态，跨天清零
        self._sell_rounds: dict[str, int] = {}
        self._sell_day: date | None = None
        self._was_paused = live_store.paused(conn) is not None   # 重启时按当前状态起步，不重复通知
        self._last_reconcile: datetime | None = None             # 内存状态：启动后第一轮立刻对账一次

    # ---- 主循环 ----

    def startup(self) -> None:
        """程序重启后：暂停中什么都不碰（恢复时会核对）；否则先核对今天的 sending/submitted 委托（防重复下单，
        spec「防重复下单」，不论模式），再对账（模式不是 off）。更早日期遗留的行不动，它们的成交以对账为准。"""
        if self._paused():
            return
        self._reconcile_sending_orders()
        if self._paused() or live_store.mode(self.conn) == "off":
            return   # 核对读不到/对不上已经自动暂停：不再碰券商
        self._reconcile(self.clock())

    def step(self) -> None:
        now = self.clock()
        live_store.set_state(self.conn, "heartbeat", now.isoformat())
        live_store.set_state(self.conn, "pid", str(os.getpid()))

        if live_store.mode(self.conn) == "off":
            self._skip_new_intents("模式关闭")
            return
        if live_store.get_state(self.conn, "trading_day") != now.date().isoformat():
            self._skip_new_intents("非交易日", keep_remote=True)   # 立即卖出（T+1 卖不了的）留到下个交易日
            return

        self._handle_resume(now)
        self._note_pause_change()
        if self._paused():
            self._while_paused(now)
            return   # 暂停中零 broker 调用

        self._self_check(now)
        self._note_sell_intents(now)
        self._align_sells(now)
        # 立即卖出收尾放在买入意图之前：卖光的同一轮里，暂停先落地，同轮的买入意图才会被「暂停中」挡住
        self._finish_remote_sells(self.clock())
        for intent in live_store.new_intents(self.conn):
            if intent.side == "buy":
                self._buy(intent)
        if not self._paused():   # 前面某一步自动暂停了：本轮不再碰券商
            self._scheduled_reconcile(self.clock())
            self._settle_after_close(self.clock())
        self._close_summary(self.clock())

    def _paused(self) -> bool:
        return live_store.paused(self.conn) is not None

    def _skip_new_intents(self, reason: str, keep_remote: bool = False) -> None:
        now = self.clock()
        for intent in live_store.new_intents(self.conn):
            if keep_remote and intent.source == "remote":
                continue
            live_store.finish_intent(self.conn, intent.id, "skipped", reason, now)

    # ---- 买入 ----

    def _buy(self, intent: Intent) -> None:
        conn, now = self.conn, self.clock()

        if self._has_order(intent.id):
            # 兜底：正常流程里 add_order(sending) 后立刻 finish_intent，这里走到说明中途出过意外
            # （比如两次写库之间进程死过），无论如何都不能再下一次单。
            live_store.finish_intent(conn, intent.id, "done", "已有对应委托，防止重复下单", now)
            return
        if now - intent.ts > timedelta(seconds=config.LIVE_INTENT_TTL):
            live_store.finish_intent(conn, intent.id, "expired", "超过 60 秒未处理", now)
            return
        if not self._in_buy_window(now):
            live_store.finish_intent(conn, intent.id, "skipped", "不在买入时段", now)
            return
        if not self._can_cancel_by(now + timedelta(seconds=config.LIVE_BUY_WAIT)):
            live_store.finish_intent(conn, intent.id, "skipped", "临近休市，来不及等成交和撤单", now)
            return
        if live_store.paused(conn) is not None:
            live_store.finish_intent(conn, intent.id, "skipped", "暂停中", now)
            return
        if len(live_store.positions(conn)) >= config.LIVE_MAX_POSITIONS:
            live_store.finish_intent(conn, intent.id, "skipped", "程序已有持仓", now)
            return
        try:
            client_positions = self._read(self.broker.positions)
        except Exception:
            return   # 读失败：_read 内部按连续失败次数处理，这里保留 new，下一秒重试
        self._reconcile(now, client_positions)   # 下单前对账，复用刚读到的持仓
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
            if q is not None and q.limit_up > 0:
                price = min(price, q.limit_up)
        except Exception:
            pass   # 取不到行情就只用前半（不封涨停价）

        try:
            balance = self._read(self.broker.balance)
        except Exception:
            return
        budget = min(balance, config.LIVE_BUDGET) * (1 - config.LIVE_CASH_BUFFER)
        shares = ledger.affordable_shares(budget, price)
        if shares == 0:
            live_store.finish_intent(conn, intent.id, "skipped", "买不起一手", now)
            return
        if not self._can_cancel_by(self.clock() + timedelta(seconds=config.LIVE_BUY_WAIT)):
            # 上面几次读券商要十来秒：下单前按现在的时钟再核一次，等完还能在连续竞价里撤
            live_store.finish_intent(conn, intent.id, "skipped", "临近休市，来不及等成交和撤单", now)
            return
        if self._paused():   # 读券商那十来秒里手机点了暂停：这一笔也不下
            live_store.finish_intent(conn, intent.id, "skipped", "暂停中", now)
            return

        if live_store.mode(conn) == "dryrun":
            self._buy_dryrun(intent, now, price, shares)
        else:
            self._buy_live(intent, now, price, shares)

    def _has_order(self, intent_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM live_orders WHERE intent_id=? LIMIT 1",
                                 (intent_id,)).fetchone() is not None

    def _in_buy_window(self, now: datetime) -> bool:
        t = now.time()
        return any(a <= t < b for a, b in config.LIVE_BUY_WINDOWS)

    def _can_cancel_by(self, t: datetime) -> bool:
        """到 t 时刻还来得及撤单：t 再往后 LIVE_ORDER_MARGIN 秒仍在连续竞价里（午休、收盘集合竞价撤不了单）。"""
        start, end = t.time(), (t + timedelta(seconds=config.LIVE_ORDER_MARGIN)).time()
        return any(a <= start and end < b for a, b in config.LIVE_CANCEL_WINDOWS)

    def _buy_dryrun(self, intent: Intent, now: datetime, price: float, shares: int) -> None:
        """spec：演练的持仓按模拟成交价记入 live_positions（不是限价，限价只用来算股数）。"""
        conn = self.conn
        live_store.add_order(conn, intent.id, now, "buy", intent.code, price, shares, "dryrun")
        cost = shares * intent.sim_price + ledger.buy_fee(shares * intent.sim_price)
        live_store.put_position(conn, LivePosition(intent.code, shares, cost, intent.sim_price, now.date(),
                                                    intent.sim_price))
        live_store.finish_intent(conn, intent.id, "done", "演练", now)
        self._notify("买入（演练）", f"{intent.name} {shares}股 @{intent.sim_price}（限价 {price}，没下单）")

    def _buy_live(self, intent: Intent, now: datetime, price: float, shares: int) -> None:
        conn = self.conn
        order_id = live_store.add_order(conn, intent.id, now, "buy", intent.code, price, shares, "sending")
        # 意图在这里就算处理完：place() 之后不管发生什么，同一个意图都不会再触发第二次下单。
        live_store.finish_intent(conn, intent.id, "done", "下单中", now)
        try:
            entrust_no = self.broker.place("buy", intent.code, price, shares)
        except Exception as e:
            if isinstance(e, BrokerError) and e.code in REJECT_CODES:
                live_store.update_order(conn, order_id, self.clock(), status="failed", note=str(e))
                live_store.finish_intent(conn, intent.id, "done", f"券商拒单：{e}", self.clock())
                self._notify("买入失败", f"{intent.name} 券商拒单：{e}")
                return
            if isinstance(e, BrokerError) and e.code in INFLIGHT_CODES:
                self._auto_pause(f"下单结果不明：{e}，可能稍后才报出，恢复前请在 App 里核对")
                live_store.update_order(conn, order_id, self.clock(), note=f"结果不明：{e}")
                live_store.finish_intent(conn, intent.id, "done", "结果不明", self.clock())
                return
            self._reconcile_unclear_order(order_id, intent, now, price, shares)
            return
        live_store.update_order(conn, order_id, self.clock(), status="submitted", entrust_no=entrust_no or "")
        matched, confirmed = self._wait_fill(intent.code, "buy", price, shares, entrust_no, now,
                                             config.LIVE_BUY_WAIT)
        self._settle_wait_result(order_id, intent, matched, confirmed, price, shares)

    def _reconcile_unclear_order(self, order_id: int, intent: Intent, order_ts: datetime,
                                 price: float, shares: int) -> None:
        """place() 抛出既不是拒单也不是网关超时的异常（含非 BrokerError）：单笔核对。"""
        conn = self.conn
        try:
            orders = self._read(self.broker.orders_today)
        except Exception:
            self._auto_pause("下单结果不明：核对读不到当日委托")
            live_store.update_order(conn, order_id, self.clock(), note="下单结果不明：核对读不到当日委托")
            live_store.finish_intent(conn, intent.id, "done", "结果不明", self.clock())
            return
        matched = self._match_order(orders, intent.code, "buy", price, shares, None, order_ts)
        if matched is None:
            live_store.update_order(conn, order_id, self.clock(), status="failed",
                                    note="下单结果核对：没找到这笔委托")
            self._auto_pause(f"下单层返回认不出的错误：{intent.name} 核对没找到这笔委托")
            live_store.finish_intent(conn, intent.id, "done", "下单层返回认不出的错误：核对没找到这笔委托",
                                     self.clock())
            return
        live_store.update_order(conn, order_id, self.clock(), status="submitted", entrust_no=matched.entrust_no)
        if not matched.open:
            self._settle_wait_result(order_id, intent, matched, True, price, shares)
            return
        # 对上了且还挂着：当作已提交，照常等成交
        result, confirmed = self._wait_fill(intent.code, "buy", price, shares, matched.entrust_no or None,
                                            order_ts, config.LIVE_BUY_WAIT)
        self._settle_wait_result(order_id, intent, result, confirmed, price, shares)

    def _settle_wait_result(self, order_id: int, intent: Intent, matched, confirmed: bool,
                            price: float, shares: int) -> None:
        conn, now = self.conn, self.clock()
        if matched is not None and matched.entrust_no:
            live_store.update_order(conn, order_id, now, entrust_no=matched.entrust_no)   # 之后只按号跟踪
        if not confirmed:
            live_store.update_order(conn, order_id, now, note="委托状态查不到，需要人工核对")
            self._auto_pause(f"委托状态查不到：{intent.name} 这笔委托核对不到最终状态，请在 App 里确认")
            live_store.finish_intent(conn, intent.id, "done", "结果不明（委托状态查不到）", now)
            return
        filled = matched.filled if matched else 0
        if filled > 0:
            avg_price = matched.avg_price if matched.avg_price else price
            cost = filled * avg_price + ledger.buy_fee(filled * avg_price)
            live_store.put_position(conn, LivePosition(intent.code, filled, cost, avg_price, now.date(),
                                                        intent.sim_price))
            status = "filled" if filled >= shares else "partial"
            note = "已成交" if status == "filled" else "部分成交，已撤单"
            live_store.update_order(conn, order_id, now, status=status, filled=filled, avg_price=avg_price,
                                    note=note)
            live_store.finish_intent(conn, intent.id, "done", note, now)
            diff_pct = round((avg_price / intent.sim_price - 1) * 100, 2) if intent.sim_price else 0.0
            self._notify("买入", f"{intent.name} {filled}股 @{avg_price}（模拟 @{intent.sim_price}，"
                                f"差 {diff_pct}%）")
        else:
            live_store.update_order(conn, order_id, now, status="cancelled", filled=0, note="未成交，已撤单")
            live_store.finish_intent(conn, intent.id, "done", "未成交，已撤单", now)
            self._notify("买入", f"{intent.name} 没成交，已撤单")

    def _wait_fill(self, code: str, side: str, price: float, shares: int, entrust_no, since_ts: datetime,
                   wait: float):
        """等成交（买入用）：_poll_fill 等到 wait 秒，没等到终态就核对撤单再确认。
        返回 (matched, confirmed)：confirmed=True 才代表真的知道最终状态（成功读到 + 不再 open），
        confirmed=False 时 matched 只是最后一次读到的参考，不能拿来当真。"""
        matched, closed = self._poll_fill(code, side, price, shares, entrust_no, since_ts, wait)
        if closed:
            return matched, True
        entrust_no = entrust_no or (matched.entrust_no if matched is not None else None) or None
        return self._cancel_and_confirm(code, side, price, shares, entrust_no, since_ts, matched)

    def _poll_fill(self, code: str, side: str, price: float, shares: int, entrust_no, since_ts: datetime,
                   wait: float, must_cancel: bool = True):
        """只轮询不撤单：每 LIVE_POLL 秒读一次当日委托，直到确认不再 open 或等够 wait 秒（按时钟算：网关一次读
        要好几秒）。返回 (matched, closed)：closed=True 才是终态（成功读到 + 不再 open）；False 时 matched
        只是最后一次读到的参考（可能是 None）。
        must_cancel（买单、上午的卖单）：现在还能撤、但再等一轮（LIVE_POLL + 一次读 LIVE_READ_EST）就撤不了 →
        提前收手，让调用方趁连续竞价还在去撤。下午的卖单不用：等过 14:57 就留给收盘集合竞价。"""
        deadline = self.clock() + timedelta(seconds=wait)
        matched = None
        while True:
            t = self.clock()
            if must_cancel and self._can_cancel_by(t) and \
                    not self._can_cancel_by(t + timedelta(seconds=config.LIVE_POLL + config.LIVE_READ_EST)):
                return matched, False
            self.sleep(config.LIVE_POLL)
            try:
                orders = self._read(self.broker.orders_today)
            except Exception:
                orders = None
            if orders is not None:
                matched = self._match_order(orders, code, side, price, shares, entrust_no, since_ts)
                if matched is not None and matched.entrust_no and not entrust_no:
                    entrust_no = matched.entrust_no   # 第一次对上以后只按委托号跟踪
                if matched is not None and not matched.open:
                    return matched, True
            if self.clock() >= deadline:
                return matched, False

    def _cancel_and_confirm(self, code: str, side: str, price: float, shares: int, entrust_no,
                            since_ts: datetime, matched):
        """等够时间还没等到终态：核对撤单（_cancel_last_checked 会自己在核对不上/读不到时暂停），再确认。
        真实撤单是异步的（「已报待撤」仍算 open）：撤单发出去了就每 LIVE_POLL 秒读一次，最多 LIVE_CANCEL_WAIT 秒；
        没发出去只读一次。只有读成功且显示不再 open，才算数；否则 (matched, False)。
        已到午休/收盘集合竞价（撤不了）→ 不撤，自动暂停交给人工。"""
        if not self._can_cancel_by(self.clock()):
            self._auto_pause(f"来不及撤单：{code} 这笔委托还挂着，已到午休或收盘集合竞价，请在 App 里处理")
            return matched, False
        row = {"entrust_no": entrust_no, "code": code, "side": side, "price": price, "shares": shares}
        cancel_sent = self._cancel_last_checked(row, since_ts)
        deadline = self.clock() + timedelta(seconds=config.LIVE_CANCEL_WAIT)
        final = matched
        while True:
            try:
                orders = self._read(self.broker.orders_today)
            except Exception:
                orders = None
            if orders is not None:
                final = self._match_order(orders, code, side, price, shares, entrust_no, since_ts) or final
                if final is not None and not final.open:
                    return final, True
            if not cancel_sent or self.clock() >= deadline:
                return final, False
            self.sleep(config.LIVE_POLL)

    def _match_order(self, orders, code: str, side: str, price: float, shares: int, entrust_no, since_ts):
        """有委托号只认委托号；没有（网关默认拿不到号）按代码/方向/价格/数量 + 委托时间不早于下单时刻
        （容忍电脑时钟快 LIVE_MATCH_TOLERANCE 秒）匹配，排除已经记在别的行上的委托号，取委托时间最晚的一条。"""
        if entrust_no:
            return next((o for o in orders if o.entrust_no == entrust_no), None)
        since = (since_ts - timedelta(seconds=config.LIVE_MATCH_TOLERANCE)).strftime("%H:%M:%S")
        known = {r["entrust_no"] for r in live_store.orders_on(self.conn, since_ts.date()) if r["entrust_no"]}
        candidates = sorted((o for o in orders if o.code == code and o.side == side and _price_eq(o.price, price)
                             and o.shares == shares and o.time >= since and o.entrust_no not in known),
                            key=lambda o: o.time)
        return candidates[-1] if candidates else None

    def _cancel_last_checked(self, row: dict, since_ts: datetime) -> bool:
        """撤单前核对：最后一笔 open 的委托必须是程序自己这笔——有委托号只认委托号，没有才退回
        代码/方向/价格/数量四要素；对不上就不撤，自动暂停；读不到也不撤，自动暂停。
        程序这笔这时已经不 open 了（刚好成交完/被撤/废单）→ 不用撤，也不算异常。返回是否发出了撤单。"""
        try:
            orders = self._read(self.broker.orders_today)
        except Exception:
            self._auto_pause("撤单前核对不上：读不到当日委托")
            return False
        ours = self._match_order(orders, row["code"], row["side"], row["price"], row["shares"],
                                 row.get("entrust_no"), since_ts)
        if ours is not None and not ours.open:
            return False
        # 网关没保证行顺序：「最后一笔」按委托时间取（同一秒的保持原顺序，取靠后的）
        open_orders = sorted((o for o in orders if o.open), key=lambda o: o.time)
        last = open_orders[-1] if open_orders else None
        if row.get("entrust_no"):
            matches = last is not None and last.entrust_no == row["entrust_no"]
        else:
            matches = last is not None and (last.code == row["code"] and last.side == row["side"]
                                            and _price_eq(last.price, row["price"])
                                            and last.shares == row["shares"])
        if not matches:
            self._auto_pause("撤单前核对不上：最后一笔不是程序的委托")
            return False
        if not self._can_cancel_by(self.clock()):   # 上面的核对读取也要好几秒：发撤单那一刻再核一次时段
            self._auto_pause(f"来不及撤单：{row['code']} 这笔委托还挂着，已到午休或收盘集合竞价，请在 App 里处理")
            return False
        try:
            return self.broker.cancel_last()
        except Exception:
            return True   # 撤单结果不明：当作可能发出去了，交给调用方轮询确认（确认不了会自动暂停）

    # ---- 卖出（按状态对齐）----

    def _note_sell_intents(self, now: datetime) -> None:
        """模拟盘的卖出意图只记「这次怎么决定的」，结果看 live_orders；remote 意图保持 new（收尾见 _finish_remote_sells）。"""
        held = live_store.positions(self.conn)
        for intent in live_store.new_intents(self.conn):
            if intent.side != "sell" or intent.source != "sim":
                continue
            p = held.get(intent.code)
            if p is None:
                note = "程序没有这只"
            elif p.buy_date >= now.date():
                note = "当天买的，下个交易日卖"
            else:
                note = "按持仓对齐卖出"
            live_store.finish_intent(self.conn, intent.id, "done", note, now)

    def _to_sell(self, now: datetime) -> list[str]:
        """待卖：程序持仓里昨天或更早买的，且模拟账户已不持有，或有 remote 卖出意图（手机立即卖出）。"""
        sim_held = {code for (code,) in self.conn.execute(
            "SELECT code FROM positions WHERE account=? AND shares>0", (config.LIVE_ACCOUNT,))}
        remote = self.conn.execute("SELECT 1 FROM live_intents WHERE status='new' AND source='remote' "
                                   "AND side='sell' LIMIT 1").fetchone() is not None
        return sorted(code for code, p in live_store.positions(self.conn).items()
                      if p.buy_date < now.date() and (remote or code not in sim_held))

    def _in_sell_window(self, now: datetime) -> bool:
        t = now.time()
        return any(a <= t < b for a, b in config.LIVE_SELL_WINDOWS)

    def _align_sells(self, now: datetime) -> None:
        if now.date() != self._sell_day:
            self._sell_day = now.date()
            self._next_sell_try.clear()
            self._sell_rounds.clear()
        if not self._in_sell_window(now):
            return
        for code in self._to_sell(now):
            if live_store.paused(self.conn) is not None:
                return   # 前一只处理中自动暂停了：不再碰券商
            next_try = self._next_sell_try.get(code)
            if next_try is not None and next_try > now:
                continue
            self._sell(code, now)

    def _retry_later(self, code: str) -> None:
        # 从这一刻算起（不是从 step 开头算）：一轮下单+等待+撤单要一分多钟，从开头算会立刻重试
        self._next_sell_try[code] = self.clock() + timedelta(seconds=config.LIVE_SELL_RETRY)

    def _name(self, code: str) -> str:
        row = self.conn.execute("SELECT name FROM live_intents WHERE code=? AND name<>'' ORDER BY id DESC LIMIT 1",
                                (code,)).fetchone()
        return row[0] if row else code

    def _sell(self, code: str, now: datetime) -> None:
        if not self._follow_open_sells(code, now.date()):
            return
        while self._sell_round(code):
            pass

    def _follow_open_sells(self, code: str, today: date) -> bool:
        """先处理本地已经挂着的程序卖单：还 open 只更新成交，不重下；已了结就结算。
        返回 True = 没有挂着的卖单了，可以继续卖；False = 这次到此为止（已设好下次重试或已暂停）。"""
        rows = [r for r in live_store.orders_on(self.conn, today) if r["side"] == "sell" and r["code"] == code]
        if any(r["status"] == "sending" for r in rows):
            self._retry_later(code)   # 结果不明的卖单没核对完（启动/恢复时核对）：绝不再下一单
            return False
        rows = [r for r in rows if r["status"] == "submitted"]
        if not rows:
            return True
        try:
            orders = self._read(self.broker.orders_today)
        except Exception:
            self._retry_later(code)
            return False
        for row in rows:
            m = self._match_order(orders, code, "sell", row["price"], row["shares"], row["entrust_no"] or None,
                                  datetime.fromisoformat(row["ts"]))
            if m is None:
                live_store.update_order(self.conn, row["id"], self.clock(), note="当日委托里找不到")
                self._auto_pause(f"卖单核对不上：{self._name(code)} 当日委托里找不到这笔卖单，请在 App 里确认")
                return False
            if m.open and self._should_reprice(row):
                final, confirmed = self._cancel_and_confirm(code, "sell", row["price"], row["shares"],
                                                            m.entrust_no or None, datetime.fromisoformat(row["ts"]), m)
                if not confirmed:
                    live_store.update_order(self.conn, row["id"], self.clock(), note="委托状态查不到，需要人工核对")
                    self._auto_pause(f"委托状态查不到：{self._name(code)} 这笔卖单核对不到最终状态，请在 App 里确认")
                    return False
                m = final
            if m.open:
                self._record_sell_fill(row, m)
                self._retry_later(code)
                return False
            self._settle_sell(row, m)
        return True

    def _should_reprice(self, row: dict) -> bool:
        """挂着的卖单（跨了午休、或下单中途重启留下的）该不该撤了重挂：不是跌停排队、已经挂够 LIVE_SELL_WAIT、
        现在还能撤（收盘集合竞价里的留着）。"""
        age = (self.clock() - datetime.fromisoformat(row["ts"])).total_seconds()
        return row["note"] != "跌停排队" and age >= config.LIVE_SELL_WAIT and self._can_cancel_by(self.clock())

    def _sell_round(self, code: str) -> bool:
        """一轮卖出（最多一次 place）。返回 True = 撤完立即重挂下一轮；False = 这次尝试结束。"""
        conn = self.conn
        if live_store.paused(conn) is not None:
            return False   # 上一轮处理期间暂停了（自动/手机）：当前这轮已经做完，不开新的一轮
        t = self.clock()
        if not self._in_sell_window(t):
            self._retry_later(code)   # 等待/撤单期间过了收市点：不在时段外重挂
            return False
        if self._morning_sell_too_late(t):
            self._retry_later(code)
            return False
        pos = live_store.positions(conn).get(code)
        if pos is None or pos.shares <= 0:
            self._sell_rounds.pop(code, None)
            return False
        name = self._name(code)
        dryrun = live_store.mode(conn) == "dryrun"
        try:   # 先取行情再读客户端持仓：行情没有就不必白读一次要好几秒的网关
            q = self.quotes([code]).get(code)
        except Exception:
            q = None
        if q is None or q.price <= 0 or q.limit_down <= 0:
            self._notify("卖出", f"{name} 取不到有效行情，暂不卖", key=f"sell_quote:{code}")
            self._retry_later(code)
            return False
        if dryrun:
            qty = pos.shares   # 演练持仓客户端里本来就没有：不按可用封顶，也不读客户端
        else:
            try:
                holdings = self._read(self.broker.positions)
            except Exception:
                self._retry_later(code)
                return False
            self._reconcile(self.clock(), holdings)   # 下单前对账，复用刚读到的持仓
            pos = live_store.positions(conn).get(code)   # 对账可能已经减账
            if pos is None or pos.shares <= 0:
                self._sell_rounds.pop(code, None)
                return False
            held = holdings.get(code)
            cap = min(pos.shares, held.available if held else 0)
            qty = cap // 100 * 100   # 下单层只收 100 的倍数
            if qty == 0:
                if cap > 0:
                    self._notify("卖出", f"{name} 剩 {cap} 股零股，程序卖不了，请手动卖出；卖掉之前程序不会再跟买",
                                 key=f"odd_lot:{code}")
                self._retry_later(code)   # 客户端一股都没有：上面的对账已记下减账待确认
                return False
        queue = q.price <= q.limit_down + 0.005
        price = q.limit_down if queue else max(round(q.price * (1 - config.LIVE_SELL_DISCOUNT), 2), q.limit_down)

        if dryrun:
            live_store.add_order(conn, None, self.clock(), "sell", code, price, qty, "dryrun")
            live_store.reduce_position(conn, code, qty)
            self._sell_rounds.pop(code, None)
            self._notify("卖出（演练）", f"{name} {qty}股 @{price}")
            return False

        order_ts = self.clock()
        if self._morning_sell_too_late(order_ts) or self._paused():
            # 读持仓、取行情花了时间：下单前按现在的时钟再核一次；这期间手机点了暂停也不下
            self._retry_later(code)
            return False
        order_id = live_store.add_order(conn, None, order_ts, "sell", code, price, qty, "sending")
        entrust_no = self._place_sell(order_id, code, price, qty, order_ts)
        if entrust_no is None:
            return False
        row = {"id": order_id, "code": code, "price": price, "shares": qty, "filled": 0, "avg_price": 0.0}
        if queue:
            live_store.update_order(conn, order_id, self.clock(), note="跌停排队")   # 之后不撤它重挂
            self._notify("卖出", f"{name} 跌停，挂跌停价排队", key=f"limit_down:{code}")
            self._retry_later(code)
            return False

        morning = order_ts.time() < config.LIVE_CANCEL_WINDOWS[0][1]   # 上午必须在午休前撤掉，下午可以进集合竞价
        matched, closed = self._poll_fill(code, "sell", price, qty, entrust_no or None, order_ts,
                                          config.LIVE_SELL_WAIT, must_cancel=morning)
        entrust_no = entrust_no or (matched.entrust_no if matched is not None else "")
        if closed:
            self._settle_sell(row, matched)
        elif not self._can_cancel_by(self.clock()):
            if matched is not None:   # 收盘集合竞价撤不了：留 submitted，之后由第 1 步跟踪、收盘后结算
                self._record_sell_fill(row, matched)
            self._retry_later(code)
            return False
        else:
            final, confirmed = self._cancel_and_confirm(code, "sell", price, qty, entrust_no or None, order_ts,
                                                        matched)
            if not confirmed:
                live_store.update_order(conn, order_id, self.clock(), note="委托状态查不到，需要人工核对")
                self._auto_pause(f"委托状态查不到：{name} 这笔卖单核对不到最终状态，请在 App 里确认")
                return False
            self._settle_sell(row, final)

        left = live_store.positions(conn).get(code)
        if left is None or left.shares <= 0:
            self._sell_rounds.pop(code, None)
            return False
        rounds = self._sell_rounds.get(code, 0) + 1
        self._sell_rounds[code] = rounds
        if rounds < config.LIVE_SELL_ROUNDS:
            return True
        self._retry_later(code)
        self._notify("卖出", f"{name} 卖出没成功，之后每分钟重试", key=f"sell_retry:{code}")
        return False

    def _morning_sell_too_late(self, t: datetime) -> bool:
        """上午等完再撤会撞上午休（撤不了）：留到下午。下午临近 14:57 照下，没卖完的进收盘集合竞价。"""
        return t.time() < config.LIVE_CANCEL_WINDOWS[0][1] and \
            not self._can_cancel_by(t + timedelta(seconds=config.LIVE_SELL_WAIT))

    def _place_sell(self, order_id: int, code: str, price: float, qty: int, order_ts: datetime) -> str | None:
        """下卖单。返回委托号（拿不到号是 ""）；None = 这次没有可继续跟踪的委托（已处理好行状态/暂停/重试）。
        不管抛什么，这里只调一次 place。"""
        conn, name = self.conn, self._name(code)
        try:
            entrust_no = self.broker.place("sell", code, price, qty)
        except Exception as e:
            if isinstance(e, BrokerError) and e.code in REJECT_CODES:
                live_store.update_order(conn, order_id, self.clock(), status="failed", note=str(e))
                self._notify("卖出失败", f"{name} 券商拒单：{e}", key=f"sell_reject:{code}")
                self._retry_later(code)
                return None
            if isinstance(e, BrokerError) and e.code in INFLIGHT_CODES:
                live_store.update_order(conn, order_id, self.clock(), note=f"结果不明：{e}")
                self._auto_pause(f"卖出结果不明：{name} {e}，可能稍后才报出，恢复前请在 App 里核对")
                return None
            try:
                orders = self._read(self.broker.orders_today)
            except Exception:
                live_store.update_order(conn, order_id, self.clock(), note="下单结果不明：核对读不到当日委托")
                self._auto_pause(f"卖出结果不明：{name} 核对读不到当日委托")
                return None
            matched = self._match_order(orders, code, "sell", price, qty, None, order_ts)
            if matched is None:
                live_store.update_order(conn, order_id, self.clock(), status="failed",
                                        note="下单结果核对：没找到这笔委托")
                self._auto_pause(f"下单层返回认不出的错误：{name} 卖单核对没找到这笔委托")
                return None
            entrust_no = matched.entrust_no
        entrust_no = entrust_no or ""
        live_store.update_order(conn, order_id, self.clock(), status="submitted", entrust_no=entrust_no)
        return entrust_no

    def _record_sell_fill(self, row: dict, m) -> dict:
        """读到的成交数比行里记的多 → 只入账增量（减仓 + 更新行）。返回更新后的行。
        先记行再减仓：中途崩了宁可账上多（客户端可用余额封顶 + 对账会纠正），不能少。"""
        if m.entrust_no and not row.get("entrust_no"):
            live_store.update_order(self.conn, row["id"], self.clock(), entrust_no=m.entrust_no)   # 之后只按号跟踪
            row = {**row, "entrust_no": m.entrust_no}
        delta = m.filled - row["filled"]
        if delta <= 0:
            return row
        avg_price = m.avg_price if m.avg_price else row["price"]
        live_store.update_order(self.conn, row["id"], self.clock(), filled=m.filled, avg_price=avg_price)
        live_store.reduce_position(self.conn, row["code"], delta)
        return {**row, "filled": m.filled, "avg_price": avg_price}

    def _settle_sell(self, row: dict, m, closing: bool = False) -> dict:
        """卖单落终态（调用方保证：读成功且不再 open，或已收盘）：先入账增量，再写 filled/partial/cancelled。"""
        conn = self.conn
        p = live_store.positions(conn).get(row["code"])
        buy_price = p.buy_price if p else 0.0   # 减仓可能把持仓删掉，先取
        row = self._record_sell_fill(row, m)
        filled = row["filled"]
        status = "filled" if filled >= row["shares"] else ("partial" if filled > 0 else "cancelled")
        note = {"filled": "已成交", "partial": "部分成交，已撤单", "cancelled": "未成交，已撤单"}[status]
        if closing and status != "filled":
            note = "收盘未成交部分作废"
        live_store.update_order(conn, row["id"], self.clock(), status=status, note=note)
        if filled > 0:
            avg_price = row["avg_price"] or row["price"]
            pnl = f"{round((avg_price / buy_price - 1) * 100, 2)}%" if buy_price else "未知"
            self._notify("卖出", f"{self._name(row['code'])} {filled}股 @{avg_price}，盈亏 {pnl}")
        return row

    def _settle_after_close(self, now: datetime) -> None:
        """收盘后（LIVE_SELL_SETTLE，留 1 分钟等集合竞价成交回显）成交数已是最终值：读一次当日委托，把今天还
        submitted 的委托直接落终态——卖单（14:57 后留给集合竞价的），和万一挂进集合竞价的买单。"""
        if now.time() < config.LIVE_SELL_SETTLE:
            return
        today = now.date()
        if live_store.get_state(self.conn, "sell_settle_day") == today.isoformat():
            return
        rows = [r for r in live_store.orders_on(self.conn, today) if r["status"] == "submitted"]
        if not rows:
            return
        try:
            orders = self._read(self.broker.orders_today)
        except Exception:
            return   # 下一秒再试（_read 会计连续失败次数）
        for row in rows:
            m = self._match_order(orders, row["code"], row["side"], row["price"], row["shares"],
                                  row["entrust_no"] or None, datetime.fromisoformat(row["ts"]))
            if m is None:   # 不许猜：保持 submitted，交给人工
                live_store.update_order(self.conn, row["id"], self.clock(), note="收盘结算找不到这笔")
                self._auto_pause(f"收盘结算找不到委托：{row['code']} 这笔核对不上，请在 App 里确认")
                continue
            if row["side"] == "sell":
                self._settle_sell(row, m, closing=True)
            else:
                self._book_buy_row(row, m, closing=True)
        live_store.set_state(self.conn, "sell_settle_day", today.isoformat())

    # ---- 暂停 / 恢复 / 立即卖出 ----

    def _handle_resume(self, now: datetime) -> None:
        """手机点了恢复（resume_requested，自动暂停也靠它）或手动暂停到点 → 清暂停，再核对+对账。
        resume_requested 的值是用户点恢复时的暂停原因：处理时暂停已经换成别的（用户没看到的新暂停）就不清。
        自动暂停 until 为 None、且不是 manual，永远不会自己到点恢复。"""
        requested = live_store.get_state(self.conn, "resume_requested")
        if requested is not None:
            live_store.set_state(self.conn, "resume_requested", None)   # 先删键：中途崩了宁可再点一次，不留旧请求
            p = live_store.paused(self.conn)
            if p is not None and (p["reason"] or "") == requested:
                live_store.clear_pause(self.conn)
                self._after_resume(now)
        p = live_store.paused(self.conn)
        if p is not None and p["kind"] == "manual" and p["until"] is not None and now >= p["until"]:
            live_store.clear_pause(self.conn)   # 昨天的 until 今天也自然失效：暂停不跨天
            self._after_resume(now)

    def _after_resume(self, now: datetime) -> None:
        """恢复：先核对 sending/submitted 委托（读不到/对不上它自己会自动暂停，那就到此为止），再对账；
        之后本轮照常往下走（卖出对齐等）。"""
        self._reconcile_sending_orders()
        if self._paused():
            return
        self._reconcile(now)

    def _note_pause_change(self) -> None:
        """未暂停 → 暂停：手动暂停通知开始（自动暂停 _auto_pause 已经发过）；暂停 → 未暂停：通知结束。"""
        p = live_store.paused(self.conn)
        if p is not None and not self._was_paused and p["kind"] == "manual":
            until = p["until"]
            self._notify(f"暂停开始（到 {until:%H:%M}）" if until else "暂停开始（需手动恢复）", p["reason"] or "")
        elif p is None and self._was_paused:
            self._notify("暂停结束", "已恢复，先对账再处理待卖")
        self._was_paused = p is not None

    def _while_paused(self, now: datetime) -> None:
        """暂停中只动本地意图状态和发通知，绝不碰券商（自检只报「暂停中」，收盘小结只看本地库）。"""
        today = now.date().isoformat()
        if now.time() >= config.LIVE_SELF_CHECK and live_store.get_state(self.conn, "self_check_day") != today:
            live_store.set_state(self.conn, "self_check_day", today)
            p = live_store.paused(self.conn)
            until = f"到 {p['until']:%H:%M}" if p["until"] else "需手动恢复"
            self._notify("自检", f"暂停中（{until}：{p['reason']}），没读客户端")
        remote = False
        for intent in live_store.new_intents(self.conn):
            if intent.side == "buy":
                live_store.finish_intent(self.conn, intent.id, "skipped", "暂停中", now)
            elif intent.source == "sim":
                live_store.finish_intent(self.conn, intent.id, "done", "暂停中延后", now)
            elif intent.source == "remote":
                remote = True   # 保持 new，恢复后照常卖
        if remote:
            self._notify("立即卖出", "暂停中收到立即卖出，恢复后执行", key="paused_sell_all")
        for code in self._to_sell(now):
            self._notify("待卖", f"暂停中，{self._name(code)} 该卖了，程序没卖", key=f"paused_sell:{code}")
        self._close_summary(now)

    def _finish_remote_sells(self, now: datetime) -> None:
        """手机「立即卖出」收尾：程序清仓 → 意图 done，暂停到收盘（manual）；还有持仓 → 保持 new，
        当天买的卖不了，提醒一次，下个交易日开盘卖。已经在暂停（比如本轮刚自动暂停）就不动：
        不能把自动暂停覆盖成会自己恢复的手动暂停。"""
        if self._paused():
            return
        remote = [i for i in live_store.new_intents(self.conn) if i.source == "remote" and i.side == "sell"]
        if not remote:
            return
        held = live_store.positions(self.conn)
        if not held:
            for intent in remote:
                live_store.finish_intent(self.conn, intent.id, "done", "已卖完", now)
            end = datetime.combine(now.date(), config.LIVE_PAUSE_CLOSE)
            if now < end:   # 收盘后按下就没有「到收盘」可言：不留一条一出生就过期的暂停
                live_store.pause(self.conn, end, "立即卖出", "manual")
            return
        for code, p in sorted(held.items()):
            if p.buy_date >= now.date():
                self._notify("立即卖出", f"{self._name(code)} 今天买的，下个交易日开盘卖", key=f"sell_all_t1:{code}")

    # ---- 自检 / 对账 / 收盘小结 ----

    def _label(self, code: str) -> str:
        name = self._name(code)
        return code if name == code else f"{name}({code})"

    def _self_check(self, now: datetime) -> None:
        """每个交易日 LIVE_SELF_CHECK 之后的第一轮：读资金和客户端持仓确认在线，通过也发通知。
        先记 self_check_day 再读：成功失败都只发一次。"""
        today = now.date().isoformat()
        if now.time() < config.LIVE_SELF_CHECK or live_store.get_state(self.conn, "self_check_day") == today:
            return
        live_store.set_state(self.conn, "self_check_day", today)
        try:
            balance = self._read(self.broker.balance)
            self._read(self.broker.positions)
        except Exception as e:
            self._notify("自检失败", f"读客户端失败：{e}")
            return
        held = live_store.positions(self.conn)
        desc = "、".join(f"{self._label(code)} {p.shares}股" for code, p in sorted(held.items())) or "空仓"
        self._notify("自检通过", f"模式 {live_store.mode(self.conn)}，可用资金 {balance:.2f} 元，程序持仓 {desc}")

    def _scheduled_reconcile(self, now: datetime) -> None:
        """只在卖出时段（午休、收盘后不对账）：有持仓每 LIVE_RECONCILE_HOLDING 分钟对账；空仓每
        LIVE_RECONCILE_IDLE 分钟读一次资金确认在线（失败只计数）。"""
        if not self._in_sell_window(now):
            return
        holding = bool(live_store.positions(self.conn))
        gap = timedelta(minutes=config.LIVE_RECONCILE_HOLDING if holding else config.LIVE_RECONCILE_IDLE)
        if self._last_reconcile is not None and now - self._last_reconcile < gap:
            return
        if holding:
            self._reconcile(now)
            return
        self._last_reconcile = now
        try:
            self._read(self.broker.balance)
        except Exception:
            pass   # 只计数：_read 连续失败到阈值会自动暂停

    def _reconcile(self, now: datetime, holdings: dict | None = None) -> None:
        """对账（spec「对账」）：客户端股数比程序账少 → 连续两次读到同一个数、间隔 ≥ LIVE_REDUCE_CONFIRM 秒才按客户端
        减账（记为人工卖出）；不少（多出的是用户的）→ 不动。holdings 为 None 时自己读，读失败不改账。
        dryrun 只读不改账。今天这只有 sending/submitted 委托的不对账：成交由委托跟踪入账，这里再减就重复了。"""
        conn = self.conn
        mode = live_store.mode(conn)
        if mode == "off":
            return
        self._last_reconcile = now
        if holdings is None:
            try:
                holdings = self._read(self.broker.positions)
            except Exception:
                return
        if mode == "dryrun":
            return
        held = live_store.positions(conn)
        for (key,) in conn.execute("SELECT key FROM live_state WHERE key LIKE 'reduce:%'").fetchall():
            if key[len("reduce:"):] not in held:
                live_store.set_state(conn, key, None)   # 程序已经不持有了：旧的待确认不能留给以后的持仓
        busy = {r["code"] for r in live_store.orders_on(conn, now.date()) if r["status"] in ("sending", "submitted")}
        for code, p in held.items():
            if code in busy or p.buy_date >= now.date():
                # 有挂着的委托：成交由委托跟踪入账；当天买的：T+1 当天不可能卖出，读到少了只会是客户端口径问题
                continue
            client = holdings.get(code)
            client_shares = client.shares if client else 0
            key = f"reduce:{code}"
            if client_shares >= p.shares:
                live_store.set_state(conn, key, None)
                continue
            seen = self._reduce_pending(key)
            if seen is not None and seen[0] == client_shares:
                if (now - seen[1]).total_seconds() >= config.LIVE_REDUCE_CONFIRM:
                    live_store.reduce_position(conn, code, p.shares - client_shares)
                    live_store.set_state(conn, key, None)
                    self._notify("按客户端减账", f"按客户端减账：{self._name(code)} 程序账 {p.shares} 股 → "
                                                f"{client_shares} 股（记为人工卖出）")
                continue   # 同一个数但还不到 60 秒：保持原来的计时
            live_store.set_state(conn, key, json.dumps({"shares": client_shares, "ts": now.isoformat()}))

    def _reduce_pending(self, key: str):
        """读减账待确认：(客户端股数, 时间)；没有或读不懂当没有。"""
        raw = live_store.get_state(self.conn, key)
        if raw is None:
            return None
        try:
            d = json.loads(raw)
            return int(d["shares"]), datetime.fromisoformat(d["ts"])
        except (ValueError, KeyError, TypeError):
            return None

    def _close_summary(self, now: datetime) -> None:
        """收盘小结：LIVE_CLOSE_SUMMARY 之后、收盘后结算完成才发（结算一直读不到，到 LIVE_CLOSE_SUMMARY_LATEST 也发，
        注明有委托没结算）。只看本地库，不读券商。"""
        conn, today = self.conn, now.date().isoformat()
        if now.time() < config.LIVE_CLOSE_SUMMARY or live_store.get_state(conn, "close_summary_day") == today:
            return
        orders = live_store.orders_on(conn, now.date())
        unsettled = live_store.get_state(conn, "sell_settle_day") != today and any(
            r["status"] == "submitted" for r in orders)
        if unsettled and now.time() < config.LIVE_CLOSE_SUMMARY_LATEST:
            return
        live_store.set_state(conn, "close_summary_day", today)
        filled = sum(1 for r in orders if r["filled"] > 0 or r["status"] == "dryrun")
        skipped = conn.execute(
            "SELECT note, COUNT(*) FROM live_intents WHERE status IN ('skipped', 'expired') "
            "AND substr(handled_ts, 1, 10)=? GROUP BY note ORDER BY COUNT(*) DESC, note", (today,)).fetchall()
        groups = "、".join(f"{note or '未注明'} {n}" for note, n in skipped)
        held = live_store.positions(conn)
        desc = "、".join(f"{code} {p.shares}股 成本价 {p.buy_price}" for code, p in sorted(held.items())) or "空仓"
        sim = conn.execute("SELECT COUNT(*) FROM fills WHERE account=? AND substr(ts, 1, 10)=?",
                           (config.LIVE_ACCOUNT, today)).fetchone()[0]
        text = (f"模式 {live_store.mode(conn)}；实盘委托 {len(orders)} 笔，成交 {filled} 笔；"
                f"跳过 {sum(n for _, n in skipped)} 笔" + (f"（{groups}）" if groups else "") +
                f"；程序持仓 {desc}；模拟成交 {sim} 笔" + ("；有委托没结算" if unsettled else ""))
        self._notify("收盘小结", text)

    # ---- sending / submitted 恢复（启动核对）----

    def _reconcile_sending_orders(self) -> None:
        """启动/恢复时核对 sending/submitted 委托。前几天遗留的读不到了（当日委托只有今天的）：
        标成 unknown、自动暂停、报清楚是哪几笔——不能静默丢掉（对账只会减账，丢了的买单补不回来）。"""
        now = self.clock()
        today = now.date().isoformat()
        pending = live_store.open_orders(self.conn)
        stale = [r for r in pending if r["ts"][:10] < today]
        if stale:
            for r in stale:
                live_store.update_order(self.conn, r["id"], now, status="unknown",
                                        note="隔日未核对：成交与否以 App 为准")
            desc = "、".join(f"{r['ts'][:10]} {'买' if r['side'] == 'buy' else '卖'} {self._label(r['code'])}"
                            for r in stale)
            self._auto_pause(f"前几天有委托没核对到最终状态：{desc}。买单如果成交了，这只现在算你的持仓，"
                             f"程序不会卖；卖单以客户端持仓为准。在 App 里处理好后再点恢复")
            return
        rows = [r for r in pending if r["ts"][:10] == today]
        if not rows:
            return
        try:
            orders = self._read(self.broker.orders_today)
        except Exception:
            self._auto_pause("启动核对读不到当日委托")
            return
        for row in rows:
            if self._paused():
                return   # 前一行对不上已经自动暂停：后面的不再碰券商，恢复时再核对
            self._reconcile_open_order_row(row, orders)

    def _reconcile_open_order_row(self, row: dict, orders: list) -> None:
        conn, now = self.conn, self.clock()
        since = datetime.fromisoformat(row["ts"])
        # submitted 行已经有委托号就只认委托号：不然可能认成用户后来手动下的一笔四要素相同的单
        matched = self._match_order(orders, row["code"], row["side"], row["price"], row["shares"],
                                    row["entrust_no"] or None, since)
        if matched is None:
            live_store.update_order(conn, row["id"], now, status="failed",
                                    note="启动核对：没找到这笔委托")
            self._auto_pause(f"启动核对：{row['code']} 这笔委托核对不到，需要人工确认")
            self._finish_row_intent(row["intent_id"], "启动核对：没找到这笔委托")
            return
        live_store.update_order(conn, row["id"], now, status="submitted", entrust_no=matched.entrust_no)
        row = {**row, "entrust_no": matched.entrust_no}
        closing = now.time() >= config.LIVE_SELL_SETTLE   # 收盘后成交数已定：直接按最终状态落账，不撤单
        if row["side"] == "sell":
            # 卖单：closed（或已收盘）→ 增量 + 终态；还 open → 不撤，只入账增量，留 submitted 交给 _sell 跟踪
            if matched.open and not closing:
                self._record_sell_fill(row, matched)
                self._finish_row_intent(row["intent_id"], "挂单仍在，继续跟踪")
            else:
                self._settle_sell(row, matched, closing=closing)
                self._finish_row_intent(row["intent_id"], "启动核对：卖单已了结")
            return
        if not matched.open or closing:
            final, confirmed = matched, True
        else:
            final, confirmed = self._cancel_and_confirm(row["code"], row["side"], row["price"], row["shares"],
                                                         matched.entrust_no, since, matched)
        if not confirmed:
            live_store.update_order(conn, row["id"], self.clock(), note="委托状态查不到，需要人工核对")
            self._auto_pause(f"委托状态查不到：{row['code']} 这笔委托核对不到最终状态，请在 App 里确认")
            self._finish_row_intent(row["intent_id"], "结果不明（委托状态查不到）")
            return
        self._book_buy_row(row, final, closing=closing)

    def _book_buy_row(self, row: dict, final, closing: bool = False) -> None:
        """买单按最终状态落账（核对补记 / 收盘后结算用）：成交了就记程序持仓，写终态，收尾意图。"""
        conn, now = self.conn, self.clock()
        filled = final.filled
        avg_price = final.avg_price if final.avg_price else row["price"]
        status = "filled" if filled >= row["shares"] else ("partial" if filled > 0 else "cancelled")
        note = {"filled": "已成交", "partial": "部分成交，已撤单", "cancelled": "未成交，已撤单"}[status]
        if closing and status != "filled":
            note = "收盘未成交部分作废"
        live_store.update_order(conn, row["id"], now, status=status, filled=filled, avg_price=avg_price, note=note,
                                entrust_no=final.entrust_no or row.get("entrust_no") or "")
        if filled > 0:
            cost = filled * avg_price + ledger.buy_fee(filled * avg_price)
            sim_price = self._intent_sim_price(row["intent_id"])
            live_store.put_position(conn, LivePosition(row["code"], filled, cost, avg_price,
                                                        datetime.fromisoformat(row["ts"]).date(), sim_price))
            self._notify("买入", f"{self._name(row['code'])} {filled}股 @{avg_price}（核对补记）")
        else:   # 买单落成没成交就没有对账兜底了（对账只减不加）：告诉用户去 App 看一眼
            self._notify("买入", f"{self._name(row['code'])} 核对为没成交（{note}），请在 App 里确认")
        self._finish_row_intent(row["intent_id"], note)

    def _finish_row_intent(self, intent_id, note: str) -> None:
        if intent_id is not None:
            live_store.finish_intent(self.conn, intent_id, "done", note, self.clock())

    def _intent_sim_price(self, intent_id) -> float:
        if intent_id is None:
            return 0.0
        row = self.conn.execute("SELECT sim_price FROM live_intents WHERE id=?", (intent_id,)).fetchone()
        return row[0] if row else 0.0

    # ---- 读取 / 通知 / 自动暂停（公共基础设施）----

    def _read(self, fn):
        """统一读取：成功重置 read_fail_streak、写 last_read_ok；任何异常都算一次失败，+1，到
        LIVE_READ_FAIL_PAUSE 自动暂停，然后照样抛出（调用方自己决定这次失败要不要紧急处理）。
        每次读之前顺手写心跳：几轮卖出下来一次 step 能超过看门狗的 LIVE_WATCHDOG_STALE，不能被当成卡死杀掉。"""
        live_store.set_state(self.conn, "heartbeat", self.clock().isoformat())
        try:
            result = fn()
        except Exception:
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
        """已经在自动暂停中就不再重新暂停/重复推送——避免级联失败时反复覆盖更具体的原因、刷屏报警。
        手动暂停（到点会自动恢复）中撞上意外 → 升级成自动暂停，只能手动恢复。"""
        p = live_store.paused(self.conn)
        if p is not None and p["kind"] == "auto":
            return
        live_store.pause(self.conn, None, reason, "auto")
        self._notify("自动暂停", f"{reason}，需要手动恢复")
