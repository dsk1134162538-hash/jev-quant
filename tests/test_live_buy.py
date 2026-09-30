"""Executor 骨架 + 买入路径 + sending 恢复：tests/live_fakes.py 的 FakeBroker/FakeClock/FakeNotifier
注入，全程不联网、不真等（时钟靠 FakeClock.sleep 推进）。"""
from dataclasses import replace
from datetime import date, datetime, timedelta

from factories import quote
from jevq import config, db
from jevq.ledger import affordable_shares
from jevq.live import store as live_store
from jevq.live.broker import BrokerError, OrderRow
from jevq.live.executor import Executor
from jevq.live.store import LivePosition
from jevq.models import Fill
from live_fakes import FakeBroker, FakeClock, FakeNotifier

TODAY = date(2026, 9, 29)


def conn():
    return db.connect(":memory:")


def make(c, broker, notifier=None, clock=None, quotes=None):
    return Executor(c, broker, notifier or FakeNotifier(), clock=clock.now, sleep=clock.sleep,
                    quotes=quotes or (lambda codes: {}))


def setup_ready(c, clock, mode="live"):
    live_store.set_mode(c, mode)
    live_store.set_state(c, "trading_day", clock.now().date().isoformat())


def add_intent(c, now, code="600001", price=10.0, shares=500, name="甲公司"):
    live_store.add_intents(c, [Fill(config.LIVE_ACCOUNT, code, "buy", price, shares, 5.0, now, "测试")],
                           {code: name})


def only_intent(c):
    return c.execute("SELECT status, note FROM live_intents").fetchone()


def only_order(c):
    return live_store.orders_on(c, TODAY)[0]


BUDGET = min(50_000, config.LIVE_BUDGET) * (1 - config.LIVE_CASH_BUFFER)


# ---- 心跳 / 模式 / 交易日 ----

def test_mode_off_skips_new_intents_without_broker_calls():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    add_intent(c, clock.now())
    broker = FakeBroker(clock, cash=50_000)
    make(c, broker, clock=clock).step()
    assert only_intent(c) == ("skipped", "模式关闭")
    assert broker.calls == []
    assert live_store.get_state(c, "heartbeat") == clock.now().isoformat()


def test_non_trading_day_skips_new_intents_without_broker_calls():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 31))
    live_store.set_mode(c, "live")   # trading_day 没写 → 非今天
    add_intent(c, clock.now())
    broker = FakeBroker(clock, cash=50_000)
    make(c, broker, clock=clock).step()
    assert only_intent(c) == ("skipped", "非交易日")
    assert broker.calls == []


# ---- 买入跳过原因 ----

def test_buy_expired_after_ttl_marked_expired():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    old_ts = clock.now() - timedelta(seconds=61)
    live_store.add_intents(c, [Fill(config.LIVE_ACCOUNT, "600001", "buy", 10.0, 500, 5.0, old_ts, "测试")],
                           {"600001": "甲"})
    make(c, FakeBroker(clock, cash=50_000), clock=clock).step()
    assert only_intent(c)[0] == "expired"


def test_buy_outside_buy_window_skipped():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 11, 31))   # 午间休市
    setup_ready(c, clock)
    add_intent(c, clock.now())
    make(c, FakeBroker(clock, cash=50_000), clock=clock).step()
    assert only_intent(c) == ("skipped", "不在买入时段")


def test_buy_skipped_when_program_already_holds_a_position():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    live_store.put_position(c, LivePosition("600002", 100, 1000.0, 10.0, date(2026, 9, 28), 10.0))
    add_intent(c, clock.now(), code="600001")
    broker = FakeBroker(clock, cash=50_000)
    make(c, broker, clock=clock).step()
    assert only_intent(c) == ("skipped", "程序已有持仓")
    assert broker.calls == []


