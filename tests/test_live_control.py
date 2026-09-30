"""Executor 暂停/恢复、立即卖出、对账、自检、收盘小结（Task 5）：FakeBroker/FakeClock/FakeNotifier 注入，
全程不联网、不真等。写法照抄 test_live_sell.py。"""
import json
from datetime import date, datetime, timedelta

from factories import quote
from jevq import config, db
from jevq.live import store as live_store
from jevq.live.executor import Executor
from jevq.live.store import LivePosition
from jevq.models import Fill
from live_fakes import FakeBroker, FakeClock, FakeNotifier

TODAY = date(2026, 9, 29)
YESTERDAY = date(2026, 9, 28)
AM = datetime(2026, 9, 29, 9, 35)


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


def env(at=AM, mode="live", holdings=None, cash=50_000.0, checked=True):
    """(conn, clock, broker, notifier, executor)；checked=True 表示今天的自检已经做过（不干扰别的断言）。"""
    c = conn()
    clock = FakeClock(at)
    setup_ready(c, clock, mode)
    if checked:
        live_store.set_state(c, "self_check_day", at.date().isoformat())
    broker = FakeBroker(clock, cash=cash, holdings=holdings)
    notifier = FakeNotifier()
    return c, clock, broker, notifier, make(c, broker, notifier, clock=clock)


def hold(c, code="600001", shares=500, buy_price=10.0, buy_date=YESTERDAY):
    live_store.put_position(c, LivePosition(code, shares, shares * buy_price, buy_price, buy_date, buy_price))


def sim_holds(c, code="600001", shares=500):
    c.execute("INSERT OR REPLACE INTO positions(account, code, shares, cost, buy_price, buy_date) "
              "VALUES (?, ?, ?, ?, ?, ?)",
              (config.LIVE_ACCOUNT, code, shares, shares * 10.0, 10.0, YESTERDAY.isoformat()))
    c.commit()


def remember_name(c, now, code="600001", name="甲公司"):
    live_store.add_intents(c, [Fill(config.LIVE_ACCOUNT, code, "buy", 10.0, 500, 5.0, now, "开仓")], {code: name})
    c.execute("UPDATE live_intents SET status='done'")
    c.commit()


def add_sim(c, now, side, code="600001", name="甲公司"):
    live_store.add_intents(c, [Fill(config.LIVE_ACCOUNT, code, side, 10.0, 500, 5.0, now, "测试")], {code: name})


def places(broker):
    return [call for call in broker.calls if call[0] == "place"]


def names(broker):
    return [call[0] for call in broker.calls]


def texts(notifier, word):
    return [text for title, text in notifier.sent if word in title or word in text]


def intent_rows(c, where="1=1"):
    return c.execute(f"SELECT source, side, code, status, note FROM live_intents WHERE {where} ORDER BY id").fetchall()


def pending(c, code="600001"):
    raw = live_store.get_state(c, f"reduce:{code}")
    return None if raw is None else json.loads(raw)


def fake_order(broker, side, code, price, shares):
    """先让假券商里真有这笔委托（已成交），返回它的委托号；调用记录清空。"""
    entrust_no = broker.place(side, code, price, shares)
    broker.calls.clear()
    return entrust_no


# ---- 暂停中：零 broker 调用 ----

def test_paused_buy_intent_is_skipped_and_broker_untouched():
    c, clock, broker, notifier, ex = env()
    add_sim(c, clock.now(), "buy", "600002", "乙公司")
    live_store.pause(c, clock.now() + timedelta(minutes=30), "手机暂停", "manual")
    ex.step()
    assert intent_rows(c) == [("sim", "buy", "600002", "skipped", "暂停中")]
    assert broker.calls == []


def test_paused_sim_sell_intent_is_done_as_deferred():
    c, clock, broker, notifier, ex = env()
    hold(c)
    add_sim(c, clock.now(), "sell")
    live_store.pause(c, clock.now() + timedelta(minutes=30), "手机暂停", "manual")
    ex.step()
    assert intent_rows(c) == [("sim", "sell", "600001", "done", "暂停中延后")]
    assert live_store.positions(c)["600001"].shares == 500
    assert broker.calls == []


