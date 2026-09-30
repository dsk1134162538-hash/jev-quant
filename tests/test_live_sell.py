"""Executor 卖出对齐（Task 4）：FakeBroker/FakeClock/FakeNotifier 注入，全程不联网、不真等。
写法照抄 test_live_buy.py。"""
from dataclasses import replace
from datetime import date, datetime, timedelta

from factories import quote
from jevq import config, db
from jevq.live import store as live_store
from jevq.live.broker import BrokerError
from jevq.live.executor import Executor
from jevq.live.store import LivePosition
from jevq.models import Fill
from live_fakes import FakeBroker, FakeClock, FakeNotifier

TODAY = date(2026, 9, 29)
YESTERDAY = date(2026, 9, 28)


def conn():
    return db.connect(":memory:")


def quotes_fn(pct=0.0, pre_close=10.0):
    return lambda codes: {c: quote(code=c, pct=pct, pre_close=pre_close) for c in codes}


def make(c, broker, notifier=None, clock=None, quotes=None):
    return Executor(c, broker, notifier or FakeNotifier(), clock=clock.now, sleep=clock.sleep,
                    quotes=quotes or quotes_fn())


def setup_ready(c, clock, mode="live"):
    live_store.set_mode(c, mode)
    live_store.set_state(c, "trading_day", clock.now().date().isoformat())


def hold(c, code="600001", shares=500, buy_price=10.0, buy_date=YESTERDAY):
    """程序持仓（live_positions）。"""
    live_store.put_position(c, LivePosition(code, shares, shares * buy_price, buy_price, buy_date, buy_price))


def sim_holds(c, code="600001", shares=500):
    """模拟账户 LIVE_ACCOUNT 还持有这只（positions 表）。"""
    c.execute("INSERT OR REPLACE INTO positions(account, code, shares, cost, buy_price, buy_date) "
              "VALUES (?, ?, ?, ?, ?, ?)",
              (config.LIVE_ACCOUNT, code, shares, shares * 10.0, 10.0, YESTERDAY.isoformat()))
    c.commit()


def remember_name(c, now, code="600001", name="甲公司"):
    live_store.add_intents(c, [Fill(config.LIVE_ACCOUNT, code, "buy", 10.0, 500, 5.0, now, "开仓")], {code: name})
    c.execute("UPDATE live_intents SET status='done'")
    c.commit()


def add_sim_sell(c, now, code="600001"):
    live_store.add_intents(c, [Fill(config.LIVE_ACCOUNT, code, "sell", 10.5, 500, 5.0, now, "测试")],
                           {code: "甲公司"})


def sell_orders(c, day=TODAY):
    return [o for o in live_store.orders_on(c, day) if o["side"] == "sell"]


def places(broker):
    return [call for call in broker.calls if call[0] == "place"]


def texts(notifier, word):
    return [text for title, text in notifier.sent if word in title or word in text]


def paused_reason(c):
    p = live_store.paused(c)
    return None if p is None else (p["kind"], p["reason"])


# ---- 基本卖出 ----

def test_sell_when_sim_no_longer_holds_places_discounted_limit_and_settles():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500, buy_price=10.0)
    remember_name(c, clock.now())
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    notifier = FakeNotifier()
    make(c, broker, notifier, clock=clock).step()

    assert places(broker) == [("place", "sell", "600001", round(10.0 * (1 - config.LIVE_SELL_DISCOUNT), 2), 500)]
    order = sell_orders(c)[0]
    assert order["status"] == "filled" and order["filled"] == 500
    assert live_store.positions(c) == {}
    sold = [text for title, text in notifier.sent if "卖出" in title]
    assert len(sold) == 1 and "甲公司" in sold[0] and "盈亏" in sold[0] and "-1.0%" in sold[0]


def test_sim_position_with_zero_shares_counts_as_not_held():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    sim_holds(c, shares=0)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    make(c, broker, clock=clock).step()
    assert len(places(broker)) == 1