def test_buy_skipped_when_client_holds_it_but_program_does_not():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001")
    broker = FakeBroker(clock, cash=50_000, holdings={"600001": [500, 500]})
    make(c, broker, clock=clock).step()
    assert only_intent(c) == ("skipped", "你持有这只")


def test_buy_skipped_when_daily_buy_limit_reached():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    for _ in range(config.LIVE_MAX_BUYS_PER_DAY):
        live_store.add_order(c, None, clock.now(), "buy", "600009", 10.0, 100, "submitted")
    add_intent(c, clock.now(), code="600001")
    make(c, FakeBroker(clock, cash=50_000), clock=clock).step()
    assert only_intent(c) == ("skipped", "今日买入已达上限")


def test_buy_skipped_for_non_main_board_code():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="300750")
    make(c, FakeBroker(clock, cash=50_000), clock=clock).step()
    assert only_intent(c) == ("skipped", "非主板")


# ---- 正常买入 ----

def test_buy_normal_fill_records_position_order_and_notifies():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.00, shares=500, name="甲公司")
    broker = FakeBroker(clock, cash=50_000)
    notifier = FakeNotifier()
    make(c, broker, notifier, clock=clock).step()

    price = 10.05
    expected_shares = affordable_shares(BUDGET, price)
    assert ("place", "buy", "600001", price, expected_shares) in broker.calls

    pos = live_store.positions(c)["600001"]
    assert pos.shares == expected_shares and pos.buy_price == price

    assert only_order(c)["status"] == "filled"
    assert only_intent(c)[0] == "done"
    assert any("买入" in title or "买入" in text for title, text in notifier.sent)


def test_buy_price_capped_at_limit_up():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.96, shares=500)   # ×1.005 后超过涨停价
    broker = FakeBroker(clock, cash=50_000)
    q = quote(code="600001", pre_close=10.0)   # limit_up = 11.0
    make(c, broker, clock=clock, quotes=lambda codes: {"600001": q}).step()
    place_call = next(call for call in broker.calls if call[0] == "place")
    assert place_call[3] == q.limit_up


def test_buy_partial_fill_cancels_after_wait_and_records_partial_shares():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    broker = FakeBroker(clock, cash=50_000)
    broker.fill_mode, broker.fill_ratio = "partial", 0.5
    t0 = clock.now()
    make(c, broker, clock=clock).step()

    assert clock.now() >= t0 + timedelta(seconds=config.LIVE_BUY_WAIT)
    assert ("cancel_last",) in broker.calls
    assert only_order(c)["status"] == "partial"

    price = 10.05
    full = affordable_shares(BUDGET, price)
    expected_partial = min(int(full * 0.5) // 100 * 100, full)
    assert live_store.positions(c)["600001"].shares == expected_partial


def test_buy_no_fill_cancels_and_notifies_no_position():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500, name="甲公司")
    broker = FakeBroker(clock, cash=50_000)
    broker.fill_mode = "none"
    notifier = FakeNotifier()
    make(c, broker, notifier, clock=clock).step()

    assert only_order(c)["status"] == "cancelled"
    assert live_store.positions(c) == {}
    assert any("没成交，已撤单" in text and "甲公司" in text for _, text in notifier.sent)