def test_paused_to_sell_notified_once_per_code_and_never_placed():
    c, clock, broker, notifier, ex = env(holdings={"600001": [500, 500], "600002": [300, 300]})
    hold(c, "600001")
    hold(c, "600002", shares=300)
    remember_name(c, clock.now(), "600001", "甲公司")
    live_store.pause(c, clock.now() + timedelta(minutes=30), "手机暂停", "manual")
    for _ in range(3):
        ex.step()
        clock.sleep(1)
    clock.sleep(600)
    ex.step()
    assert len(texts(notifier, "甲公司 该卖了")) == 1
    assert len(texts(notifier, "600002 该卖了")) == 1
    assert broker.calls == []


def test_paused_remote_sell_intent_stays_new_and_notifies_once():
    c, clock, broker, notifier, ex = env(holdings={"600001": [500, 500]})
    hold(c)
    live_store.add_remote_sell(c, clock.now())
    live_store.pause(c, clock.now() + timedelta(minutes=30), "手机暂停", "manual")
    for _ in range(3):
        ex.step()
        clock.sleep(1)
    assert intent_rows(c) == [("remote", "sell", "*", "new", None)]
    assert len(texts(notifier, "暂停中收到立即卖出，恢复后执行")) == 1
    assert broker.calls == []


# ---- 恢复 ----

def test_manual_pause_expiry_resumes_reconciles_then_sells():
    c, clock, broker, notifier, ex = env(at=datetime(2026, 9, 29, 10, 0), holdings={"600001": [500, 500]})
    hold(c)
    remember_name(c, clock.now())
    live_store.pause(c, datetime(2026, 9, 29, 10, 5), "手机暂停", "manual")
    ex.step()
    assert broker.calls == [] and live_store.paused(c) is not None

    clock.t = datetime(2026, 9, 29, 10, 5)
    ex.step()
    assert live_store.paused(c) is None
    seq = names(broker)
    assert seq[0] == "positions"                     # 恢复时先对账
    assert seq.index("positions") < seq.index("place")
    assert live_store.positions(c) == {}
    assert len(texts(notifier, "暂停结束")) == 1


def test_resume_verifies_open_orders_before_reconciling_and_selling():
    c, clock, broker, notifier, ex = env(at=datetime(2026, 9, 29, 10, 5), holdings={"600001": [500, 500]})
    hold(c)
    remember_name(c, clock.now())
    entrust_no = fake_order(broker, "buy", "600002", 10.0, 100)   # 暂停前下的买单，已经成交
    order_id = live_store.add_order(c, None, clock.now() - timedelta(minutes=3), "buy", "600002", 10.0, 100,
                                    "submitted")
    live_store.update_order(c, order_id, clock.now(), entrust_no=entrust_no)
    live_store.pause(c, clock.now(), "手机暂停", "manual")

    ex.step()
    seq = names(broker)
    assert seq[0] == "orders_today"                  # 先核对 sending/submitted
    assert seq.index("orders_today") < seq.index("positions") < seq.index("place")
    assert live_store.orders_on(c, TODAY)[0]["status"] == "filled"
    assert live_store.positions(c)["600002"].shares == 100
    assert "600001" not in live_store.positions(c)


def test_resume_open_order_check_failure_auto_pauses_and_skips_the_rest():
    c, clock, broker, notifier, ex = env(at=datetime(2026, 9, 29, 10, 5), holdings={"600001": [500, 500]})
    hold(c)
    live_store.add_order(c, None, clock.now() - timedelta(minutes=3), "buy", "600002", 10.0, 100, "sending")
    live_store.pause(c, clock.now(), "手机暂停", "manual")
    broker.fail_reads = 1
    ex.step()
    assert names(broker) == ["orders_today"]         # 核对读不到 → 自动暂停，不对账、不卖
    p = live_store.paused(c)
    assert p["kind"] == "auto" and "启动核对" in p["reason"]
    assert live_store.positions(c)["600001"].shares == 500


def test_resume_requested_clears_auto_pause_and_deletes_the_key():
    c, clock, broker, notifier, ex = env()
    live_store.pause(c, None, "撤单前核对不上", "auto")
    ex.step()
    assert live_store.paused(c) is not None and broker.calls == []

    live_store.set_state(c, "resume_requested", "1")
    clock.sleep(1)
    ex.step()
    assert live_store.paused(c) is None
    assert live_store.get_state(c, "resume_requested") is None
    assert broker.calls == [("positions",)]          # 恢复时对账
    assert len(texts(notifier, "暂停结束")) == 1


