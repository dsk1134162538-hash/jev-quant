"""终审（docs/plans/2026-09-29-live-trading-final-review.md）执行器部分的回归测试：C-1、I-2、I-4、I-5、
待确认 2/3。时钟全部注入；「慢网关」按研究文件实测耗时推进假时钟。"""
from datetime import date, datetime, timedelta

import pytest

from factories import quote
from jevq import config, db
from jevq.live import store as live_store
from jevq.live.broker import OrderRow
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
    ex = Executor(c, broker, notifier or FakeNotifier(), clock=clock.now, sleep=clock.sleep,
                  quotes=quotes or quotes_fn())
    ex._last_reconcile = clock.now()
    return ex


def setup_ready(c, clock, mode="live"):
    live_store.set_mode(c, mode)
    live_store.set_state(c, "trading_day", clock.now().date().isoformat())
    live_store.set_state(c, "self_check_day", clock.now().date().isoformat())


def add_buy_intent(c, now, code="600001", price=10.0, name="甲公司"):
    live_store.add_intents(c, [Fill(config.LIVE_ACCOUNT, code, "buy", price, 500, 5.0, now, "测试")], {code: name})


def hold(c, code="600001", shares=500, buy_price=10.0, buy_date=YESTERDAY):
    live_store.put_position(c, LivePosition(code, shares, shares * buy_price, buy_price, buy_date, buy_price))


def slow_gateway(broker, clock):
    """按网关实测耗时推进假时钟：持仓 6.4 秒、资金 2.2 秒、当日委托 5.8 秒、下单 6.5 秒、撤单 3 秒。
    返回撤单时刻的记录表。"""
    cancel_at = []

    def slow(fn, seconds, record=None):
        def call(*a):
            clock.sleep(seconds)
            if record is not None:
                record.append(clock.now())
            return fn(*a)
        return call

    broker.positions = slow(broker.positions, 6.4)
    broker.balance = slow(broker.balance, 2.2)
    broker.orders_today = slow(broker.orders_today, 5.8)
    broker.place = slow(broker.place, 6.5)
    broker.cancel_last = slow(broker.cancel_last, 3, cancel_at)
    return cancel_at


def rows(c, day=TODAY):
    return live_store.orders_on(c, day)


# ---- C-1：临近 11:30 / 14:57 的买单，撤单一定要在连续竞价里发出 ----

@pytest.mark.parametrize("at", [datetime(2026, 9, 29, 14, 55, 45), datetime(2026, 9, 29, 14, 56, 0),
                                datetime(2026, 9, 29, 14, 56, 5), datetime(2026, 9, 29, 14, 56, 9),
                                datetime(2026, 9, 29, 11, 28, 52), datetime(2026, 9, 29, 11, 29, 5)])
def test_late_buy_is_either_cancelled_in_time_or_skipped_never_left_hanging(at):
    c = conn()
    clock = FakeClock(at)
    setup_ready(c, clock)
    add_buy_intent(c, clock.now())
    broker = FakeBroker(clock, cash=50_000)
    broker.fill_mode = "none"
    cancel_at = slow_gateway(broker, clock)
    make(c, broker, clock=clock).step()

    assert live_store.paused(c) is None
    orders = rows(c)
    if orders:
        assert orders[0]["status"] == "cancelled"
        end = datetime.combine(TODAY, config.LIVE_CANCEL_WINDOWS[0][1] if at.hour == 11
                               else config.LIVE_CANCEL_WINDOWS[1][1])
        assert cancel_at and cancel_at[0] < end - timedelta(seconds=5)
    else:
        status, note = c.execute("SELECT status, note FROM live_intents").fetchone()
        assert status == "skipped" and "来不及" in note