def test_buy_cancel_check_mismatch_does_not_cancel_and_auto_pauses():
    """repro B：撤单前核对撞上用户手动下的单 —— 不能瞎撤单，也不能因此就断定「没成交」。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    broker = FakeBroker(clock, cash=50_000)
    broker.fill_mode = "none"
    broker.extra_orders = [OrderRow("U1", "600002", "buy", 9.0, 100, 0, 0.0, "已报",
                                    clock.now().strftime("%H:%M:%S"))]
    notifier = FakeNotifier()
    make(c, broker, notifier, clock=clock).step()

    assert ("cancel_last",) not in broker.calls
    assert only_order(c)["status"] == "submitted"   # 核对不上：不能瞎猜成 cancelled
    assert live_store.positions(c) == {}
    assert not any("没成交" in text for _, text in notifier.sent)
    p = live_store.paused(c)
    assert p is not None and p["kind"] == "auto"
    assert "撤单前核对不上" in p["reason"]


# ---- Critical 1 repro：等成交/撤单阶段读失败不能瞎猜成「没成交，已撤单」----

def test_wait_fill_all_reads_after_place_failing_does_not_falsely_cancel():
    """repro A：place 成功后所有读取都失败——不能因为读不到就断定 0 成交、已撤单。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    broker = FakeBroker(clock, cash=50_000)
    broker.fill_mode = "none"
    orig_place = broker.place

    def place_then_fail_reads(*a):
        r = orig_place(*a)
        broker.fail_reads = 8
        return r

    broker.place = place_then_fail_reads
    notifier = FakeNotifier()
    make(c, broker, notifier, clock=clock).step()

    order = only_order(c)
    assert order["status"] != "cancelled"
    assert live_store.positions(c) == {}
    assert not any("没成交" in text for _, text in notifier.sent)
    p = live_store.paused(c)
    assert p is not None and p["kind"] == "auto"
    pause_pushes = [t for t, _ in notifier.sent if "自动暂停" in t]
    assert len(pause_pushes) == 1   # 达到暂停阈值后不再重复暂停/重复推送


def test_wait_fill_tail_read_failures_keep_order_submitted():
    """repro A2：只有撤单前核对和最终确认这两次读取失败——同样不能断定已撤单。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    broker = FakeBroker(clock, cash=50_000)
    broker.fill_mode = "none"
    orig = broker.orders_today
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 6:
            raise BrokerError("读取失败", "FAKE")
        return orig()

    broker.orders_today = flaky
    notifier = FakeNotifier()
    make(c, broker, notifier, clock=clock).step()

    order = only_order(c)
    assert order["status"] == "submitted"
    assert not any("没成交" in text for _, text in notifier.sent)
    assert live_store.paused(c) is not None


# ---- 下单异常 ----

def test_buy_broker_reject_marks_order_failed_without_pause():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    broker = FakeBroker(clock, cash=50_000)
    broker.place_error = BrokerError("可用资金不足", "INSUFFICIENT_BALANCE")
    make(c, broker, clock=clock).step()

    assert only_order(c)["status"] == "failed"
    assert live_store.paused(c) is None
    status, note = only_intent(c)
    assert status == "done" and "拒单" in note


def test_buy_unknown_place_error_not_found_in_todays_orders_marks_failed_and_auto_pauses():
    """Important 3（controller ruling）：单笔核对找不到这笔委托，除了 failed 还要自动暂停。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    broker = FakeBroker(clock, cash=50_000)
    broker.place_error = BrokerError("网关异常", "GATEWAY_ERROR")
    make(c, broker, clock=clock).step()

    assert only_order(c)["status"] == "failed"
    p = live_store.paused(c)
    assert p is not None and p["kind"] == "auto"


def test_place_raising_plain_exception_does_not_place_twice():
    """repro C：place() 抛出的不是 BrokerError（适配器本身炸了），也不能让同一意图重新下单。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    broker = FakeBroker(clock, cash=50_000)
    broker.fill_mode = "none"
    orig_place = broker.place
    first = {"v": True}

    def place_boom(*a):
        r = orig_place(*a)
        if first["v"]:
            first["v"] = False
            raise RuntimeError("adapter blew up after submit")
        return r

    broker.place = place_boom
    ex = make(c, broker, clock=clock)
    ex.step()          # 不应该向外抛异常
    clock.sleep(1)
    ex.step()

    places = [call for call in broker.calls if call[0] == "place"]
    assert len(places) == 1
    assert only_intent(c)[0] == "done"


def test_orders_today_raising_plain_exception_during_wait_does_not_place_twice():
    """repro D：orders_today() 中途抛出非 BrokerError（比如适配器解析报表崩了），同样不能重下。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    broker = FakeBroker(clock, cash=50_000)
    broker.fill_mode = "none"
    orig = broker.orders_today
    first = {"v": True}

    def ot_boom():
        if first["v"]:
            first["v"] = False
            raise ValueError("could not convert string to float: '--'")
        return orig()

    broker.orders_today = ot_boom
    ex = make(c, broker, clock=clock)
    ex.step()          # 不应该向外抛异常
    clock.sleep(1)
    ex.step()

    places = [call for call in broker.calls if call[0] == "place"]
    assert len(places) == 1