def test_resume_requested_when_not_paused_only_deletes_the_key():
    c, clock, broker, notifier, ex = env()
    live_store.set_state(c, "resume_requested", "1")
    ex.step()
    assert live_store.get_state(c, "resume_requested") is None
    assert live_store.paused(c) is None
    assert texts(notifier, "暂停结束") == []


def test_auto_pause_never_resumes_by_itself():
    c, clock, broker, notifier, ex = env()
    live_store.pause(c, None, "连续读取失败", "auto")
    ex.step()
    clock.t = datetime(2026, 9, 30, 9, 35)
    live_store.set_state(c, "trading_day", "2026-09-30")
    ex.step()
    assert live_store.paused(c)["kind"] == "auto"
    live_store.pause(c, datetime(2026, 9, 29, 10, 0), "连续读取失败", "auto")   # 即使带着过去的 until
    ex.step()
    assert live_store.paused(c)["kind"] == "auto"
    assert broker.calls == []


def test_timed_manual_pause_upgraded_to_auto_is_not_resumed_at_its_time():
    c, clock, broker, notifier, ex = env()
    live_store.pause(c, datetime(2026, 9, 29, 10, 5), "手机暂停", "manual")
    ex._auto_pause("撤单前核对不上：最后一笔不是程序的委托")
    clock.t = datetime(2026, 9, 29, 10, 6)
    ex.step()
    assert live_store.paused(c)["kind"] == "auto"
    assert broker.calls == []


def test_manual_pause_from_yesterday_expires_today():
    c, clock, broker, notifier, ex = env(holdings={})
    live_store.pause(c, datetime(2026, 9, 28, 15, 0), "手机暂停", "manual")
    ex = make(c, broker, notifier, clock=clock)      # 隔夜重启：按数据库里的暂停状态初始化
    ex.step()
    assert live_store.paused(c) is None
    assert names(broker)[0] == "positions"
    assert len(texts(notifier, "暂停结束")) == 1


# ---- 暂停状态变化通知 ----

def test_manual_pause_notifies_start_and_end_once_each():
    c, clock, broker, notifier, ex = env()
    live_store.pause(c, datetime(2026, 9, 29, 10, 5), "手机暂停", "manual")
    ex.step()
    clock.sleep(1)
    ex.step()
    assert len(texts(notifier, "暂停开始")) == 1
    assert "到 10:05" in notifier.sent[0][0] + notifier.sent[0][1] and "手机暂停" in notifier.sent[0][1]

    clock.t = datetime(2026, 9, 29, 10, 5)
    ex.step()
    clock.sleep(1)
    ex.step()
    assert len(texts(notifier, "暂停开始")) == 1
    assert len(texts(notifier, "暂停结束")) == 1


def test_restart_during_manual_pause_does_not_repeat_the_start_notice():
    c, clock, broker, notifier, ex = env()
    live_store.pause(c, datetime(2026, 9, 29, 10, 5), "手机暂停", "manual")
    later = FakeNotifier()
    make(c, broker, later, clock=clock).step()
    assert later.sent == []


def test_auto_pause_is_notified_once_by_auto_pause_and_not_again_by_step():
    c, clock, broker, notifier, ex = env()
    ex._auto_pause("委托状态查不到：测试")
    ex.step()
    clock.sleep(1)
    ex.step()
    assert len([t for t, _ in notifier.sent if "自动暂停" in t]) == 1
    assert texts(notifier, "暂停开始") == []


def test_pause_landing_mid_attempt_finishes_the_current_round_but_starts_no_new_one():
    c, clock, broker, notifier, _ = env(holdings={"600001": [500, 500]})
    broker.fill_mode = "none"
    hold(c)
    fired = []

    def sleep(seconds):
        clock.sleep(seconds)
        if not fired:   # 页面线程在这一轮等成交期间点了暂停
            fired.append(1)
            live_store.pause(c, clock.now() + timedelta(minutes=30), "手机暂停", "manual")

    ex = Executor(c, broker, notifier, clock=clock.now, sleep=sleep, quotes=quotes_fn())
    ex.step()
    assert len(places(broker)) == 1                  # 这一轮照常撤单收尾，不开第二轮
    assert ("cancel_last",) in broker.calls
    assert live_store.orders_on(c, TODAY)[0]["status"] == "cancelled"

    calls = len(broker.calls)
    clock.sleep(1)
    ex.step()
    assert len(broker.calls) == calls and len(places(broker)) == 1