def test_sells_are_aligned_before_buy_intents_in_the_same_step():
    """程序只允许 1 个持仓：先把该卖的卖掉，同一秒里的买入意图才不会因为「程序已有持仓」被跳过。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, code="600001", shares=500)
    live_store.add_intents(c, [Fill(config.LIVE_ACCOUNT, "600002", "buy", 10.0, 500, 5.0, clock.now(), "测试")],
                           {"600002": "乙公司"})
    broker = FakeBroker(clock, cash=50_000, holdings={"600001": [500, 500]})
    make(c, broker, clock=clock).step()

    assert [(call[1], call[2]) for call in places(broker)] == [("sell", "600001"), ("buy", "600002")]
    assert set(live_store.positions(c)) == {"600002"}


def test_sell_skips_position_bought_today():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, buy_date=TODAY)
    broker = FakeBroker(clock, holdings={"600001": [500, 0]})
    make(c, broker, clock=clock).step()
    assert broker.calls == []
    assert live_store.positions(c)["600001"].shares == 500


def test_sim_sell_intent_for_code_program_does_not_hold_is_done_with_note():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_sim_sell(c, clock.now(), code="600001")
    broker = FakeBroker(clock, holdings={})
    make(c, broker, clock=clock).step()
    assert c.execute("SELECT status, note FROM live_intents").fetchone() == ("done", "程序没有这只")
    assert broker.calls == []


def test_sim_sell_intent_notes_describe_the_decision():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, code="600001", buy_date=YESTERDAY)
    hold(c, code="600002", buy_date=TODAY)
    for code in ("600001", "600002", "600003"):
        add_sim_sell(c, clock.now(), code=code)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    make(c, broker, clock=clock).step()

    notes = dict(c.execute("SELECT code, note FROM live_intents").fetchall())
    assert notes == {"600001": "按持仓对齐卖出", "600002": "当天买的，下个交易日卖", "600003": "程序没有这只"}
    assert {s for (s,) in c.execute("SELECT status FROM live_intents")} == {"done"}
    assert [call[2] for call in places(broker)] == ["600001"]


def test_sell_only_client_available_when_less_than_program_shares():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 300]})
    make(c, broker, clock=clock).step()

    assert [call[4] for call in places(broker)] == [300]   # 余下的 200 客户端没有可用，不再下单
    assert live_store.positions(c)["600001"].shares == 200


def test_sell_qty_rounds_down_to_lot_and_odd_lot_is_reported_once():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=250)
    broker = FakeBroker(clock, holdings={"600001": [250, 250]})
    notifier = FakeNotifier()
    ex = make(c, broker, notifier, clock=clock)
    ex.step()

    assert [call[4] for call in places(broker)] == [200]
    assert live_store.positions(c)["600001"].shares == 50
    assert len(texts(notifier, "零股")) == 1 and "50" in texts(notifier, "零股")[0]
    clock.sleep(61)
    ex.step()
    assert len(places(broker)) == 1
    assert len(texts(notifier, "零股")) == 1


def test_sell_nothing_when_only_odd_lot_left_notifies_and_does_not_place():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=80)
    broker = FakeBroker(clock, holdings={"600001": [80, 80]})
    notifier = FakeNotifier()
    make(c, broker, notifier, clock=clock).step()
    assert places(broker) == []
    assert len(texts(notifier, "零股")) == 1


# ---- 跌停 ----

def test_limit_down_queues_at_limit_down_price_without_cancel_and_notifies_once():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    notifier = FakeNotifier()
    ex = make(c, broker, notifier, clock=clock, quotes=quotes_fn(pct=-10))
    ex.step()

    assert places(broker) == [("place", "sell", "600001", 9.0, 500)]
    assert ("cancel_last",) not in broker.calls
    assert sell_orders(c)[0]["status"] == "submitted"
    assert len(texts(notifier, "跌停")) == 1

    for _ in range(59):        # 一分钟内不重复下单，也不重复通知
        clock.sleep(1)
        ex.step()
    assert len(places(broker)) == 1

    clock.sleep(2)             # 过了一分钟：只跟踪，不重下，不撤
    ex.step()
    assert len(places(broker)) == 1 and ("cancel_last",) not in broker.calls
    assert len(texts(notifier, "跌停")) == 1

    broker.fill_mode = "all"   # 之后成交 → 持仓减少
    clock.sleep(61)
    ex.step()
    assert live_store.positions(c) == {}
    assert sell_orders(c)[0]["status"] == "filled"
    assert len(places(broker)) == 1


def test_open_sell_tracking_books_only_the_fill_delta():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode, broker.fill_ratio = "partial", 0.5      # 挂出去就成 200
    ex = make(c, broker, clock=clock, quotes=quotes_fn(pct=-10))
    ex.step()
    assert live_store.positions(c)["600001"].shares == 500    # 排队不读，还没入账

    clock.sleep(60)
    ex.step()
    assert live_store.positions(c)["600001"].shares == 300
    order = sell_orders(c)[0]
    assert order["status"] == "submitted" and order["filled"] == 200

    clock.sleep(60)
    ex.step()                                                  # 成交数没变：不能再减一次
    assert live_store.positions(c)["600001"].shares == 300

    broker.fill_mode = "all"
    clock.sleep(60)
    ex.step()
    assert live_store.positions(c) == {}
    order = sell_orders(c)[0]
    assert order["status"] == "filled" and order["filled"] == 500
    assert len(places(broker)) == 1


# ---- 不成交 / 部分成交 / 重挂 ----

def test_no_fill_cancels_and_rehangs_three_rounds_then_retries_every_minute_notifying_once():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    notifier = FakeNotifier()
    ex = make(c, broker, notifier, clock=clock)
    t0 = clock.now()
    ex.step()

    assert len(places(broker)) == config.LIVE_SELL_ROUNDS
    assert broker.calls.count(("cancel_last",)) == config.LIVE_SELL_ROUNDS
    assert [o["status"] for o in sell_orders(c)] == ["cancelled"] * config.LIVE_SELL_ROUNDS
    assert clock.now() >= t0 + timedelta(seconds=config.LIVE_SELL_WAIT * config.LIVE_SELL_ROUNDS)
    assert len(texts(notifier, "每分钟重试")) == 1

    for _ in range(config.LIVE_SELL_RETRY - 1):   # 这一分钟内不再下单
        clock.sleep(1)
        ex.step()
    assert len(places(broker)) == config.LIVE_SELL_ROUNDS

    clock.sleep(1)                                # 一分钟到了：重试一轮（下单、等、撤）
    ex.step()
    assert len(places(broker)) == config.LIVE_SELL_ROUNDS + 1
    assert broker.calls.count(("cancel_last",)) == config.LIVE_SELL_ROUNDS + 1
    assert len(texts(notifier, "每分钟重试")) == 1
    assert live_store.paused(c) is None


def test_partial_fill_then_cancel_books_filled_and_rehangs_remainder():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode, broker.fill_ratio = "partial", 0.5
    notifier = FakeNotifier()
    make(c, broker, notifier, clock=clock).step()

    # 500 → 成 200 撤 → 剩 300 重挂 → 成 100 撤 → 剩 200 重挂 → 成 100 撤 → 剩 100
    assert [call[4] for call in places(broker)] == [500, 300, 200]
    first = sell_orders(c)[0]
    assert first["status"] == "partial" and first["filled"] == 200
    assert live_store.positions(c)["600001"].shares == 100
    assert len(texts(notifier, "盈亏")) == 3


def test_async_cancel_rehangs_normally_without_pausing():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    broker.cancel_delay_polls = 2
    make(c, broker, clock=clock).step()

    assert live_store.paused(c) is None
    assert len(places(broker)) == config.LIVE_SELL_ROUNDS
    assert [o["status"] for o in sell_orders(c)] == ["cancelled"] * config.LIVE_SELL_ROUNDS


def test_cancel_never_confirmed_keeps_sell_submitted_and_auto_pauses():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    broker.cancel_delay_polls = 999
    make(c, broker, clock=clock).step()

    order = sell_orders(c)[0]
    assert order["status"] == "submitted" and "委托状态查不到" in order["note"]
    assert len(places(broker)) == 1
    kind, reason = paused_reason(c)
    assert kind == "auto" and "委托状态查不到" in reason
    assert live_store.positions(c)["600001"].shares == 500


def test_reads_failing_after_place_never_guess_cancelled_or_place_again():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    orig_place = broker.place

    def place_then_fail_reads(*a):
        r = orig_place(*a)
        broker.fail_reads = 50
        return r

    broker.place = place_then_fail_reads
    ex = make(c, broker, clock=clock)
    ex.step()
    clock.sleep(1)
    ex.step()

    assert sell_orders(c)[0]["status"] == "submitted"
    assert len(places(broker)) == 1
    assert live_store.positions(c)["600001"].shares == 500
    assert live_store.paused(c) is not None


def test_rehang_is_not_placed_outside_sell_window():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 11, 29, 45))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    ex = make(c, broker, clock=clock)
    ex.step()
    assert len(places(broker)) == 1                 # 撤完已经过了 11:30：不重挂
    assert sell_orders(c)[0]["status"] == "cancelled"

    clock.t = datetime(2026, 9, 29, 13, 0)
    ex.step()
    assert len(places(broker)) == 1 + (config.LIVE_SELL_ROUNDS - 1)   # 下午开盘接着卖，轮数接着数


# ---- 14:57 之后不撤单 / 收盘结算 ----

def test_after_no_cancel_time_sell_stays_submitted_and_is_not_placed_again():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 14, 58))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    ex = make(c, broker, clock=clock)
    ex.step()

    assert len(places(broker)) == 1
    assert ("cancel_last",) not in broker.calls
    assert sell_orders(c)[0]["status"] == "submitted"

    while clock.now() < datetime(2026, 9, 29, 14, 59, 50):   # 下一分钟：只跟踪，不重下、不撤
        clock.sleep(1)
        ex.step()
    assert len(places(broker)) == 1
    assert ("cancel_last",) not in broker.calls
    assert sell_orders(c)[0]["status"] == "submitted"
    assert live_store.paused(c) is None


def test_no_cancel_decision_is_made_after_the_wait_not_before():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 14, 56, 50))     # 下单时还没到 14:57，等完 20 秒就过了
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    make(c, broker, clock=clock).step()

    assert len(places(broker)) == 1
    assert ("cancel_last",) not in broker.calls
    assert sell_orders(c)[0]["status"] == "submitted"


def test_close_settle_books_partial_fill_and_finalizes_row():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 14, 58))
    setup_ready(c, clock)
    hold(c, shares=500)
    remember_name(c, clock.now())
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    notifier = FakeNotifier()
    ex = make(c, broker, notifier, clock=clock)
    ex.step()
    assert sell_orders(c)[0]["status"] == "submitted"

    broker.fill_mode, broker.fill_ratio = "partial", 0.5      # 集合竞价成交了一部分
    clock.t = datetime(2026, 9, 29, 15, 1)
    ex.step()

    order = sell_orders(c)[0]
    assert order["status"] == "partial" and order["filled"] == 200
    assert "收盘未成交部分作废" in order["note"]
    assert live_store.positions(c)["600001"].shares == 300
    assert any("卖出" in title and "甲公司" in text and "盈亏" in text for title, text in notifier.sent)
    assert live_store.get_state(c, "sell_settle_day") == "2026-09-29"

    reads = broker.calls.count(("orders_today",))
    clock.sleep(1)
    ex.step()                                                  # 今天已结算：不再读
    assert broker.calls.count(("orders_today",)) == reads
    assert len(places(broker)) == 1


def test_close_settle_without_any_fill_marks_cancelled():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 14, 58))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    ex = make(c, broker, clock=clock)
    ex.step()
    clock.t = datetime(2026, 9, 29, 15, 1)
    ex.step()

    order = sell_orders(c)[0]
    assert order["status"] == "cancelled" and order["filled"] == 0
    assert live_store.positions(c)["600001"].shares == 500


def test_close_settle_read_failure_retries_next_second():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 14, 58))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    ex = make(c, broker, clock=clock)
    ex.step()
    clock.t = datetime(2026, 9, 29, 15, 1)
    broker.fail_reads = 1
    ex.step()
    assert sell_orders(c)[0]["status"] == "submitted"
    assert live_store.get_state(c, "sell_settle_day") is None

    clock.sleep(1)
    ex.step()
    assert sell_orders(c)[0]["status"] == "cancelled"
    assert live_store.get_state(c, "sell_settle_day") == "2026-09-29"


def test_close_settle_order_not_found_keeps_submitted_and_auto_pauses():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 15, 1))
    setup_ready(c, clock)
    hold(c, shares=500)
    oid = live_store.add_order(c, None, datetime(2026, 9, 29, 14, 58), "sell", "600001", 9.9, 500, "submitted")
    live_store.update_order(c, oid, clock.now(), entrust_no="999")
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    make(c, broker, clock=clock).step()

    order = sell_orders(c)[0]
    assert order["status"] == "submitted" and "收盘结算找不到这笔" in order["note"]
    kind, reason = paused_reason(c)
    assert kind == "auto" and "收盘结算找不到卖单" in reason
    assert live_store.positions(c)["600001"].shares == 500


# ---- 隔夜 / 午休 / remote ----

def test_overnight_position_is_not_sold_at_0925_but_sold_at_0930():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 25))
    setup_ready(c, clock)
    hold(c, shares=500, buy_date=YESTERDAY)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    ex = make(c, broker, clock=clock)
    ex.step()
    assert broker.calls == []

    clock.t = datetime(2026, 9, 29, 9, 30)
    ex.step()
    assert len(places(broker)) == 1
    assert live_store.positions(c) == {}


def test_no_sell_during_lunch_break():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 12, 0))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    make(c, broker, clock=clock).step()
    assert broker.calls == []


def test_remote_sell_intent_sells_position_sim_still_holds_and_stays_new():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    sim_holds(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    ex = make(c, broker, clock=clock)
    ex.step()
    assert places(broker) == []              # 模拟还持有、也没有手机指令：不卖

    live_store.add_remote_sell(c, clock.now())
    clock.sleep(1)
    ex.step()
    assert len(places(broker)) == 1
    assert live_store.positions(c) == {}
    assert c.execute("SELECT status FROM live_intents WHERE source='remote'").fetchone() == ("new",)


# ---- 演练 ----

def test_dryrun_sell_never_places_and_reduces_position_even_if_client_has_none():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock, mode="dryrun")
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={})       # 演练的持仓客户端里本来就没有
    notifier = FakeNotifier()
    make(c, broker, notifier, clock=clock).step()

    assert broker.calls == []
    order = sell_orders(c)[0]
    assert order["status"] == "dryrun" and order["shares"] == 500 and order["price"] == 9.9
    assert live_store.positions(c) == {}


# ---- 下单异常 ----

def test_broker_reject_marks_failed_without_pause_and_waits_a_minute():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.place_error = BrokerError("可用股份不足", "INSUFFICIENT_SHARES")
    notifier = FakeNotifier()
    ex = make(c, broker, notifier, clock=clock)
    ex.step()

    order = sell_orders(c)[0]
    assert order["status"] == "failed" and "可用股份不足" in order["note"]
    assert live_store.paused(c) is None
    assert len(texts(notifier, "拒单")) == 1

    for _ in range(59):
        clock.sleep(1)
        ex.step()
    assert len(places(broker)) == 1

    clock.sleep(1)                                 # 一分钟后重试（这次成功了）
    ex.step()
    assert len(places(broker)) == 2
    assert live_store.positions(c) == {}


def test_inflight_place_error_keeps_sending_and_auto_pauses_and_never_places_again():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.place_error = BrokerError("网关超时", "TIMEOUT")
    ex = make(c, broker, clock=clock)
    ex.step()

    assert sell_orders(c)[0]["status"] == "sending"
    kind, reason = paused_reason(c)
    assert kind == "auto" and "卖出结果不明" in reason

    clock.sleep(1)
    ex.step()
    assert len(places(broker)) == 1

    live_store.clear_pause(c)          # 就算人为恢复了，sending 没核对完之前也不能再下一单
    clock.sleep(61)
    ex.step()
    assert len(places(broker)) == 1
    assert sell_orders(c)[0]["status"] == "sending"


def test_unknown_place_error_but_order_found_continues_with_that_order_only():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    orig_place = broker.place

    def place_then_raise(*a):
        orig_place(*a)
        raise RuntimeError("adapter blew up after submit")

    broker.place = place_then_raise
    make(c, broker, clock=clock).step()

    assert len(places(broker)) == 1
    order = sell_orders(c)[0]
    assert order["status"] == "filled" and order["entrust_no"] == "1"
    assert live_store.positions(c) == {}
    assert live_store.paused(c) is None


def test_unknown_place_error_order_not_found_marks_failed_and_auto_pauses():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.place_error = BrokerError("网关异常", "GATEWAY_ERROR")
    ex = make(c, broker, clock=clock)
    ex.step()

    assert sell_orders(c)[0]["status"] == "failed"
    kind, reason = paused_reason(c)
    assert kind == "auto" and "下单层返回认不出的错误" in reason
    clock.sleep(1)
    ex.step()
    assert len(places(broker)) == 1


def test_unknown_place_error_and_read_failure_auto_pauses_and_keeps_sending():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.place_error = BrokerError("网关异常", "GATEWAY_ERROR")

    def boom():
        raise BrokerError("读取失败", "CONN_ERROR")

    broker.orders_today = boom
    ex = make(c, broker, clock=clock)
    ex.step()

    assert sell_orders(c)[0]["status"] == "sending"
    kind, reason = paused_reason(c)
    assert kind == "auto" and "卖出结果不明" in reason

    live_store.clear_pause(c)
    clock.sleep(61)
    ex.step()
    assert len(places(broker)) == 1


# ---- 暂停 / 行情 / 读失败 ----

def test_paused_makes_no_broker_calls_and_leaves_intents_untouched():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    add_sim_sell(c, clock.now())
    live_store.add_intents(c, [Fill(config.LIVE_ACCOUNT, "600002", "buy", 10.0, 500, 5.0, clock.now(), "测试")],
                           {"600002": "乙公司"})
    live_store.pause(c, clock.now() + timedelta(minutes=30), "手机暂停", "manual")
    broker = FakeBroker(clock, cash=50_000, holdings={"600001": [500, 500]})
    make(c, broker, clock=clock).step()

    assert broker.calls == []
    assert {s for (s,) in c.execute("SELECT status FROM live_intents")} == {"new"}
    assert live_store.positions(c)["600001"].shares == 500


def test_suspended_or_zero_price_quote_places_nothing_and_notifies_once():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    notifier = FakeNotifier()
    halted = replace(quote(code="600001", pct=0.0), price=0.0, limit_down=0.0)
    ex = make(c, broker, notifier, clock=clock, quotes=lambda codes: {"600001": halted})
    ex.step()
    assert places(broker) == []
    assert len(texts(notifier, "取不到有效行情")) == 1

    clock.sleep(61)
    ex.step()
    assert places(broker) == []
    assert len(texts(notifier, "取不到有效行情")) == 1


def test_quote_failure_places_nothing():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    notifier = FakeNotifier()

    def boom(codes):
        raise RuntimeError("行情接口挂了")

    make(c, broker, notifier, clock=clock, quotes=boom).step()
    assert places(broker) == []
    assert len(texts(notifier, "取不到有效行情")) == 1


def test_client_position_read_failure_defers_without_placing():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fail_reads = 1
    ex = make(c, broker, clock=clock)
    ex.step()
    assert places(broker) == []

    clock.sleep(1)
    ex.step()                                        # 还在等下次重试：不读
    assert broker.calls.count(("positions",)) == 1

    clock.sleep(config.LIVE_SELL_RETRY)
    ex.step()
    assert len(places(broker)) == 1


# ---- 已挂着的程序卖单 ----

def test_open_sell_row_missing_from_todays_orders_auto_pauses_and_never_places():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 40))
    setup_ready(c, clock)
    hold(c, shares=500)
    oid = live_store.add_order(c, None, datetime(2026, 9, 29, 9, 35), "sell", "600001", 9.9, 500, "submitted")
    live_store.update_order(c, oid, clock.now(), entrust_no="999")
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    ex = make(c, broker, clock=clock)
    ex.step()

    order = sell_orders(c)[0]
    assert order["status"] == "submitted" and "当日委托里找不到" in order["note"]
    kind, reason = paused_reason(c)
    assert kind == "auto" and "卖单核对不上" in reason
    assert places(broker) == []
    clock.sleep(1)
    ex.step()
    assert places(broker) == []


def test_open_sell_row_read_failure_defers_without_placing():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 40))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    entrust_no = broker.place("sell", "600001", 9.9, 500)
    oid = live_store.add_order(c, None, datetime(2026, 9, 29, 9, 35), "sell", "600001", 9.9, 500, "submitted")
    live_store.update_order(c, oid, clock.now(), entrust_no=entrust_no)
    broker.calls.clear()
    broker.fail_reads = 1
    ex = make(c, broker, clock=clock)
    ex.step()

    assert places(broker) == []
    assert sell_orders(c)[0]["status"] == "submitted"
    clock.sleep(1)
    ex.step()
    assert broker.calls.count(("orders_today",)) == 1    # 等下次重试，不每秒读


def test_closed_sell_row_found_while_tracking_settles_then_sells_the_remainder():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 40))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode, broker.fill_ratio = "partial", 0.5
    entrust_no = broker.place("sell", "600001", 9.9, 500)     # 成了 200
    broker.cancel_last()                                       # 之后被撤（部撤）
    oid = live_store.add_order(c, None, datetime(2026, 9, 29, 9, 35), "sell", "600001", 9.9, 500, "submitted")
    live_store.update_order(c, oid, clock.now(), entrust_no=entrust_no)
    broker.calls.clear()
    broker.fill_mode = "all"
    make(c, broker, clock=clock).step()

    first = sell_orders(c)[0]
    assert first["status"] == "partial" and first["filled"] == 200
    assert [call[4] for call in places(broker)] == [300]       # 剩下的 300 接着卖
    assert live_store.positions(c) == {}


# ---- 启动核对 ----

def test_startup_leaves_open_sell_uncancelled_and_books_fill_delta():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 40))
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode, broker.fill_ratio = "partial", 0.5
    entrust_no = broker.place("sell", "600001", 9.9, 500)     # 客户端里挂着，成了 200，还 open
    oid = live_store.add_order(c, None, datetime(2026, 9, 29, 9, 35), "sell", "600001", 9.9, 500, "submitted")
    live_store.update_order(c, oid, clock.now(), entrust_no=entrust_no)
    make(c, broker, clock=clock).startup()

    assert ("cancel_last",) not in broker.calls
    order = sell_orders(c)[0]
    assert order["status"] == "submitted" and order["filled"] == 200
    assert live_store.positions(c)["600001"].shares == 300
    assert live_store.paused(c) is None


def test_startup_sending_sell_found_open_becomes_submitted_and_is_not_cancelled():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 40))
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    broker.place("sell", "600001", 9.9, 500)
    live_store.add_order(c, None, datetime(2026, 9, 29, 9, 39), "sell", "600001", 9.9, 500, "sending")
    make(c, broker, clock=clock).startup()

    order = sell_orders(c)[0]
    assert order["status"] == "submitted" and order["entrust_no"] == "1"
    assert ("cancel_last",) not in broker.calls


def test_startup_closed_sell_books_final_fill_and_status():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 40))
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    entrust_no = broker.place("sell", "600001", 9.9, 500)     # 全部成交
    oid = live_store.add_order(c, None, datetime(2026, 9, 29, 9, 35), "sell", "600001", 9.9, 500, "submitted")
    live_store.update_order(c, oid, clock.now(), entrust_no=entrust_no)
    make(c, broker, clock=clock).startup()

    order = sell_orders(c)[0]
    assert order["status"] == "filled" and order["filled"] == 500
    assert live_store.positions(c) == {}


# ---- 跨天 ----

def test_sell_rounds_and_retry_times_reset_on_a_new_day():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    hold(c, shares=500)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    notifier = FakeNotifier()
    ex = make(c, broker, notifier, clock=clock)
    ex.step()
    assert len(places(broker)) == config.LIVE_SELL_ROUNDS

    clock.t = datetime(2026, 9, 30, 9, 35)
    live_store.set_state(c, "trading_day", "2026-09-30")
    ex.step()
    assert len(places(broker)) == 2 * config.LIVE_SELL_ROUNDS   # 轮数清零：又是完整的 3 轮
    assert len(texts(notifier, "每分钟重试")) == 2               # 通知按天各发一次