def test_buy_skips_intent_that_already_has_an_order_row():
    """兜底：万一意图还是 new，但已经有 live_orders 行挂在它名下，绝不能再下一次。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    iid = c.execute("SELECT id FROM live_intents").fetchone()[0]
    live_store.add_order(c, iid, clock.now(), "buy", "600001", 10.05, 500, "sending")
    broker = FakeBroker(clock, cash=50_000)
    make(c, broker, clock=clock).step()

    assert all(call[0] != "place" for call in broker.calls)
    assert only_intent(c)[0] == "done"


def test_buy_unknown_place_error_and_read_failure_auto_pauses():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    broker = FakeBroker(clock, cash=50_000)
    broker.place_error = BrokerError("网关异常", "GATEWAY_ERROR")

    def boom():
        raise BrokerError("读取失败", "CONN_ERROR")

    broker.orders_today = boom
    make(c, broker, clock=clock).step()

    p = live_store.paused(c)
    assert p is not None and p["kind"] == "auto" and "下单结果不明" in p["reason"]


# ---- 演练 ----

def test_buy_dryrun_never_calls_place_and_records_position_at_sim_price():
    """controller ruling：演练持仓按模拟成交价记入 live_positions，不是限价。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock, mode="dryrun")
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    broker = FakeBroker(clock, cash=50_000)
    make(c, broker, clock=clock).step()

    assert all(call[0] != "place" for call in broker.calls)
    assert only_order(c)["status"] == "dryrun"
    limit_price = 10.05
    expected_shares = affordable_shares(BUDGET, limit_price)
    pos = live_store.positions(c)["600001"]
    assert pos.shares == expected_shares
    assert pos.buy_price == 10.0   # 模拟成交价，不是限价 10.05
    assert only_intent(c) == ("done", "演练")


# ---- startup：sending 恢复 ----

def test_startup_sending_matches_fully_filled_updates_order_and_position():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    order_ts = datetime(2026, 9, 29, 9, 34, 0)
    live_store.add_order(c, None, order_ts, "buy", "600001", 10.05, 500, "sending")
    broker = FakeBroker(clock, cash=50_000)
    broker.place("buy", "600001", 10.05, 500)   # 客户端里真的有这笔，且已经全部成交
    make(c, broker, clock=clock).startup()

    order = only_order(c)
    assert order["status"] == "filled" and order["filled"] == 500
    assert live_store.positions(c)["600001"].shares == 500