def test_sell_round_never_starts_while_paused():
    c, clock, broker, notifier, ex = env(holdings={"600001": [500, 500]})
    hold(c)
    live_store.pause(c, None, "结果不明", "auto")
    assert ex._sell_round("600001") is False
    assert broker.calls == []


# ---- 立即卖出 ----

def test_sell_all_sells_everything_then_pauses_to_the_close_as_manual():
    c, clock, broker, notifier, ex = env(holdings={"600001": [500, 500]})
    hold(c)
    sim_holds(c)                                     # 模拟盘还持有：立即卖出照样卖
    remember_name(c, clock.now())
    live_store.add_remote_sell(c, clock.now())
    ex.step()

    assert len(places(broker)) == 1 and places(broker)[0][1:3] == ("sell", "600001")
    assert live_store.positions(c) == {}
    assert intent_rows(c, "source='remote'") == [("remote", "sell", "*", "done", "已卖完")]
    assert live_store.paused(c) == {"until": datetime(2026, 9, 29, 15, 0), "reason": "立即卖出", "kind": "manual"}
    clock.sleep(1)
    ex.step()
    assert len(places(broker)) == 1
    assert len(texts(notifier, "暂停开始")) == 1


def test_sell_all_with_flat_program_finishes_immediately_and_pauses_to_the_close():
    c, clock, broker, notifier, ex = env()
    live_store.add_remote_sell(c, clock.now())
    ex.step()
    assert intent_rows(c) == [("remote", "sell", "*", "done", "已卖完")]
    assert live_store.paused(c) == {"until": datetime(2026, 9, 29, 15, 0), "reason": "立即卖出", "kind": "manual"}
    assert places(broker) == []


def test_sell_all_after_the_close_does_not_leave_a_dead_pause_behind():
    c, clock, broker, notifier, ex = env(at=datetime(2026, 9, 29, 15, 30))
    live_store.add_remote_sell(c, clock.now())
    ex.step()
    assert intent_rows(c) == [("remote", "sell", "*", "done", "已卖完")]
    assert live_store.paused(c) is None


def test_sell_all_keeps_intent_new_for_a_position_bought_today_and_notifies_once():
    c, clock, broker, notifier, ex = env(holdings={"600001": [500, 0]})
    hold(c, buy_date=TODAY)
    remember_name(c, clock.now())
    live_store.add_remote_sell(c, clock.now())
    for _ in range(3):
        ex.step()
        clock.sleep(1)
    assert intent_rows(c, "source='remote'") == [("remote", "sell", "*", "new", None)]
    assert len(texts(notifier, "今天买的，下个交易日开盘卖")) == 1
    assert places(broker) == []
    assert live_store.paused(c) is None


def test_sell_all_pause_lands_before_buy_intents_of_the_same_step():
    c, clock, broker, notifier, ex = env(holdings={"600001": [500, 500]})
    hold(c)
    live_store.add_remote_sell(c, clock.now())
    add_sim(c, clock.now(), "buy", "600002", "乙公司")
    ex.step()
    assert [(p[1], p[2]) for p in places(broker)] == [("sell", "600001")]
    assert intent_rows(c, "source='sim'") == [("sim", "buy", "600002", "skipped", "暂停中")]


def test_sell_all_does_not_override_an_auto_pause_raised_in_the_same_step():
    c = conn()
    clock = FakeClock(AM)
    setup_ready(c, clock)
    live_store.set_state(c, "read_fail_streak", "2")
    broker = FakeBroker(clock, cash=50_000)
    broker.fail_reads = 1                            # 自检读资金失败 → 第 3 次连续失败 → 自动暂停
    notifier = FakeNotifier()
    ex = make(c, broker, notifier, clock=clock)
    live_store.add_remote_sell(c, clock.now())
    ex.step()

    assert live_store.paused(c)["kind"] == "auto"
    assert intent_rows(c) == [("remote", "sell", "*", "new", None)]
    assert broker.calls == [("balance",)]            # 暂停后本轮不再碰券商