def test_open_buy_after_close_is_booked_at_its_final_fill_without_cancelling():
    """万一买单还是挂进了收盘集合竞价：15:01 后按最终成交落账（和卖单一样），不再撤、不再暂停。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 14, 56, 50))
    setup_ready(c, clock)
    broker = FakeBroker(clock, cash=50_000)
    broker.fill_mode = "partial"
    broker.fill_ratio = 0.6
    iid = c.execute("INSERT INTO live_intents(ts, source, side, code, name, sim_price, sim_shares, reason, status) "
                    "VALUES (?, 'sim', 'buy', '600001', '甲公司', 10.0, 500, 'r', 'done')",
                    (clock.now().isoformat(),)).lastrowid
    no = broker.place("buy", "600001", 10.05, 500)
    oid = live_store.add_order(c, iid, clock.now(), "buy", "600001", 10.05, 500, "submitted")
    live_store.update_order(c, oid, clock.now(), entrust_no=no)
    clock.t = datetime(2026, 9, 29, 15, 1)
    make(c, broker, clock=clock).step()

    assert ("cancel_last",) not in broker.calls
    row = rows(c)[0]
    assert row["status"] == "partial" and row["filled"] == 300
    assert live_store.positions(c)["600001"].shares == 300
    assert live_store.paused(c) is None


def test_resume_after_close_books_open_buy_instead_of_pausing_again():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 14, 56, 50))
    setup_ready(c, clock)
    broker = FakeBroker(clock, cash=50_000)
    broker.fill_mode = "partial"   # 收盘后读到的还是「部成」（open）：成交数已是最终值
    broker.fill_ratio = 0.6
    no = broker.place("buy", "600001", 10.05, 500)
    oid = live_store.add_order(c, None, clock.now(), "buy", "600001", 10.05, 500, "submitted")
    live_store.update_order(c, oid, clock.now(), entrust_no=no)
    live_store.pause(c, None, "来不及撤单：600001 这笔委托还挂着", "auto")
    clock.t = datetime(2026, 9, 29, 15, 30)
    ex = make(c, broker, clock=clock)
    live_store.set_state(c, "resume_requested", live_store.paused(c)["reason"])
    ex.step()

    assert live_store.paused(c) is None
    assert ("cancel_last",) not in broker.calls
    assert rows(c)[0]["status"] == "partial"
    assert live_store.positions(c)["600001"].shares == 300


# ---- I-2：前几天没核对完的委托不能静默丢掉 ----

def test_startup_flags_earlier_day_unresolved_orders_loudly_and_pauses_once():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 20))
    setup_ready(c, clock)
    live_store.add_order(c, None, datetime(2026, 9, 28, 14, 52), "buy", "600001", 10.05, 500, "submitted")
    broker = FakeBroker(clock, cash=50_000)
    notifier = FakeNotifier()
    ex = make(c, broker, notifier, clock=clock)
    ex.startup()

    p = live_store.paused(c)
    assert p["kind"] == "auto" and "600001" in p["reason"] and "程序不会卖" in p["reason"]
    assert live_store.orders_on(c, YESTERDAY)[0]["status"] == "unknown"

    live_store.set_state(c, "resume_requested", p["reason"])   # 用户在 App 里处理好了，再点恢复：不会再被同一笔拦住
    ex.step()
    assert live_store.paused(c) is None


# ---- I-4：离开演练就清掉演练持仓；有真实持仓不许进演练 ----

def test_leaving_dryrun_for_off_clears_rehearsal_positions():
    c = conn()
    live_store.set_mode(c, "dryrun")
    hold(c)
    live_store.set_state(c, "reduce:600001", '{"shares": 0, "ts": "2026-10-12T10:00:00"}')
    live_store.set_mode(c, "off")
    assert live_store.positions(c) == {}
    assert live_store.get_state(c, "reduce:600001") is None


def test_entering_dryrun_with_real_positions_is_refused():
    c = conn()
    live_store.set_mode(c, "live")
    hold(c)
    with pytest.raises(ValueError):
        live_store.set_mode(c, "dryrun")
    live_store.set_mode(c, "off")
    with pytest.raises(ValueError):
        live_store.set_mode(c, "dryrun")
    assert live_store.mode(c) == "off"
    assert "600001" in live_store.positions(c)


# ---- I-5：留在连续竞价之外的卖单，回到连续竞价后要撤掉按新价重挂 ----

def test_sell_left_open_over_lunch_is_repriced_in_the_afternoon():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 11, 29, 30))
    setup_ready(c, clock)
    hold(c)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    no = broker.place("sell", "600001", 9.9, 500)
    oid = live_store.add_order(c, None, clock.now(), "sell", "600001", 9.9, 500, "submitted")
    live_store.update_order(c, oid, clock.now(), entrust_no=no)
    clock.t = datetime(2026, 9, 29, 13, 0, 30)
    make(c, broker, clock=clock, quotes=quotes_fn(pct=-3.0)).step()

    assert ("cancel_last",) in broker.calls
    places = [call for call in broker.calls if call[0] == "place"]
    assert len(places) >= 2 and places[1][3] == round(9.7 * 0.99, 2)   # 上午那笔撤掉，按下午的新价重挂
    assert rows(c)[0]["status"] == "cancelled"
    assert live_store.paused(c) is None


def test_limit_down_queue_sell_is_never_cancelled_for_repricing():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 10, 0))
    setup_ready(c, clock)
    hold(c)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    ex = make(c, broker, clock=clock, quotes=quotes_fn(pct=-10.0))
    ex.step()
    assert rows(c)[0]["note"] == "跌停排队"
    clock.t = datetime(2026, 9, 29, 10, 5)
    ex.step()
    assert ("cancel_last",) not in broker.calls
    assert len([call for call in broker.calls if call[0] == "place"]) == 1


# ---- 待确认 2：当天买的持仓不按客户端减账 ----

def test_reconcile_never_reduces_a_position_bought_today():
    """客户端「股票余额」如果不含当天买入，两次读数就会把刚买的仓位当「人工卖出」减掉。T+1 当天不可能卖出，
    当天的短少只能是读数口径的问题。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 10, 0))
    setup_ready(c, clock)
    hold(c, buy_date=TODAY)
    broker = FakeBroker(clock, holdings={})
    ex = make(c, broker, clock=clock)
    ex._reconcile(clock.now())
    clock.sleep(120)
    ex._reconcile(clock.now())
    assert live_store.positions(c)["600001"].shares == 500


