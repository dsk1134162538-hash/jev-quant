"""实盘执行器：live.py 每秒调 Executor.step()。已实现骨架、心跳、模式/交易日门槛、买入全流程（含等成交、
核对撤单、sending 恢复）、卖出对齐（Task 4：待卖、重挂、跌停排队、14:57 后不撤、收盘结算）；
暂停/对账/自检（Task 5）接着扩展。

真金白银的两条底线：
  1. 委托最终状态只在「成功读到 + broker 判定它不再 open」时才落成 filled/partial/cancelled 这类终态；
     读不到、或读到了但还 open，一律不许瞎猜，维持 submitted 并 _auto_pause，交给人工核对（见 _wait_fill /
     _cancel_and_confirm）。唯一的例外是收盘后结算（_settle_sells_after_close）：收盘后成交数已定。
  2. 一个意图 / 一次卖出尝试最多只产生一次 place() 调用：add_order(sending) 后立刻 finish_intent（买），
     place() 之后不管抛什么异常都不放它跑出 _buy_live / _place_sell；_buy 顶部另外兜底一次「这个意图是不是
     已经有委托了」，_sell 在今天这只还有 sending 卖单时绝不再下。
"""
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

    # ---- 主循环 ----

    def startup(self) -> None:
        """程序重启后先核对 sending/submitted 委托（防重复下单，spec「防重复下单」）。
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

        if live_store.paused(self.conn) is not None:
            return   # 暂停中不碰券商；暂停期间的意图/提醒留给 Task 5

        self._note_sell_intents(now)
        self._align_sells(now)
        for intent in live_store.new_intents(self.conn):
            if intent.side == "buy":
                self._buy(intent)
        self._settle_sells_after_close(self.clock())

    def _skip_new_intents(self, reason: str) -> None:
        now = self.clock()
        for intent in live_store.new_intents(self.conn):
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
        return self._cancel_and_confirm(code, side, price, shares, entrust_no, since_ts, matched)

    def _poll_fill(self, code: str, side: str, price: float, shares: int, entrust_no, since_ts: datetime,
                   wait: float):
        """只轮询不撤单：每 LIVE_POLL 秒读一次当日委托，直到确认不再 open 或等够 wait 秒（按时钟算：网关一次读
        要好几秒）。返回 (matched, closed)：closed=True 才是终态（成功读到 + 不再 open）；False 时 matched
        只是最后一次读到的参考（可能是 None）。"""
        deadline = self.clock() + timedelta(seconds=wait)
        matched = None
        while True:
            self.sleep(config.LIVE_POLL)
            try:
                orders = self._read(self.broker.orders_today)
            except Exception:
                orders = None
            if orders is not None:
                matched = self._match_order(orders, code, side, price, shares, entrust_no, since_ts)
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
        if entrust_no:
            return next((o for o in orders if o.entrust_no == entrust_no), None)
        since = (since_ts - timedelta(seconds=5)).strftime("%H:%M:%S")
        candidates = [o for o in orders if o.code == code and o.side == side and _price_eq(o.price, price)
                     and o.shares == shares and o.time >= since]
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
        try:
            return self.broker.cancel_last()
        except Exception:
            return True   # 撤单结果不明：当作可能发出去了，交给调用方轮询确认（确认不了会自动暂停）

    # ---- 卖出（按状态对齐）----

    def _note_sell_intents(self, now: datetime) -> None:
        """模拟盘的卖出意图只记「这次怎么决定的」，结果看 live_orders；remote 意图保持 new（Task 5 收尾）。"""
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
            self._retry_later(code)   # 结果不明的卖单没核对完（Task 5 恢复时核对）：绝不再下一单
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
            if m.open:
                self._record_sell_fill(row, m)
                self._retry_later(code)
                return False
            self._settle_sell(row, m)
        return True

    def _sell_round(self, code: str) -> bool:
        """一轮卖出（最多一次 place）。返回 True = 撤完立即重挂下一轮；False = 这次尝试结束。"""
        conn = self.conn
        t = self.clock()
        if not self._in_sell_window(t):
            self._retry_later(code)   # 等待/撤单期间过了收市点：不在时段外重挂
            return False
        if t.time() < config.LIVE_CANCEL_WINDOWS[0][1] and \
                not self._can_cancel_by(t + timedelta(seconds=config.LIVE_SELL_WAIT)):
            # 上午等完再撤会撞上午休（撤不了）：留到下午。下午临近 14:57 照下，没卖完的进收盘集合竞价
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
            held = holdings.get(code)
            cap = min(pos.shares, held.available if held else 0)
            qty = cap // 100 * 100   # 下单层只收 100 的倍数
            if qty == 0:
                if cap > 0:
                    self._notify("卖出", f"{name} 剩 {cap} 股零股，程序卖不了，请手动卖出", key=f"odd_lot:{code}")
                self._retry_later(code)   # 客户端一股都没有的情况留给 Task 5 对账减账
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
        order_id = live_store.add_order(conn, None, order_ts, "sell", code, price, qty, "sending")
        entrust_no = self._place_sell(order_id, code, price, qty, order_ts)
        if entrust_no is None:
            return False
        row = {"id": order_id, "code": code, "price": price, "shares": qty, "filled": 0, "avg_price": 0.0}
        if queue:
            self._notify("卖出", f"{name} 跌停，挂跌停价排队", key=f"limit_down:{code}")
            self._retry_later(code)
            return False

        matched, closed = self._poll_fill(code, "sell", price, qty, entrust_no or None, order_ts,
                                          config.LIVE_SELL_WAIT)
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

    def _settle_sells_after_close(self, now: datetime) -> None:
        """收盘后（LIVE_SELL_SETTLE，留 1 分钟等集合竞价成交回显）成交数已是最终值：读一次当日委托，把今天还 submitted 的卖单直接落终态（没卖完的部分作废）。"""
        if now.time() < config.LIVE_SELL_SETTLE:
            return
        today = now.date()
        if live_store.get_state(self.conn, "sell_settle_day") == today.isoformat():
            return
        rows = [r for r in live_store.orders_on(self.conn, today) if r["side"] == "sell" and r["status"] == "submitted"]
        if not rows:
            return
        try:
            orders = self._read(self.broker.orders_today)
        except Exception:
            return   # 下一秒再试（_read 会计连续失败次数）
        for row in rows:
            m = self._match_order(orders, row["code"], "sell", row["price"], row["shares"],
                                  row["entrust_no"] or None, datetime.fromisoformat(row["ts"]))
            if m is None:   # 不许猜：保持 submitted，交给人工
                live_store.update_order(self.conn, row["id"], self.clock(), note="收盘结算找不到这笔")
                self._auto_pause(f"收盘结算找不到卖单：{row['code']} 这笔卖单核对不上，请在 App 里确认")
                continue
            self._settle_sell(row, m, closing=True)
        live_store.set_state(self.conn, "sell_settle_day", today.isoformat())

    # ---- sending / submitted 恢复（启动核对）----

    def _reconcile_sending_orders(self) -> None:
        today = self.clock().date().isoformat()
        rows = [r for r in live_store.open_orders(self.conn) if r["ts"][:10] == today]
        if not rows:
            return
        try:
            orders = self._read(self.broker.orders_today)
        except Exception:
            self._auto_pause("启动核对读不到当日委托")
            return
        for row in rows:
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
        if row["side"] == "sell":
            # 卖单：closed → 增量 + 终态；还 open → 不撤，只入账增量，留 submitted 交给 _sell 跟踪
            row = {**row, "entrust_no": matched.entrust_no}
            if matched.open:
                self._record_sell_fill(row, matched)
                self._finish_row_intent(row["intent_id"], "挂单仍在，继续跟踪")
            else:
                self._settle_sell(row, matched)
                self._finish_row_intent(row["intent_id"], "启动核对：卖单已了结")
            return
        if not matched.open:
            final, confirmed = matched, True
        else:
            final, confirmed = self._cancel_and_confirm(row["code"], row["side"], row["price"], row["shares"],
                                                         matched.entrust_no, since, matched)
        if not confirmed:
            live_store.update_order(conn, row["id"], self.clock(), note="委托状态查不到，需要人工核对")
            self._auto_pause(f"委托状态查不到：{row['code']} 这笔委托核对不到最终状态，请在 App 里确认")
            self._finish_row_intent(row["intent_id"], "结果不明（委托状态查不到）")
            return
        filled = final.filled
        avg_price = final.avg_price if final.avg_price else row["price"]
        status = "filled" if filled >= row["shares"] else ("partial" if filled > 0 else "cancelled")
        live_store.update_order(conn, row["id"], self.clock(), status=status, filled=filled, avg_price=avg_price)
        if row["side"] == "buy" and filled > 0:
            cost = filled * avg_price + ledger.buy_fee(filled * avg_price)
            sim_price = self._intent_sim_price(row["intent_id"])
            live_store.put_position(conn, LivePosition(row["code"], filled, cost, avg_price,
                                                        self.clock().date(), sim_price))
        note = {"filled": "已成交", "partial": "部分成交，已撤单", "cancelled": "未成交，已撤单"}[status]
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
        LIVE_READ_FAIL_PAUSE 自动暂停，然后照样抛出（调用方自己决定这次失败要不要紧急处理）。"""
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