def test_startup_sending_no_match_marks_failed_and_auto_pauses():
    """Important 3（controller ruling）：启动核对没找到，除了 failed 还要自动暂停。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    order_ts = datetime(2026, 9, 29, 9, 34, 0)
    live_store.add_order(c, None, order_ts, "buy", "600001", 10.05, 500, "sending")
    broker = FakeBroker(clock, cash=50_000)   # 当日委托里没有这一笔
    make(c, broker, clock=clock).startup()

    order = only_order(c)
    assert order["status"] == "failed"
    assert "没找到" in order["note"]
    p = live_store.paused(c)
    assert p is not None and p["kind"] == "auto"


def test_startup_read_failure_auto_pauses():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    order_ts = datetime(2026, 9, 29, 9, 34, 0)
    live_store.add_order(c, None, order_ts, "buy", "600001", 10.05, 500, "sending")
    broker = FakeBroker(clock, cash=50_000)
    broker.fail_reads = 1
    make(c, broker, clock=clock).startup()

    p = live_store.paused(c)
    assert p is not None and p["kind"] == "auto" and "启动核对" in p["reason"]


def test_startup_also_reconciles_submitted_rows_not_just_sending():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    order_ts = datetime(2026, 9, 29, 9, 34, 0)
    live_store.add_order(c, None, order_ts, "buy", "600001", 10.05, 500, "submitted")
    broker = FakeBroker(clock, cash=50_000)
    broker.place("buy", "600001", 10.05, 500)   # 客户端里真的有这笔，且已经全部成交
    make(c, broker, clock=clock).startup()

    order = only_order(c)
    assert order["status"] == "filled" and order["filled"] == 500
    assert live_store.positions(c)["600001"].shares == 500


def test_startup_reconciles_row_and_finishes_intent_preventing_rebuy():
    """repro E：进程在 place() 成功之后、finish_intent 之前崩了；重启后 startup 核对这一行，
    必须顺手把意图标 done，不然下一秒 step() 会对同一个意图再下一次单。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    iid = c.execute("SELECT id FROM live_intents").fetchone()[0]
    broker = FakeBroker(clock, cash=50_000)
    broker.fill_mode = "none"
    live_store.add_order(c, iid, clock.now(), "buy", "600001", 10.05, 1900, "sending")
    broker.place("buy", "600001", 10.05, 1900)   # 模拟真实下单已经发生，只是进程写库前先崩了
    clock.sleep(5)

    ex2 = make(c, broker, clock=clock)
    ex2.startup()
    ex2.step()

    places = [call for call in broker.calls if call[0] == "place"]
    assert len(places) == 1
    assert only_intent(c)[0] == "done"


# ---- Important 4：撤单前核对的身份匹配，有委托号只认委托号 ----

def test_cancel_check_prefers_entrust_no_over_coincidental_field_match():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    broker = FakeBroker(clock, cash=50_000)
    broker.fill_mode = "none"
    # 用户手动下的单：代码/方向/价格/数量和程序这笔完全一样，但委托号不同——不能靠四要素误判成自己的单。
    broker.extra_orders = [OrderRow("USER1", "600001", "buy", 10.05, 500, 0, 0.0, "已报",
                                    clock.now().strftime("%H:%M:%S"))]
    notifier = FakeNotifier()
    make(c, broker, notifier, clock=clock).step()

    assert ("cancel_last",) not in broker.calls
    assert only_order(c)["status"] == "submitted"
    p = live_store.paused(c)
    assert p is not None and p["kind"] == "auto"


# ---- Minor：涨停价读到非正数当无效处理 ----

def test_buy_price_ignores_non_positive_limit_up():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_intent(c, clock.now(), code="600001", price=10.0, shares=500)
    broker = FakeBroker(clock, cash=50_000)
    bad_quote = replace(quote(code="600001", pre_close=10.0), limit_up=0.0)
    make(c, broker, clock=clock, quotes=lambda codes: {"600001": bad_quote}).step()
    place_call = next(call for call in broker.calls if call[0] == "place")
    assert place_call[3] == 10.05   # 涨停价无效：只用前半（不封顶）


# ---- 连续读取失败自动暂停 ----

def test_read_auto_pauses_after_consecutive_failures():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    ex = make(c, FakeBroker(clock, cash=50_000), clock=clock)

    def boom():
        raise BrokerError("网络错误", "CONN_ERROR")

    for _ in range(config.LIVE_READ_FAIL_PAUSE - 1):
        try:
            ex._read(boom)
        except BrokerError:
            pass
        assert live_store.paused(c) is None

    try:
        ex._read(boom)
    except BrokerError:
        pass
    p = live_store.paused(c)
    assert p is not None and p["kind"] == "auto" and "客户端可能掉线" in p["reason"]