# ---- 待确认 3：没有委托号时的匹配 ----

def test_entrust_no_learned_from_first_match_is_written_back():
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_buy_intent(c, clock.now())
    broker = FakeBroker(clock, cash=50_000)
    orig = broker.place
    broker.place = lambda *a: (orig(*a), None)[1]   # 网关默认 capture_entrust_no=false：拿不到委托号
    make(c, broker, clock=clock).step()
    row = rows(c)[0]
    assert row["status"] == "filled" and row["entrust_no"] == "1"


def test_match_tolerates_the_pc_clock_running_ahead_of_the_broker():
    """家里电脑时钟比券商快 20 秒：委托时间比下单时刻还早，也要对得上。"""
    c = conn()
    clock = FakeClock(datetime(2026, 9, 29, 9, 35))
    setup_ready(c, clock)
    add_buy_intent(c, clock.now())
    broker = FakeBroker(clock, cash=50_000)
    orig_place, orig_orders = broker.place, broker.orders_today
    broker.place = lambda *a: (orig_place(*a), None)[1]

    def skewed():
        out = []
        for o in orig_orders():
            t = datetime.combine(TODAY, datetime.strptime(o.time, "%H:%M:%S").time()) - timedelta(seconds=20)
            out.append(OrderRow(o.entrust_no, o.code, o.side, o.price, o.shares, o.filled, o.avg_price, o.status,
                                t.strftime("%H:%M:%S"), o.cancelled))
        return out

    broker.orders_today = skewed
    make(c, broker, clock=clock).step()
    assert rows(c)[0]["status"] == "filled"
    assert live_store.paused(c) is None


@pytest.mark.parametrize("at", [datetime(2026, 9, 29, 11, 28, 30), datetime(2026, 9, 29, 11, 28, 45),
                                datetime(2026, 9, 29, 11, 29, 5), datetime(2026, 9, 29, 11, 29, 15)])
def test_late_morning_sell_with_slow_gateway_never_hangs_over_lunch(at):
    """终审 I-5 复现：正常网关耗时下，11:29 前后开始的一轮卖出要么撤单赶在午休前，要么干脆留到下午，
    不能留一笔撤不掉的旧价卖单挂过午休。"""
    c = conn()
    clock = FakeClock(at)
    setup_ready(c, clock)
    hold(c)
    broker = FakeBroker(clock, holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    cancel_at = slow_gateway(broker, clock)
    make(c, broker, clock=clock).step()

    assert live_store.paused(c) is None
    assert all(r["status"] != "submitted" for r in rows(c))
    assert all(t < datetime(2026, 9, 29, 11, 30) for t in cancel_at)