# ---- 对账 ----

def test_reconcile_first_low_reading_does_not_reduce_the_books():
    c, clock, broker, notifier, ex = env(holdings={"600001": [300, 300]})
    hold(c, shares=500)
    ex._reconcile(clock.now())
    assert live_store.positions(c)["600001"].shares == 500
    assert pending(c)["shares"] == 300


def test_reconcile_same_low_reading_a_minute_later_reduces_and_notifies():
    c, clock, broker, notifier, ex = env(holdings={"600001": [300, 300]})
    hold(c, shares=500)
    remember_name(c, clock.now())
    ex._reconcile(clock.now())
    clock.sleep(59)
    ex._reconcile(clock.now())
    assert live_store.positions(c)["600001"].shares == 500     # 不足 60 秒
    clock.sleep(1)
    ex._reconcile(clock.now())
    assert live_store.positions(c)["600001"].shares == 300
    assert live_store.get_state(c, "reduce:600001") is None
    [note] = texts(notifier, "按客户端减账")
    assert "甲公司" in note and "500" in note and "300" in note and "人工卖出" in note


def test_reconcile_client_holds_nothing_reduces_to_zero_and_deletes_the_row():
    c, clock, broker, notifier, ex = env(holdings={})
    hold(c, shares=500)
    ex._reconcile(clock.now())
    clock.sleep(60)
    ex._reconcile(clock.now())
    assert live_store.positions(c) == {}
    assert len(texts(notifier, "按客户端减账")) == 1


def test_reconcile_different_reading_restarts_the_timer():
    c, clock, broker, notifier, ex = env(holdings={"600001": [300, 300]})
    hold(c, shares=500)
    ex._reconcile(clock.now())
    clock.sleep(60)
    broker.holdings["600001"] = [200, 200]
    ex._reconcile(clock.now())
    assert live_store.positions(c)["600001"].shares == 500
    assert pending(c)["shares"] == 200
    clock.sleep(30)
    ex._reconcile(clock.now())
    assert live_store.positions(c)["600001"].shares == 500     # 新数才过了 30 秒
    clock.sleep(30)
    ex._reconcile(clock.now())
    assert live_store.positions(c)["600001"].shares == 200


def test_reconcile_client_holding_more_or_equal_leaves_books_and_clears_pending():
    c, clock, broker, notifier, ex = env(holdings={"600001": [300, 300]})
    hold(c, shares=500)
    ex._reconcile(clock.now())
    assert pending(c) is not None
    broker.holdings["600001"] = [800, 800]           # 多出来的是用户自己的
    clock.sleep(120)
    ex._reconcile(clock.now())
    assert live_store.positions(c)["600001"].shares == 500
    assert pending(c) is None
    assert texts(notifier, "按客户端减账") == []


def test_reconcile_skips_a_code_with_a_submitted_sell_order_today():
    c, clock, broker, notifier, ex = env(holdings={})
    hold(c, shares=500)
    live_store.add_order(c, None, clock.now(), "sell", "600001", 9.9, 500, "submitted")
    ex._reconcile(clock.now())
    clock.sleep(120)
    ex._reconcile(clock.now())
    assert live_store.positions(c)["600001"].shares == 500
    assert pending(c) is None


def test_reconcile_skips_a_code_with_a_sending_order_today():
    c, clock, broker, notifier, ex = env(holdings={})
    hold(c, shares=500)
    live_store.add_order(c, None, clock.now(), "sell", "600001", 9.9, 500, "sending")
    ex._reconcile(clock.now())
    clock.sleep(120)
    ex._reconcile(clock.now())
    assert live_store.positions(c)["600001"].shares == 500
    assert pending(c) is None


def test_reconcile_in_dryrun_reads_but_never_changes_the_books():
    c, clock, broker, notifier, ex = env(mode="dryrun", holdings={})
    hold(c, shares=500)
    ex._reconcile(clock.now())
    clock.sleep(120)
    ex._reconcile(clock.now())
    assert live_store.positions(c)["600001"].shares == 500
    assert pending(c) is None
    assert broker.calls == [("positions",), ("positions",)]


def test_reconcile_read_failure_changes_nothing_and_counts_the_failure():
    c, clock, broker, notifier, ex = env(holdings={})
    hold(c, shares=500)
    broker.fail_reads = 1
    ex._reconcile(clock.now())
    assert live_store.positions(c)["600001"].shares == 500
    assert pending(c) is None
    assert ex.read_fail_streak == 1


def test_reconcile_drops_pending_reductions_for_codes_the_program_no_longer_holds():
    c, clock, broker, notifier, ex = env(holdings={"600001": [500, 500]})
    hold(c, shares=500)
    live_store.set_state(c, "reduce:600009", json.dumps({"shares": 0, "ts": clock.now().isoformat()}))
    ex._reconcile(clock.now())
    assert live_store.get_state(c, "reduce:600009") is None


def test_sell_round_reconciles_with_the_holdings_it_read_and_never_double_counts():
    c, clock, broker, notifier, ex = env(holdings={})      # 用户手动把它卖光了
    hold(c, shares=500)
    remember_name(c, clock.now())
    ex.step()
    assert places(broker) == []
    assert pending(c)["shares"] == 0
    assert live_store.positions(c)["600001"].shares == 500

    clock.sleep(61)
    ex.step()
    assert places(broker) == []
    assert live_store.positions(c) == {}
    assert len(texts(notifier, "按客户端减账")) == 1


# ---- 定时对账 ----

def test_scheduled_reconcile_with_holding_runs_every_five_minutes():
    c, clock, broker, notifier, ex = env(holdings={"600001": [500, 500]})
    hold(c)
    sim_holds(c)
    ex.step()
    assert names(broker) == ["positions"]            # 启动后第一轮立刻做一次
    clock.t = datetime(2026, 9, 29, 9, 39, 59)
    ex.step()
    assert names(broker) == ["positions"]
    clock.t = datetime(2026, 9, 29, 9, 40, 0)
    ex.step()
    assert names(broker) == ["positions", "positions"]


def test_scheduled_check_when_flat_reads_balance_every_fifteen_minutes():
    c, clock, broker, notifier, ex = env()
    ex.step()
    assert broker.calls == [("balance",)]
    clock.t = datetime(2026, 9, 29, 9, 49, 59)
    ex.step()
    assert broker.calls == [("balance",)]
    clock.t = datetime(2026, 9, 29, 9, 50, 0)
    ex.step()
    assert broker.calls == [("balance",), ("balance",)]


def test_scheduled_idle_read_failure_only_counts():
    c, clock, broker, notifier, ex = env()
    broker.fail_reads = 1
    ex.step()
    assert ex.read_fail_streak == 1
    assert live_store.paused(c) is None
    assert notifier.sent == []


def test_no_scheduled_reconcile_during_the_lunch_break():
    c, clock, broker, notifier, ex = env(at=datetime(2026, 9, 29, 11, 45), holdings={"600001": [500, 500]})
    hold(c)
    sim_holds(c)
    ex.step()
    clock.t = datetime(2026, 9, 29, 12, 59, 59)
    ex.step()
    assert broker.calls == []
    clock.t = datetime(2026, 9, 29, 13, 0, 0)
    ex.step()
    assert names(broker) == ["positions"]


# ---- 自检 ----

def test_self_check_at_0905_is_sent_once():
    c, clock, broker, notifier, ex = env(at=datetime(2026, 9, 29, 9, 4, 59), checked=False,
                                         holdings={"600001": [500, 500]})
    hold(c)
    sim_holds(c)
    remember_name(c, clock.now())
    ex.step()
    assert broker.calls == [] and notifier.sent == []

    clock.t = datetime(2026, 9, 29, 9, 5, 0)
    ex.step()
    assert names(broker) == ["balance", "positions"]
    [text] = texts(notifier, "自检通过")
    assert "模式 live" in text and "50000.00" in text and "甲公司" in text and "500股" in text
    for delta in (1, 3600):
        clock.sleep(delta)
        ex.step()
    assert len(texts(notifier, "自检通过")) == 1
    assert live_store.get_state(c, "self_check_day") == "2026-09-29"


def test_self_check_reports_an_empty_program_as_flat():
    c, clock, broker, notifier, ex = env(at=datetime(2026, 9, 29, 9, 5, 0), checked=False)
    ex.step()
    [text] = texts(notifier, "自检通过")
    assert "空仓" in text


def test_self_check_failure_is_sent_once_and_not_retried():
    c, clock, broker, notifier, ex = env(at=datetime(2026, 9, 29, 9, 5, 0), checked=False)
    broker.fail_reads = 1
    ex.step()
    assert len(texts(notifier, "自检失败")) == 1
    assert live_store.get_state(c, "self_check_day") == "2026-09-29"
    for delta in (1, 60, 3600):
        clock.sleep(delta)
        ex.step()
    assert len(texts(notifier, "自检失败")) == 1 and texts(notifier, "自检通过") == []


# ---- 收盘小结 ----

def test_close_summary_is_sent_once_at_1502_without_touching_the_broker():
    c, clock, broker, notifier, ex = env(at=datetime(2026, 9, 29, 15, 1, 59))
    hold(c, "600001", shares=500, buy_price=10.0, buy_date=TODAY)
    filled = live_store.add_order(c, None, datetime(2026, 9, 29, 9, 40), "buy", "600001", 10.05, 500, "filled")
    live_store.update_order(c, filled, clock.now(), filled=500)
    live_store.add_order(c, None, datetime(2026, 9, 29, 10, 0), "buy", "600002", 10.0, 500, "cancelled")
    for code in ("600003", "600004", "600005"):
        add_sim(c, datetime(2026, 9, 29, 11, 40), "buy", code)
    for intent in live_store.new_intents(c):
        live_store.finish_intent(c, intent.id, "skipped", "你持有这只" if intent.code == "600005" else "不在买入时段",
                                 datetime(2026, 9, 29, 11, 40))
    for i in range(3):
        c.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) VALUES (?,?,?,?,?,?,?,?)",
                  (config.LIVE_ACCOUNT, "600001", "buy", 10.0, 100, 5.0, f"2026-09-29T09:3{i}:00", "t"))
    c.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) VALUES (?,?,?,?,?,?,?,?)",
              ("other", "600001", "buy", 10.0, 100, 5.0, "2026-09-29T09:30:00", "t"))
    c.commit()

    ex.step()
    assert texts(notifier, "收盘小结") == []          # 15:01:59 还早
    clock.t = datetime(2026, 9, 29, 15, 2, 0)
    ex.step()
    [text] = texts(notifier, "收盘小结")
    assert "委托 2 笔" in text and "成交 1 笔" in text
    assert "跳过 3 笔" in text and "不在买入时段 2" in text and "你持有这只 1" in text
    assert "600001" in text and "500股" in text and "成本价 10.0" in text
    assert "模拟成交 3 笔" in text
    for delta in (1, 3600):
        clock.sleep(delta)
        ex.step()
    assert len(texts(notifier, "收盘小结")) == 1
    assert live_store.get_state(c, "close_summary_day") == "2026-09-29"
    assert broker.calls == []


def test_close_summary_flat_program_says_so():
    c, clock, broker, notifier, ex = env(at=datetime(2026, 9, 29, 15, 2, 0))
    ex.step()
    [text] = texts(notifier, "收盘小结")
    assert "空仓" in text and "委托 0 笔" in text and "没结算" not in text


def test_close_summary_waits_for_the_after_close_settlement_then_reports():
    c, clock, broker, notifier, ex = env(at=datetime(2026, 9, 29, 15, 2, 0), holdings={"600001": [500, 500]})
    hold(c)
    broker.fill_mode = "none"
    entrust_no = fake_order(broker, "sell", "600001", 9.9, 500)
    order_id = live_store.add_order(c, None, datetime(2026, 9, 29, 14, 58), "sell", "600001", 9.9, 500, "submitted")
    live_store.update_order(c, order_id, clock.now(), entrust_no=entrust_no)
    ex.step()
    assert live_store.orders_on(c, TODAY)[0]["status"] == "cancelled"     # 收盘后结算先落终态
    [text] = texts(notifier, "收盘小结")
    assert "没结算" not in text


def test_close_summary_goes_out_at_the_latest_time_even_if_settlement_cannot_read():
    c, clock, broker, notifier, ex = env(at=datetime(2026, 9, 29, 15, 2, 0), holdings={"600001": [500, 500]})
    hold(c)
    live_store.add_order(c, None, datetime(2026, 9, 29, 14, 58), "sell", "600001", 9.9, 500, "submitted")
    broker.fail_reads = 2
    ex.step()
    assert texts(notifier, "收盘小结") == []          # 还在等结算
    clock.t = datetime(2026, 9, 29, 15, 10, 0)
    ex.step()
    [text] = texts(notifier, "收盘小结")
    assert "有卖单没结算" in text
    assert live_store.orders_on(c, TODAY)[0]["status"] == "submitted"     # 不许猜


# ---- 启动 ----

def test_startup_while_paused_makes_no_broker_calls():
    c, clock, broker, notifier, ex = env(holdings={"600001": [500, 500]})
    hold(c)
    live_store.add_order(c, None, clock.now(), "buy", "600002", 10.0, 100, "sending")
    live_store.pause(c, None, "结果不明", "auto")
    ex.startup()
    assert broker.calls == []
    assert live_store.orders_on(c, TODAY)[0]["status"] == "sending"


def test_startup_not_paused_verifies_open_orders_then_reconciles():
    c, clock, broker, notifier, ex = env()
    fake_order(broker, "buy", "600002", 10.0, 100)
    live_store.add_order(c, None, clock.now(), "buy", "600002", 10.0, 100, "sending")
    ex.startup()
    assert broker.calls == [("orders_today",), ("positions",)]
    assert live_store.orders_on(c, TODAY)[0]["status"] == "filled"
    assert live_store.positions(c)["600002"].shares == 100


def test_startup_reconciles_even_with_nothing_to_verify():
    c, clock, broker, notifier, ex = env(holdings={"600001": [300, 300]})
    hold(c, shares=500)
    ex.startup()
    assert broker.calls == [("positions",)]
    assert pending(c)["shares"] == 300               # 第一次读到少：只记下，不减


def test_startup_in_dryrun_reads_positions_but_leaves_the_books():
    c, clock, broker, notifier, ex = env(mode="dryrun", holdings={})
    hold(c, shares=500)
    ex.startup()
    assert broker.calls == [("positions",)]
    assert live_store.positions(c)["600001"].shares == 500 and pending(c) is None


def test_startup_in_off_mode_never_reconciles():
    c, clock, broker, notifier, ex = env(mode="off")
    hold(c)
    ex.startup()
    assert broker.calls == []


def test_startup_open_order_check_failure_auto_pauses_and_skips_reconcile():
    c, clock, broker, notifier, ex = env()
    live_store.add_order(c, None, clock.now(), "buy", "600002", 10.0, 100, "sending")
    broker.fail_reads = 1
    ex.startup()
    assert names(broker) == ["orders_today"]
    assert live_store.paused(c)["kind"] == "auto"


def test_startup_leaves_open_orders_from_earlier_days_alone():
    c, clock, broker, notifier, ex = env()
    live_store.add_order(c, None, datetime(2026, 9, 28, 14, 0), "sell", "600001", 9.9, 500, "submitted")
    ex.startup()
    assert names(broker) == ["positions"]
    assert live_store.open_orders(c)[0]["status"] == "submitted"


# ---- store.pause / clear_pause ----

def test_pause_and_clear_pause_commit_once_each_and_roundtrip():
    c = conn()
    log = []
    c.set_trace_callback(log.append)
    live_store.pause(c, datetime(2026, 9, 29, 15, 0), "手机暂停", "manual")
    assert log.count("COMMIT") == 1
    assert live_store.paused(c) == {"until": datetime(2026, 9, 29, 15, 0), "reason": "手机暂停", "kind": "manual"}

    log.clear()
    live_store.pause(c, None, "连续读取失败", "auto")     # 覆盖：旧 until 必须清掉
    assert log.count("COMMIT") == 1
    assert live_store.paused(c) == {"until": None, "reason": "连续读取失败", "kind": "auto"}

    log.clear()
    live_store.clear_pause(c)
    assert log.count("COMMIT") == 1
    assert live_store.paused(c) is None
    assert c.execute("SELECT COUNT(*) FROM live_state WHERE key IN "
                     "('paused','paused_until','pause_reason','pause_kind')").fetchone()[0] == 0
