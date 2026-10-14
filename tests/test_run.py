import threading
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import pytest

import run
from factories import quote
from jevq import config, db
from jevq.data import board_events as be
from jevq.ledger import Ledger
from jevq.live import store as live_store
from jevq.models import Fill
from jevq.notify import Notifier
from jevq.rules.board import SCAN_WINDOWS, Candidate
from jevq.runlock import hold_run_lock
from jevq.strategies.board import BoardStrategy as RealBoardStrategy

DAYS = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]
CLOSE_DATES = ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22"]


def seed_closes(dbp, code, prices=None, volumes=None):
    """给 code 灌 6 个交易日的收盘价，让 5 日涨幅覆盖率天然是 100%（默认 5.0%，和旧 mock 值保持一致）。
    给了 volumes（6 个，手）就一并写成交量。"""
    prices = prices or [10.0, 10.0, 10.0, 10.0, 10.0, 10.5]
    conn = db.connect(dbp)
    conn.executemany("INSERT OR REPLACE INTO closes(date, code, close, volume) VALUES (?, ?, ?, ?)",
                     [(d, code, p, v) for d, p, v in zip(CLOSE_DATES, prices, volumes or [None] * len(prices))])
    conn.commit()
    conn.close()


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def now(self) -> datetime:
        return self.t

    def step(self) -> float:
        self.t += timedelta(minutes=1)
        return 0.0


class FakeMarket:
    fail = False

    def __init__(self, jev):
        self.overseas_trace = None

    def overseas(self, pcts):
        if FakeMarket.fail:
            raise RuntimeError("jev down")
        return "中性"


class FakeEngine:
    instances: list = []
    behavior = None

    def __init__(self, conn, ledger, market, fetch_quotes, fetch_pools, universe, prev_day, overseas,
                 accounts_provider=None, articles_provider=None):
        self.conn, self.ledger = conn, ledger
        self.articles_provider, self.articles, self.sectors = articles_provider, [], []
        self.universe, self.prev_day, self.overseas = universe, prev_day, overseas
        self.fetch_quotes, self.accounts_provider = fetch_quotes, accounts_provider
        self.ticks, self.last_quotes, self.sentiment_label = [], {}, "正常"
        self.buy_calls: list = []
        self.sentiment_trace = None
        FakeEngine.instances.append(self)

    def sell_step(self, now):
        """卖出阶段（run.py 每分钟先调它：取行情）；ticks 记的是「这一分钟跑过」。"""
        self.ticks.append(now)
        if FakeEngine.behavior:
            return FakeEngine.behavior(self, now) or []
        return []

    def buy_step(self, now):
        self.buy_calls.append(now)
        return []

    def tick(self, now):
        return self.sell_step(now) + self.buy_step(now)


def seed_concepts(dbp, code="600001", concept="题材A", updated="2026-09-23"):
    conn = db.connect(dbp)
    conn.execute("INSERT OR REPLACE INTO concepts(code, concept, updated) VALUES (?, ?, ?)",
                 (code, concept, updated))
    conn.commit()
    conn.close()


class SpyBoard(RealBoardStrategy):
    """真实 BoardStrategy 包一层：记录实例、按需注入观察池（一次性或常驻）、按需炸一次
    sell_step/on_fast、按需在 on_fast 里模拟真实耗时（顺手拨一下假表）。"""
    instances: list = []
    boom_once = False
    force_watch_once: dict | None = None
    force_watch: dict | None = None   # 常驻版：每分钟 buy_step 都补回去，不怕被剔出观察池
    fast_call_times: list = []
    on_fast_latency = 0.0
    on_fast_boom_once = False
    advance_clock = None   # Callable[[float], None]，测试用来模拟 on_fast 的真实耗时

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        SpyBoard.instances.append(self)

    def sell_step(self, quotes, now):
        if SpyBoard.boom_once:
            SpyBoard.boom_once = False
            raise RuntimeError("board boom")
        return super().sell_step(quotes, now)

    def buy_step(self, quotes, now, deadline=None):
        fills = super().buy_step(quotes, now, deadline)
        if SpyBoard.force_watch_once:
            self.watch.update(SpyBoard.force_watch_once)
            SpyBoard.force_watch_once = None
        if SpyBoard.force_watch:
            for code, cand in SpyBoard.force_watch.items():
                self.watch.setdefault(code, cand)
        return fills

    def on_fast(self, now):
        SpyBoard.fast_call_times.append(now)
        if SpyBoard.on_fast_latency and SpyBoard.advance_clock:
            SpyBoard.advance_clock(SpyBoard.on_fast_latency)
        if SpyBoard.on_fast_boom_once:
            SpyBoard.on_fast_boom_once = False
            raise RuntimeError("on_fast boom")
        return super().on_fast(now)


class Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "data" / "jevq.sqlite"
    reports = tmp_path / "reports"
    monkeypatch.setattr(run, "DB_PATH", str(dbp))
    monkeypatch.setattr(run, "REPORT_DIR", str(reports))
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    # 这台机器上真实跑着日常任务的守护进程，环境变量/注册表里可能有真的 PUSHPLUS_TOKEN；
    # 测试永远不能真的发推送或联网，默认给一个禁用的假 Notifier，需要验证推送行为的测试
    # 自己注入带假 post 的 Notifier（见 env.run(notifier=...)）。
    monkeypatch.setattr(run, "notifier_from_env", lambda **kw: Notifier(None))
    clock = Clock(datetime(2026, 9, 23, 9, 29))
    monkeypatch.setattr(run, "now", clock.now)
    monkeypatch.setattr(run, "seconds_to_next_minute", clock.step)
    monkeypatch.setattr(run.akdata, "trade_days", lambda: DAYS)
    monkeypatch.setattr(run.akdata, "load_universe", lambda today, days: {"600001": "甲"})
    seed_closes(str(dbp), "600001")
    # 默认涨停池为空：保证测试不会真的发请求，也不给封板统计留任何行。
    feed_zt = {"rows": pd.DataFrame(columns=["代码", "名称", "首次封板时间", "连板数"])}
    monkeypatch.setattr(run.akdata, "fetch_zt_pool", lambda d: feed_zt["rows"])
    # 默认永远失败：模拟 K 线接口不可用，同时保证测试不会真的发请求。
    monkeypatch.setattr(run.akdata, "fetch_daily_sina",
                        lambda code, **kw: (_ for _ in ()).throw(RuntimeError("sina down")))
    monkeypatch.setattr(run.tencent, "fetch_kline",
                        lambda code, session=None: (_ for _ in ()).throw(RuntimeError("WAF 501")))
    monkeypatch.setattr(run.tencent, "fetch_overseas", lambda: {"道琼斯": 0.5})
    feed = {"quotes": {}}

    def fetch_quotes(codes):
        if isinstance(feed["quotes"], Exception):
            raise feed["quotes"]
        return {c: q for c, q in feed["quotes"].items() if c in codes}

    monkeypatch.setattr(run.tencent, "fetch_quotes", fetch_quotes)
    monkeypatch.setattr(run, "sync_notices", lambda conn, today: 0)
    # 公告正文预抓默认顶掉：绝不真的去敲东方财富正文接口；需要验证的测试自己再注入。
    prefetch_calls = []
    monkeypatch.setattr(run, "prefetch_bodies",
                        lambda conn, today, codes, **kw: prefetch_calls.append((today, list(codes))) or 0)
    monkeypatch.setattr(run, "MarketJudge", FakeMarket)
    monkeypatch.setattr(run, "Engine", FakeEngine)
    FakeMarket.fail = False
    FakeEngine.instances, FakeEngine.behavior = [], None

    # 题材表默认空库；needs_refresh 天然会返回 True，必须顶掉，否则会真的去敲 akshare 的网。
    monkeypatch.setattr(run.concepts, "needs_refresh", lambda conn, today: False)
    monkeypatch.setattr(run.concepts, "refresh_concepts", lambda *a, **kw: 0)

    monkeypatch.setattr(run, "BoardStrategy", SpyBoard)
    SpyBoard.instances, SpyBoard.boom_once = [], False
    SpyBoard.force_watch_once, SpyBoard.force_watch, SpyBoard.fast_call_times = None, None, []
    SpyBoard.on_fast_latency, SpyBoard.on_fast_boom_once = 0.0, False
    SpyBoard.advance_clock = lambda seconds: setattr(clock, "t", clock.t + timedelta(seconds=seconds))

    def tick_wait(seconds, stop):
        clock.t += timedelta(seconds=seconds)
        return False

    monkeypatch.setattr(run, "_tick_wait", tick_wait)

    logs: list[str] = []

    e = Env()
    e.db, e.reports, e.clock, e.feed, e.logs = str(dbp), reports, clock, feed, logs
    e.feed_zt = feed_zt
    e.prefetch_calls = prefetch_calls
    e.run = lambda **kw: run.run_day(log=logs.append, **kw)
    e.log_text = lambda: "\n".join(logs)
    e.boards = SpyBoard.instances
    return e


HOLD_ACCOUNT = "rebound_main"


def hold(env, price=9.9, buy_day=date(2026, 9, 22)):
    conn = db.connect(env.db)
    Ledger(conn).buy(HOLD_ACCOUNT, quote("600001", pct=round((price / 10 - 1) * 100, 2)),
                     datetime.combine(buy_day, datetime.min.time()).replace(hour=10), "x")
    conn.close()


def main_equity_check(env, price):
    conn = db.connect(env.db)
    try:
        ledger = Ledger(conn)
        shares = ledger.positions(HOLD_ACCOUNT)[0].shares
        expected = round(ledger.cash(HOLD_ACCOUNT) + shares * price, 2)
        value = conn.execute("SELECT value FROM equity WHERE account=?", (HOLD_ACCOUNT,)).fetchone()[0]
    finally:
        conn.close()
    assert value == pytest.approx(expected)


# ---- C1：异常边界 ----

def test_tick_exception_is_logged_and_day_continues(env):
    def boom_once(eng, now):
        if len(eng.ticks) == 1:
            eng.conn.execute("INSERT INTO turnover VALUES ('half-written', 1)")
            raise RuntimeError("boom")

    FakeEngine.behavior = boom_once
    path = env.run()
    eng = FakeEngine.instances[0]
    assert len(eng.ticks) == 240                       # 两个交易时段每分钟一轮
    assert "本轮出错：RuntimeError('boom')" in env.log_text()
    assert path is not None and Path(path).exists()
    conn = db.connect(env.db)
    assert conn.execute("SELECT COUNT(*) FROM turnover").fetchone()[0] == 0   # 半截写入已回滚
    conn.close()


def test_calendar_failure_falls_back_to_weekdays_sell_only(env, monkeypatch):
    def fail():
        raise TimeoutError("sina 超时")

    monkeypatch.setattr(run.akdata, "trade_days", fail)
    env.run()
    eng = FakeEngine.instances[0]
    assert eng.prev_day == date(2026, 9, 22)
    assert eng.universe == {}
    assert "只卖模式：今日不开新仓" in env.log_text()
    assert len(eng.ticks) == 240


def test_calendar_failure_weekend_is_not_a_trading_day(env, monkeypatch):
    monkeypatch.setattr(run.akdata, "trade_days", lambda: 1 / 0)
    env.clock.t = datetime(2026, 9, 26, 9, 29)          # 周六
    assert env.run() is None
    assert FakeEngine.instances == []
    assert "非交易日" in env.log_text()


def test_calendar_failure_monday_prev_day_is_friday(env, monkeypatch):
    monkeypatch.setattr(run.akdata, "trade_days", lambda: 1 / 0)
    env.clock.t = datetime(2026, 9, 28, 9, 29)          # 周一
    env.run()
    assert FakeEngine.instances[0].prev_day == date(2026, 9, 25)


@pytest.mark.parametrize("target", ["load_universe", "ret5_from_db"])
def test_universe_failure_runs_sell_only(env, monkeypatch, target):
    def fail(*a):
        raise RuntimeError("akshare down")

    monkeypatch.setattr(run.akdata if target == "load_universe" else run.closes, target, fail)
    path = env.run()
    eng = FakeEngine.instances[0]
    assert eng.universe == {}
    assert "只卖模式：今日不开新仓" in env.log_text()
    assert len(eng.ticks) == 240 and path is not None


@pytest.mark.parametrize("which", ["fetch", "judge"])
def test_overseas_failure_gives_none(env, monkeypatch, which):
    if which == "fetch":
        def fail():
            raise RuntimeError("qt down")
        monkeypatch.setattr(run.tencent, "fetch_overseas", fail)
    else:
        FakeMarket.fail = True
    env.run()
    assert FakeEngine.instances[0].overseas is None
    assert len(FakeEngine.instances[0].ticks) == 240


def test_overseas_ok_passed_to_engine(env):
    env.run()
    assert FakeEngine.instances[0].overseas == "中性"
    assert FakeEngine.instances[0].universe == {"600001": "甲"}


def test_notice_sync_failure_is_logged_and_day_continues(env, monkeypatch):
    calls = []

    def fail(conn, today):
        calls.append(today)
        raise RuntimeError("notice down")

    monkeypatch.setattr(run, "sync_notices", fail)
    path = env.run()
    assert len(calls) == 2                              # 盘前 + 13:00 重同步都失败也照常跑完
    assert len(FakeEngine.instances[0].ticks) == 240 and path is not None
    assert "notice down" in env.log_text()


def test_premarket_catalyst_window_starts_prev_close_and_extra_snapshot_before_report(env, monkeypatch):
    calls = []
    monkeypatch.setattr(run.config, "CATALYST_ENABLED", True)
    monkeypatch.setattr(run.config, "CATALYST_DEADLINE", time(23, 0))     # 测试时钟 09:29 起，放宽到不跳过
    monkeypatch.setattr(run.config, "MARKET_EXTRA_ENABLED", True)
    monkeypatch.setattr(run.catalyst, "run", lambda conn, day, start, end, **k: calls.append(("catalyst", day, start)) or {})
    monkeypatch.setattr(run.market_extra, "snapshot", lambda conn, day, **k: calls.append(("extra", day)) or {})
    real_report = run.write_report
    monkeypatch.setattr(run, "write_report", lambda *a, **k: calls.append(("report",)) or real_report(*a, **k))
    env.run()
    kinds = [c[0] for c in calls]
    assert kinds == ["catalyst", "extra", "report"]
    assert calls[0][2].time() == time(15, 0) and calls[0][2].date() < calls[0][1]


def test_catalyst_failure_does_not_stop_the_day(env, monkeypatch):
    monkeypatch.setattr(run.config, "CATALYST_ENABLED", True)
    monkeypatch.setattr(run.config, "CATALYST_DEADLINE", time(23, 0))

    def boom(*a, **k):
        raise RuntimeError("rss down")
    monkeypatch.setattr(run.catalyst, "run", boom)
    assert env.run() is not None and "rss down" in env.log_text()
    assert len(FakeEngine.instances[0].ticks) == 240


def test_missing_key_without_positions_is_fatal(env, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        env.run()


def test_missing_key_with_positions_runs_sell_only(env, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    hold(env)
    env.run()
    eng = FakeEngine.instances[0]
    assert eng.universe == {} and eng.overseas is None
    assert len(eng.ticks) == 240
    assert "只卖模式：今日不开新仓" in env.log_text()


def test_report_failure_is_logged(env, monkeypatch):
    def fail(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(run, "write_report", fail)
    assert env.run() is None
    assert "disk full" in env.log_text()


# ---- I2：单进程锁 ----

def test_second_runner_refused_while_lock_held(env):
    with hold_run_lock(Path(env.db).parent / "runner.lock"):
        with pytest.raises(RuntimeError, match="已有另一个交易进程在运行"):
            env.run()
    assert FakeEngine.instances == []
    env.run()                                           # 锁释放后可以正常运行
    assert len(FakeEngine.instances) == 1


# ---- I3：收盘后重启 / 报告价格 ----

def test_report_uses_refreshed_close_prices(env):
    hold(env, price=9.9)
    FakeEngine.behavior = lambda eng, now: setattr(eng, "last_quotes", {"600001": quote("600001", pct=0.0)})
    env.feed["quotes"] = {"600001": quote("600001", pct=3.0)}
    env.run()
    main_equity_check(env, 10.3)


def test_report_falls_back_when_refresh_fails(env):
    hold(env, price=9.9)
    FakeEngine.behavior = lambda eng, now: setattr(eng, "last_quotes", {"600001": quote("600001", pct=1.0)})
    env.feed["quotes"] = RuntimeError("qt down")
    assert env.run() is not None
    main_equity_check(env, 10.1)


def test_late_start_with_existing_report_skips(env):
    env.reports.mkdir()
    report = env.reports / "2026-09-23.md"
    report.write_text("原报告", encoding="utf-8")
    env.clock.t = datetime(2026, 9, 23, 15, 10)
    assert env.run() is None
    assert "今日报告已生成，跳过" in env.log_text()
    assert report.read_text(encoding="utf-8") == "原报告"
    assert all(not e.ticks for e in FakeEngine.instances)


def test_late_start_without_report_writes_it_with_fresh_prices(env, monkeypatch):
    hold(env, price=9.9)
    loaded = []
    monkeypatch.setattr(run.akdata, "load_universe", lambda today, days: loaded.append(1) or {})
    env.feed["quotes"] = {"600001": quote("600001", pct=5.0, name="甲",
                                          ts=datetime(2026, 9, 23, 15, 10))}
    env.clock.t = datetime(2026, 9, 23, 15, 10)
    path = env.run()
    assert path is not None and Path(path).exists()
    assert all(not e.ticks for e in FakeEngine.instances)
    assert loaded == [1]                                 # 晚启动也要加载股票池，为了给收盘价补全库存数据
    main_equity_check(env, 10.5)
    assert "600001 甲" in Path(path).read_text(encoding="utf-8")


def test_late_start_saves_closes_for_universe_codes(env, monkeypatch):
    hold(env, price=9.9)
    monkeypatch.setattr(run.akdata, "load_universe",
                        lambda today, days: {"600001": "甲", "600002": "乙"})
    env.feed["quotes"] = {
        "600001": quote("600001", pct=5.0, name="甲", ts=datetime(2026, 9, 23, 15, 10)),
        "600002": quote("600002", pct=1.0, name="乙", ts=datetime(2026, 9, 23, 15, 10)),
    }
    env.clock.t = datetime(2026, 9, 23, 15, 10)
    env.run()
    conn = db.connect(env.db)
    try:
        codes = {r[0] for r in conn.execute(
            "SELECT code FROM closes WHERE date='2026-09-23'").fetchall()}
    finally:
        conn.close()
    assert codes == {"600001", "600002"}                 # 不止持仓，全股票池也要存


def test_late_start_universe_load_failure_falls_back_to_held_only(env, monkeypatch):
    hold(env, price=9.9)

    def fail(today, days):
        raise RuntimeError("akshare down")

    monkeypatch.setattr(run.akdata, "load_universe", fail)
    env.feed["quotes"] = {"600001": quote("600001", pct=5.0, name="甲",
                                          ts=datetime(2026, 9, 23, 15, 10))}
    env.clock.t = datetime(2026, 9, 23, 15, 10)
    path = env.run()
    assert path is not None
    assert "晚启动股票池加载失败，收盘价只存持仓" in env.log_text()
    conn = db.connect(env.db)
    try:
        codes = {r[0] for r in conn.execute(
            "SELECT code FROM closes WHERE date='2026-09-23'").fetchall()}
    finally:
        conn.close()
    assert codes == {"600001"}


# ---- Task 19：本地收盘价库 ----

def test_insufficient_ret5_coverage_sets_warning_and_skips_buys(env, monkeypatch):
    monkeypatch.setattr(run.closes, "ret5_from_db", lambda conn, codes, today: {})
    seed_concepts(env.db)   # 题材数据本身是全的，这条测试只关心 ret5 覆盖率告警，不想被题材缺失的提示串进来
    state: dict = {}
    env.run(state=state)
    eng = FakeEngine.instances[0]
    assert env.boards[0].enabled is False                 # 题材是全的，只因 ret5 不够停买
    assert state.get("warning") == "5 日涨幅数据不足，今日只卖不买"
    assert "5 日涨幅覆盖不足（0/1），今日只卖不买" in env.log_text()
    assert len(eng.ticks) == 240                          # 只卖不买不代表停跑，照常盯盘


def test_report_saves_closes_before_writing_it(env):
    env.feed["quotes"] = {"600001": quote("600001", pct=2.0, name="甲",
                                          ts=datetime(2026, 9, 23, 15, 0))}
    env.run()
    conn = db.connect(env.db)
    try:
        row = conn.execute("SELECT close FROM closes WHERE date=? AND code='600001'",
                           ("2026-09-23",)).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == pytest.approx(10.2)
    assert "收盘价存库" in env.log_text()


def test_report_saves_day_volume_in_lots_with_close(env):
    env.feed["quotes"] = {"600001": quote("600001", pct=2.0, name="甲", volume=45678,
                                          ts=datetime(2026, 9, 23, 15, 0))}
    env.run()
    conn = db.connect(env.db)
    try:
        row = conn.execute("SELECT volume FROM closes WHERE date=? AND code='600001'",
                           ("2026-09-23",)).fetchone()
    finally:
        conn.close()
    assert row == (45678.0,)                              # Quote.volume 已是手，原样存


def test_stale_quote_is_not_saved_to_closes(env):
    # ts 是昨天的行情（比如刷新失败沿用的快照），不能当成今天的收盘价存库。
    env.feed["quotes"] = {"600001": quote("600001", pct=2.0, name="甲",
                                          ts=datetime(2026, 9, 22, 15, 0))}
    env.run()
    conn = db.connect(env.db)
    try:
        row = conn.execute("SELECT close FROM closes WHERE date=? AND code='600001'",
                           ("2026-09-23",)).fetchone()
    finally:
        conn.close()
    assert row is None


def test_backfill_flag_runs_without_jev_key(env, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    calls = []
    monkeypatch.setattr(run.akdata, "fetch_daily_sina",
                        lambda code, **kw: calls.append(code) or [])
    run.run_backfill(log=env.logs.append)
    assert "股票池 1 只" in env.log_text()
    assert "补数" in env.log_text()


def test_backfill_uses_sina_with_volume(env, monkeypatch):
    """补数走新浪日 K（腾讯 K 线被 WAF 封了），带量的行把成交量一起写进库。"""
    import sqlite3
    monkeypatch.delenv("TYPESAFE_API_KEY")
    rows = [("2026-09-%02d" % d, 10.0 + d / 10, 1000.0 + d) for d in range(10, 23)]
    monkeypatch.setattr(run.akdata, "fetch_daily_sina", lambda code, **kw: rows)
    monkeypatch.setattr(run.tencent, "fetch_kline",
                        lambda code, session=None: pytest.fail("不应再调用腾讯 K 线"))
    run.run_backfill(log=env.logs.append)
    conn = sqlite3.connect(env.db)
    vol = conn.execute("SELECT volume FROM closes WHERE code='600001' AND date='2026-09-22'").fetchone()
    conn.close()
    assert vol == (1022.0,)


def test_backfill_flag_holds_runner_lock(env):
    with hold_run_lock(Path(env.db).parent / "runner.lock"):
        with pytest.raises(RuntimeError, match="已有另一个交易进程在运行"):
            run.run_backfill(log=env.logs.append)


# ---- Task 6：run_day 接入 BoardStrategy（每分钟 + 3 秒扫板）----

def test_next_wake_no_watch_returns_full_remainder():
    t = datetime(2026, 9, 23, 9, 30, 0)
    assert run.next_wake(t, False) == 60.0


def test_next_wake_no_watch_matches_plain_remaining_seconds():
    t = datetime(2026, 9, 23, 9, 30, 37)
    assert run.next_wake(t, False) == 23.0


def test_next_wake_with_watch_returns_fast_interval():
    t = datetime(2026, 9, 23, 9, 30, 0)          # 到下一分钟边界还有 62 秒，远大于 3 秒节拍
    assert run.next_wake(t, True) == 3.0


def test_next_wake_near_boundary_caps_at_remainder():
    t = datetime(2026, 9, 23, 9, 31, 59)          # 距下一分钟只剩 1 秒
    assert run.next_wake(t, True) == 1.0


def test_board_watch_state_lists_watch_codes_with_names():
    class FakeBoard:
        watch = {"600002": Candidate("600002", "题材A", 3, 1, {}),
                 "600001": Candidate("600001", "题材A", 3, 2, {})}

    result = run._board_watch_state(FakeBoard(), {"600001": "甲", "600002": "乙"})
    assert result == [{"code": "600001", "name": "甲"}, {"code": "600002", "name": "乙"}]


def test_empty_concepts_disables_board_and_warns(env):
    state: dict = {}
    env.run(state=state)
    assert "题材数据缺失，扫首板今日只卖不买" in state.get("warning", "")
    assert "题材数据缺失，扫首板今日只卖不买" in env.log_text()
    assert env.boards[0].enabled is False
    assert len(FakeEngine.instances[0].ticks) == 240


def test_concepts_present_but_ret5_short_still_disables_board(env, monkeypatch):
    seed_concepts(env.db)
    monkeypatch.setattr(run.closes, "ret5_from_db", lambda conn, codes, today: {})
    state: dict = {}
    env.run(state=state)
    assert env.boards[0].enabled is False
    # 这条只是 ret5 不够，不是题材缺失，不该重复提示题材数据缺失
    assert "题材数据缺失" not in state.get("warning", "")


def test_concepts_and_ret5_both_present_enables_board(env):
    seed_concepts(env.db)
    env.run()
    assert env.boards[0].enabled is True


def test_board_on_minute_exception_is_logged_and_day_continues(env):
    seed_concepts(env.db)
    SpyBoard.boom_once = True
    path = env.run()
    assert "扫首板本轮出错：RuntimeError('board boom')" in env.log_text()
    assert len(FakeEngine.instances[0].ticks) == 240   # 引擎取行情不受影响
    assert path is not None and Path(path).exists()


def test_calendar_failure_disables_board_stale_before(env, monkeypatch):
    def fail():
        raise TimeoutError("sina 超时")

    monkeypatch.setattr(run.akdata, "trade_days", fail)
    env.run()
    assert env.boards[0].stale_before is None
    assert "交易日历不可用：停用「隔夜超期」规则" in env.log_text()


def test_concept_refresh_runs_when_stale_and_failure_is_logged_only(env, monkeypatch):
    monkeypatch.setattr(run.concepts, "needs_refresh", lambda conn, today: True)
    calls = []

    def fail(conn, today, **kw):
        calls.append((today, kw))
        raise RuntimeError("akshare down")

    monkeypatch.setattr(run.concepts, "refresh_concepts", fail)
    path = env.run()
    assert len(calls) == 1 and calls[0][0] == date(2026, 9, 23)
    # deadline 是 09:25，且接了 stop，这样刷新慢了/收到停止指令都能提前退出，不顶着开盘时间
    assert calls[0][1]["deadline"] == datetime(2026, 9, 23, 9, 25)
    assert calls[0][1]["stop"] is not None
    assert "题材刷新失败：RuntimeError('akshare down')" in env.log_text()
    assert path is not None and len(FakeEngine.instances[0].ticks) == 240


def test_concept_refresh_not_attempted_when_not_stale(env, monkeypatch):
    calls = []
    monkeypatch.setattr(run.concepts, "needs_refresh", lambda conn, today: False)
    monkeypatch.setattr(run.concepts, "refresh_concepts", lambda *a, **kw: calls.append(1) or 0)
    env.run()
    assert calls == []


def test_late_start_does_not_load_concepts_or_build_board(env, monkeypatch):
    hold(env, price=9.9)
    monkeypatch.setattr(run.akdata, "load_universe", lambda today, days: {})
    env.feed["quotes"] = {"600001": quote("600001", pct=5.0, name="甲",
                                          ts=datetime(2026, 9, 23, 15, 10))}
    env.clock.t = datetime(2026, 9, 23, 15, 10)
    needs_refresh_calls = []
    monkeypatch.setattr(run.concepts, "needs_refresh", lambda conn, today: needs_refresh_calls.append(1) or False)
    path = env.run()
    assert path is not None
    assert needs_refresh_calls == []          # 晚启动不读/刷题材
    assert SpyBoard.instances == []            # 也不构造 BoardStrategy


def test_load_concepts_failure_disables_board_and_logs(env, monkeypatch):
    seed_concepts(env.db)
    monkeypatch.setattr(run.concepts, "load_concepts", lambda conn: (_ for _ in ()).throw(RuntimeError("db locked")))
    path = env.run()
    assert "题材读取失败" in env.log_text()
    assert env.boards[0].enabled is False
    assert path is not None and len(FakeEngine.instances[0].ticks) == 240


def test_prev_closes_failure_disables_board_and_logs(env, monkeypatch):
    seed_concepts(env.db)
    monkeypatch.setattr(run.closes, "prev_closes_from_db",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("db locked")))
    path = env.run()
    assert "昨收/前日收读取失败" in env.log_text()
    assert env.boards[0].enabled is False
    assert path is not None and len(FakeEngine.instances[0].ticks) == 240


def test_on_fast_exception_is_isolated_and_ticks_continue(env):
    seed_concepts(env.db)
    cand = Candidate("600001", "题材A", 3, 1, {"pct": 1, "amount": 1, "ret5": 1})
    SpyBoard.force_watch_once = {"600001": cand}
    SpyBoard.on_fast_boom_once = True
    path = env.run()
    assert "扫板出错：RuntimeError('on_fast boom')" in env.log_text()
    assert len(SpyBoard.fast_call_times) > 1        # 炸完之后循环继续跑，不是就此死掉
    assert len(FakeEngine.instances[0].ticks) == 240
    assert path is not None


def test_fast_scan_pushes_board_main_fill_and_does_not_log_error(monkeypatch):
    """回归测试：_fast_scan 曾经把 acct_names 当成 _run_day 的局部变量直接用，实际上它不在
    _fast_scan 的作用域里，一 NameError 就被 except 吞掉，扫板成交推送悄悄丢失（只在日志里
    看得到，桌面面板和微信都收不到）。这里直接单测 _fast_scan：board_main 的成交必须真的
    推送出去，并且不能出现「扫板出错」日志。"""
    monkeypatch.setattr(run, "now", lambda: datetime(2026, 9, 23, 10, 0, 0))
    monkeypatch.setattr(run, "_tick_wait", lambda seconds, stop: False)

    calls = {"n": 0}

    class FakeBoard:
        windows = SCAN_WINDOWS
        watch = {"600001": object()}

        def has_watch(self):
            calls["n"] += 1
            return calls["n"] == 1          # 只在循环第一轮进 on_fast，第二轮退出循环

        def on_fast(self, now):
            return [Fill("board_main", "600001", "buy", 10.0, 1000, 5.0, now, "扫板")]

    class FakeNotifier:
        def __init__(self):
            self.sent = []

        def send(self, title, content):
            self.sent.append((title, content))

    conn = db.connect(":memory:")
    logs: list[str] = []
    notifier = FakeNotifier()
    acct_names = {"board_main": "首板·主(Jev)"}
    stopped = run._fast_scan(FakeBoard(), threading.Event(), logs.append, {"names": {"600001": "甲"}},
                             notifier, conn, acct_names)
    assert stopped is False
    assert not any("扫板出错" in line for line in logs)          # acct_names NameError 不再发生
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] == "【首板·主(Jev) 买入】甲 @10.0"


def test_once_mode_shares_same_t_for_engine_and_board_tick(env, monkeypatch):
    seed_concepts(env.db)
    seen = {}

    class RecordingBoard(SpyBoard):
        def sell_step(self, quotes, now):
            seen["board_t"] = now
            return super().sell_step(quotes, now)

    monkeypatch.setattr(run, "BoardStrategy", RecordingBoard)
    orig_tick = FakeEngine.sell_step

    def recording_tick(self, now):
        seen["engine_t"] = now
        return orig_tick(self, now)

    monkeypatch.setattr(FakeEngine, "sell_step", recording_tick)
    env.run(once=True)
    assert seen["board_t"] == seen["engine_t"]


def test_fast_scan_survives_on_fast_latency_no_minute_skipped_or_doubled(env):
    """真实场景里 on_fast 会花点真实时间；固定 deadline 之前的老实现会在这种情况下把
    deadline 越推越远，导致好几个整分钟的 tick（连带各策略的卖出）被跳过。"""
    seed_concepts(env.db)
    cand = Candidate("600001", "题材A", 3, 1, {"pct": 1, "amount": 1, "ret5": 1})
    SpyBoard.force_watch = {"600001": cand}
    SpyBoard.on_fast_latency = 0.5
    env.run()
    eng = FakeEngine.instances[0]
    assert len(eng.ticks) == 240                       # 一分钟都没被吞掉
    minutes = [t.replace(second=0, microsecond=0) for t in eng.ticks]
    assert len(minutes) == len(set(minutes))            # 也没有哪一分钟被吃两次


def test_watchlist_minute_tick_keeps_two_second_buffer(env):
    seed_concepts(env.db)
    cand = Candidate("600001", "题材A", 3, 1, {"pct": 1, "amount": 1, "ret5": 1})
    SpyBoard.force_watch = {"600001": cand}
    env.run()
    eng = FakeEngine.instances[0]
    assert len(eng.ticks) == 240
    # 第一笔 tick 是在观察池还是空的时候打的（09:30:00 整点）；从下一笔起观察池已经非空，
    # fast_scan 固定在 :02 边界交回控制权，tick 时间应该带着这个缓冲，不会退化成整点 :00。
    assert all(t.second >= 2 for t in eng.ticks[1:])


def test_watch_triggers_multiple_fast_scans_within_one_minute(env):
    seed_concepts(env.db)
    cand = Candidate("600001", "题材A", 3, 1, {"pct": 1, "amount": 1, "ret5": 1})
    SpyBoard.force_watch_once = {"600001": cand}
    env.run()
    per_minute = Counter(t.replace(second=0, microsecond=0) for t in SpyBoard.fast_call_times)
    assert per_minute                                    # on_fast 至少被调用过
    assert max(per_minute.values()) > 1                   # 且同一分钟内不止一次


# ---- Task 7：封板时段统计（收盘写报告前调用 fill_next_day / record_day）----

def seed_board_event(dbp, day, code="600001", first_seal="093000"):
    conn = db.connect(dbp)
    be.record_day(conn, day, [{"代码": code, "名称": "甲", "首次封板时间": first_seal, "连板数": 1}], {})
    conn.close()


def test_close_step_fills_prev_day_and_records_today_first_boards(env):
    seed_board_event(env.db, date(2026, 9, 22), code="600001")
    env.feed["quotes"] = {"600001": quote("600001", pct=2.0, pre_close=10.0,
                                          ts=datetime(2026, 9, 23, 15, 0))}
    env.feed_zt["rows"] = pd.DataFrame([
        {"代码": "600002", "名称": "乙", "首次封板时间": "093500", "连板数": 1},
    ])
    env.run()
    conn = db.connect(env.db)
    try:
        filled = conn.execute(
            "SELECT next_close_pct FROM board_events WHERE date='2026-09-22' AND code='600001'").fetchone()[0]
        today_codes = [r[0] for r in conn.execute(
            "SELECT code FROM board_events WHERE date='2026-09-23'").fetchall()]
    finally:
        conn.close()
    assert filled == pytest.approx(2.0)
    assert today_codes == ["600002"]
    assert "封板统计回填 1 条" in env.log_text()
    assert "封板统计记录 1 只首板" in env.log_text()


def test_late_start_also_fills_and_records_board_events(env, monkeypatch):
    hold(env, price=9.9)
    seed_board_event(env.db, date(2026, 9, 22), code="600001")
    monkeypatch.setattr(run.akdata, "load_universe", lambda today, days: {})
    env.feed["quotes"] = {"600001": quote("600001", pct=5.0, name="甲",
                                          ts=datetime(2026, 9, 23, 15, 10))}
    env.feed_zt["rows"] = pd.DataFrame([
        {"代码": "600003", "名称": "丙", "首次封板时间": "140500", "连板数": 1},
    ])
    env.clock.t = datetime(2026, 9, 23, 15, 10)
    env.run()
    conn = db.connect(env.db)
    try:
        filled = conn.execute(
            "SELECT next_close_pct FROM board_events WHERE date='2026-09-22' AND code='600001'").fetchone()[0]
        today_codes = [r[0] for r in conn.execute(
            "SELECT code FROM board_events WHERE date='2026-09-23'").fetchall()]
    finally:
        conn.close()
    assert filled is not None
    assert today_codes == ["600003"]


def test_board_events_failures_do_not_block_report(env, monkeypatch):
    monkeypatch.setattr(run.akdata, "fetch_zt_pool",
                        lambda d: (_ for _ in ()).throw(RuntimeError("zt down")))
    monkeypatch.setattr(run.board_events, "fill_next_day",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fill down")))
    path = env.run()
    assert path is not None and Path(path).exists()
    assert "封板统计回填失败：RuntimeError('fill down')" in env.log_text()
    assert "封板统计记录失败：RuntimeError('zt down')" in env.log_text()


# ---- final fix wave ----

def seed_turnover(dbp, ts):
    conn = db.connect(dbp)
    conn.execute("INSERT OR REPLACE INTO turnover VALUES (?, ?)", (ts, 1.0))
    conn.commit()
    conn.close()


def test_ran_on_detects_turnover_rows_of_that_day(env):
    seed_turnover(env.db, "2026-09-22 14:59")
    conn = db.connect(env.db)
    try:
        assert run.ran_on(conn, date(2026, 9, 22)) is True
        assert run.ran_on(conn, date(2026, 9, 21)) is False
    finally:
        conn.close()


def test_ran_yesterday_disables_board_stale_rule(env):
    seed_turnover(env.db, "2026-09-22 10:00")
    env.run()
    assert env.boards[0].stale_before is None


def test_missed_yesterday_keeps_board_stale_rule(env):
    seed_turnover(env.db, "2026-09-21 10:00")               # 前天跑过，昨天没跑
    env.run()
    assert env.boards[0].stale_before == date(2026, 9, 22)


def test_board_gets_empty_quotes_when_minute_fetch_failed(env, monkeypatch):
    from datetime import time as dtime
    seed_concepts(env.db)
    conn = db.connect(env.db)
    Ledger(conn).buy("board_main", quote("600001", pct=1.0, ts=datetime(2026, 9, 22, 10, 0)),
                     datetime(2026, 9, 22, 10, 0), "x")
    conn.close()
    seen = []

    class RecordingBoard(SpyBoard):
        def sell_step(self, quotes, now):
            seen.append((now.time().replace(second=0), dict(quotes)))
            return super().sell_step(quotes, now)

    monkeypatch.setattr(run, "BoardStrategy", RecordingBoard)

    def behavior(eng, now):
        if now.time() < dtime(9, 31):
            # 09:30 取数成功
            eng.last_quotes = {"600001": quote("600001", pct=1.0, ts=now)}
            eng.last_fetch_minute = now.strftime("%Y-%m-%d %H:%M")
        elif now.time() < dtime(9, 32):
            # 09:31 取数失败：last_quotes 保持 09:30 的旧快照，这里篡改成会触发止损的价格，
            # 用来证明 board 没拿它去卖
            eng.last_quotes = {"600001": quote("600001", pct=-5.0, ts=now.replace(minute=30))}
        else:
            eng.last_quotes = {"600001": quote("600001", pct=1.0, ts=now)}
            eng.last_fetch_minute = now.strftime("%Y-%m-%d %H:%M")

    FakeEngine.behavior = behavior
    env.run()
    by_minute = dict(seen)
    assert by_minute[dtime(9, 30)] != {}
    assert by_minute[dtime(9, 31)] == {}
    assert by_minute[dtime(9, 32)] != {}
    conn = db.connect(env.db)
    try:
        sells = conn.execute("SELECT COUNT(*) FROM fills WHERE account='board_main' AND side='sell' "
                             "AND reason='止损'").fetchone()[0]
    finally:
        conn.close()
    assert sells == 0


def test_missing_yesterday_close_disables_board_with_warning(env, monkeypatch):
    seed_concepts(env.db)
    conn = db.connect(env.db)
    conn.execute("DELETE FROM closes WHERE date='2026-09-22'")     # 昨天（prev_day）没存收盘价
    conn.commit()
    conn.close()
    monkeypatch.setattr(run.closes, "ret5_from_db", lambda conn, codes, today: {"600001": 5.0})
    state: dict = {}
    path = env.run(state=state)
    assert env.boards[0].enabled is False
    assert "昨日收盘价缺失，扫首板今日只卖不买" in state.get("warning", "")
    assert "昨日收盘价缺失，扫首板今日只卖不买" in env.log_text()
    assert path is not None and len(FakeEngine.instances[0].ticks) == 240


def test_board_gets_prev_prev_closes_from_calendar(env):
    seed_concepts(env.db)
    env.run()
    assert env.boards[0].enabled is True
    assert env.boards[0].prev_prev_closes == {"600001": 10.0}     # 09-21 的收盘价


def test_fast_scan_only_inside_scan_windows(env):
    from datetime import time as dtime
    seed_concepts(env.db)
    cand = Candidate("600001", "题材A", 3, 1, {"pct": 1, "amount": 1, "ret5": 1})
    SpyBoard.force_watch = {"600001": cand}               # 观察池全天非空
    env.run()
    assert SpyBoard.fast_call_times                         # 09:30–10:30 有快扫
    assert all(dtime(9, 30) <= t.time() < dtime(10, 30) for t in SpyBoard.fast_call_times)
    assert len(FakeEngine.instances[0].ticks) == 240


def test_once_mode_concept_refresh_deadline_is_ten_minutes_from_now(env, monkeypatch):
    monkeypatch.setattr(run.concepts, "needs_refresh", lambda conn, today: True)
    calls = []
    monkeypatch.setattr(run.concepts, "refresh_concepts", lambda conn, today, **kw: calls.append(kw) or 0)
    env.clock.t = datetime(2026, 9, 23, 13, 40)
    env.run(once=True)
    assert calls[0]["deadline"] == datetime(2026, 9, 23, 13, 50)


# ---- 自定义策略接入（rule builder Task 6） ----

CUSTOM_SPEC = {
    "name": "测试规则", "capital": 50000,
    "select": {"mode": "all", "conditions": [{"field": "pct", "op": ">=", "value": 9}]},
    "entry": {"mode": "all", "conditions": []},
    "exits": [{"when": {"mode": "all", "conditions": [{"field": "pct", "op": "<=", "value": -5}]},
               "reason": "止损"}],
    "jev": {"notices": False, "news": False},
}


def seed_custom(env, name="测试规则", saved=date(2026, 9, 21)) -> str:
    from jevq.data import custom_store
    conn = db.connect(env.db)
    try:
        sid, _version = custom_store.save(conn, dict(CUSTOM_SPEC, name=name), saved, DAYS)
    finally:
        conn.close()
    assert sid is not None
    return sid


def feeding_tick(eng, now):
    """模拟真实 Engine._fetch：按 accounts_provider 把全部账户持仓的票一起拉行情。"""
    accounts = eng.accounts_provider() if eng.accounts_provider else ()
    held = {p.code for a in accounts for p in eng.ledger.positions(a)}
    eng.last_quotes = eng.fetch_quotes(sorted(set(eng.universe) | held))
    eng.last_fetch_minute = now.strftime("%Y-%m-%d %H:%M")


class FakeCustom:
    instances: list = []
    boom: set = set()

    def __init__(self, conn, ledger, sid, version, spec, notice_judge, news_judge, universe, features,
                 concept_map, env_provider, prev_day, can_buy=True, trade_days=None):
        self.conn, self.sid, self.version, self.can_buy = conn, sid, version, can_buy
        self.calls, self.buy_calls = [], []
        self.trade_days, self.env_provider = trade_days, env_provider
        for account in (f"{sid}_main", f"{sid}_shadow"):          # 与真实 CustomStrategy 一样开户
            ledger.ensure_account(account, spec["capital"])
        FakeCustom.instances.append(self)

    def sell_step(self, quotes, now):
        self.calls.append((now, bool(quotes)))
        if self.sid in FakeCustom.boom:
            self.conn.execute("INSERT INTO turnover VALUES ('custom-half', 1)")
            raise RuntimeError("custom boom")
        return []

    def buy_step(self, quotes, now, deadline=None):
        self.buy_calls.append((now, deadline))
        return []


@pytest.fixture
def fake_custom(monkeypatch):
    FakeCustom.instances, FakeCustom.boom = [], set()
    monkeypatch.setattr(run, "CustomStrategy", FakeCustom)
    return FakeCustom


def test_custom_strategy_exception_does_not_affect_others_or_builtin(env, fake_custom):
    seed_custom(env, "一号")
    seed_custom(env, "二号")
    fake_custom.boom = {"c1"}
    FakeEngine.behavior = feeding_tick
    env.feed["quotes"] = {"600001": quote("600001", pct=-1, ts=datetime(2026, 9, 23, 10, 0))}
    path = env.run()
    assert path is not None and Path(path).exists(), env.log_text()
    assert len(FakeEngine.instances[0].ticks) == 240                  # 引擎照常每分钟取行情
    by_sid = {s.sid: s for s in fake_custom.instances}
    assert set(by_sid) == {"c1", "c2"}
    assert by_sid["c2"].trade_days == DAYS                            # 交易日历传给自定义策略（持有天数用）
    assert len(by_sid["c2"].calls) == 240                             # 另一个自定义策略照常每分钟
    assert all(has_quotes for _, has_quotes in by_sid["c2"].calls)
    assert len(by_sid["c1"].calls) == 240                             # 出错的下一分钟继续跑
    assert "自定义策略 c1 本轮出错：RuntimeError('custom boom')" in env.log_text()
    conn = db.connect(env.db)
    assert conn.execute("SELECT COUNT(*) FROM turnover WHERE ts='custom-half'").fetchone()[0] == 0
    conn.close()


def test_disabled_custom_with_holdings_loaded_sell_only(env, fake_custom):
    from jevq.data import custom_store
    seed_custom(env, "停用的")
    seed_custom(env, "在跑的")
    conn = db.connect(env.db)
    custom_store.disable(conn, "c1", date(2026, 9, 22), DAYS)       # 09-23 起停用
    ledger = Ledger(conn)
    ledger.ensure_account("c1_main", 50000)
    ledger.buy("c1_main", quote("600002"), datetime(2026, 9, 22, 10, 0), "x")
    conn.close()
    env.run(once=True)
    loaded = {(s.sid, s.version, s.can_buy) for s in fake_custom.instances}
    assert loaded == {("c1", 1, False), ("c2", 1, True)}


def test_custom_strategy_not_double_driven_and_can_buy_false_in_sell_only(env, fake_custom, monkeypatch):
    seed_custom(env)
    monkeypatch.delenv("TYPESAFE_API_KEY")
    conn = db.connect(env.db)
    Ledger(conn).buy(HOLD_ACCOUNT, quote("600001"), datetime(2026, 9, 22, 10, 0), "x")   # 有持仓才允许无 key 运行
    conn.close()
    env.run(once=True)
    assert [(s.sid, s.can_buy) for s in fake_custom.instances] == [("c1", False)]
    assert len(fake_custom.instances[0].calls) == 1


def test_custom_holding_outside_universe_is_sold_and_pushed(env):
    seed_custom(env)
    conn = db.connect(env.db)
    ledger = Ledger(conn)
    ledger.ensure_account("c1_main", 50000)
    ledger.buy("c1_main", quote("600002"), datetime(2026, 9, 22, 10, 0), "x")     # 600002 不在股票池
    conn.close()
    FakeEngine.behavior = feeding_tick
    env.feed["quotes"] = {"600001": quote("600001", pct=-1, ts=datetime(2026, 9, 23, 9, 29)),
                          "600002": quote("600002", pct=-6, ts=datetime(2026, 9, 23, 9, 29))}

    class FakeNotifier:
        def __init__(self):
            self.sent = []

        def send(self, title, content):
            self.sent.append((title, content))

        def once(self, *a):
            pass

    notifier = FakeNotifier()
    env.run(once=True, notifier=notifier)
    conn = db.connect(env.db)
    try:
        assert Ledger(conn).positions("c1_main") == []
        reasons = [r for (r,) in conn.execute("SELECT reason FROM fills WHERE account='c1_main' AND side='sell'")]
    finally:
        conn.close()
    assert reasons == ["止损"]
    assert any(t.startswith("【测试规则·主(Jev) 卖出】") for t, _ in notifier.sent)


# ---- 公告正文预抓 ----


def test_prefetch_bodies_runs_after_each_notice_sync(env, monkeypatch):
    events = []
    monkeypatch.setattr(run, "sync_notices", lambda conn, today: events.append("sync") or 0)
    monkeypatch.setattr(run, "prefetch_bodies", lambda conn, today, codes, budget: events.append(
        ("prefetch", today, list(codes), budget)) or 2)
    path = env.run()
    pre = ("prefetch", date(2026, 9, 23), ["600001"], config.PREFETCH_BUDGET)
    mid = ("prefetch", date(2026, 9, 23), ["600001"], config.PREFETCH_BUDGET_MIDDAY)
    assert events == ["sync", pre, "sync", mid]          # 盘前 + 13:00 重同步后各预抓一次，午间等得更短
    assert config.PREFETCH_BUDGET_MIDDAY < config.PREFETCH_BUDGET
    assert path is not None
    assert "公告正文预抓 2 条" in env.log_text()


def test_prefetch_bodies_failure_is_logged_and_day_continues(env, monkeypatch):
    def fail(conn, today, codes, **kw):
        raise RuntimeError("body down")

    monkeypatch.setattr(run, "prefetch_bodies", fail)
    path = env.run()
    assert len(FakeEngine.instances[0].ticks) == 240 and path is not None
    assert "公告正文预抓失败" in env.log_text() and "body down" in env.log_text()


# ---- Task 4b：每分钟先让所有策略卖完再碰 Jev；买入阶段共用一个 Jev 时间预算 ----

class SentimentMarket(FakeMarket):
    def __init__(self, jev):
        super().__init__(jev)
        self.sentiment_trace = None

    def sentiment(self, stats):
        return "正常"


class ClockJudge:
    """假 Jev 判断：每次 check 记一笔事件、把假 monotonic 拨快 step 秒（模拟慢 Jev），全部否决。"""
    def __init__(self, events, clock, step, verdict=("reject", "慢判否决")):
        self.events, self.clock, self.step, self.verdict = events, clock, step, verdict
        self.calls, self.last_trace = [], None

    def check(self, code, *args):
        self.calls.append(code)
        self.events.append(("judge", code))
        self.clock[0] += self.step
        return self.verdict


REBOUND_CLOSES = [10.0, 10.0, 10.0, 10.0, 10.0, 9.5]     # 近 5 日 -5%：落在超跌反弹的候选区间


def _real_engine(env, monkeypatch, events, clock, step, codes=("600001",)):
    """真实 Engine + 真实超跌反弹；情绪、公告排雷都记事件，排雷每次把假 monotonic 拨快 step 秒。"""
    from jevq.engine import Engine as RealEngine
    import jevq.strategies.rebound as rebound_mod

    class Market(SentimentMarket):
        def sentiment(self, stats):
            events.append(("sentiment",))
            return super().sentiment(stats)

    monkeypatch.setattr(run, "Engine", RealEngine)
    monkeypatch.setattr(run, "MarketJudge", Market)
    monkeypatch.setattr(run.akdata, "fetch_pool_stats", lambda d: {})
    monkeypatch.setattr(run, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rebound_mod, "monotonic", lambda: clock[0])
    judge = ClockJudge(events, clock, step)
    monkeypatch.setattr(run, "NoticeJudge", lambda conn, jev: judge)
    monkeypatch.setattr(run, "NewsJudge", lambda conn, jev: ClockJudge(events, clock, 0.0, ("pass", "ok")))
    monkeypatch.setattr(run.akdata, "load_universe", lambda today, days: {c: c for c in codes})
    monkeypatch.setattr(config, "REBOUND_MIN_SHRINK_DAYS", 0)     # 这些测试看的是 Jev 预算/卖出顺序，不看成交量条件
    for c in codes:
        seed_closes(env.db, c, REBOUND_CLOSES)
    return judge


def test_slow_jev_in_buy_phase_does_not_delay_board_and_custom_sells(env, monkeypatch):
    monkeypatch.setattr("jevq.config.REBOUND_MAIN_RET5_MAX", 0.0)    # 测 Jev 时限，不测 10-14 的主账户跌幅门槛
    """买入阶段的 Jev（引擎刷新情绪、超跌反弹排雷）再慢，同一分钟里扫首板、自定义策略的卖出也已经做完。"""
    events, clock = [], [0.0]
    judge = _real_engine(env, monkeypatch, events, clock, step=30.0)
    seed_custom(env)                                              # 规则：跌到 -5% 止损
    conn = db.connect(env.db)
    ledger = Ledger(conn)
    ledger.ensure_account("c1_main", 50000)
    ledger.buy("board_main", quote("600002", ts=datetime(2026, 9, 22, 10, 0)), datetime(2026, 9, 22, 10, 0), "x")
    ledger.buy("c1_main", quote("600003", ts=datetime(2026, 9, 22, 10, 0)), datetime(2026, 9, 22, 10, 0), "x")
    conn.close()
    from jevq.ledger import Ledger as LedgerCls
    orig_sell = LedgerCls.sell

    def recording_sell(self, account, pos, q, now, reason):
        events.append(("sell", account))
        return orig_sell(self, account, pos, q, now, reason)

    monkeypatch.setattr(LedgerCls, "sell", recording_sell)
    ts = datetime(2026, 9, 23, 10, 0)
    env.feed["quotes"] = {"600001": quote("600001", pct=-1.0, ts=ts),
                          "600002": quote("600002", pct=-6.0, ts=ts),
                          "600003": quote("600003", pct=-6.0, ts=ts)}
    env.clock.t = ts
    env.run(once=True)
    assert judge.calls == ["600001"]                              # 超跌反弹确实问了（慢）Jev
    first_jev = events.index(("sentiment",))
    assert first_jev < events.index(("judge", "600001"))          # 同一分钟先刷情绪，超跌反弹才判
    sold = [i for i, e in enumerate(events) if e[0] == "sell"]
    assert {events[i][1] for i in sold} == {"board_main", "c1_main"}
    assert all(i < first_jev for i in sold)


def test_every_minute_all_sells_run_before_any_buy_phase(env, fake_custom, monkeypatch):
    seed_concepts(env.db)
    seed_custom(env, "一号")
    seed_custom(env, "二号")
    events = []

    class OrderBoard(SpyBoard):
        def sell_step(self, quotes, now):
            events.append((now, "sell", "board"))
            return super().sell_step(quotes, now)

        def buy_step(self, quotes, now, deadline=None):
            events.append((now, "buy", "board"))
            return super().buy_step(quotes, now, deadline)

    monkeypatch.setattr(run, "BoardStrategy", OrderBoard)
    FakeEngine.behavior = lambda eng, now: events.append((now, "sell", "engine"))
    orig_buy = FakeEngine.buy_step
    monkeypatch.setattr(FakeEngine, "buy_step",
                        lambda self, now: events.append((now, "buy", "engine")) or orig_buy(self, now))
    orig_c_sell, orig_c_buy = FakeCustom.sell_step, FakeCustom.buy_step
    monkeypatch.setattr(FakeCustom, "sell_step", lambda self, q, now: events.append(
        (now, "sell", self.sid)) or orig_c_sell(self, q, now))
    monkeypatch.setattr(FakeCustom, "buy_step", lambda self, q, now, deadline=None: events.append(
        (now, "buy", self.sid)) or orig_c_buy(self, q, now, deadline))
    env.run()
    minutes = sorted({t for t, _, _ in events})
    assert len(minutes) == 240
    for m in minutes:
        seq = [(phase, who) for t, phase, who in events if t == m]
        assert seq == [("sell", "engine"), ("sell", "board"), ("sell", "c1"), ("sell", "c2"),
                       ("buy", "board"), ("buy", "engine"), ("buy", "c1"), ("buy", "c2")]


def test_buy_phases_share_one_deadline_computed_after_sells(env, fake_custom, monkeypatch):
    seed_concepts(env.db)
    seed_custom(env)
    clock = [1000.0]
    monkeypatch.setattr(run, "monotonic", lambda: clock[0])
    FakeEngine.behavior = lambda eng, now: clock.__setitem__(0, clock[0] + 7.0)   # 卖出阶段花了 7 秒
    deadlines = []

    class DeadlineBoard(SpyBoard):
        def buy_step(self, quotes, now, deadline=None):
            deadlines.append(("board", deadline))
            return super().buy_step(quotes, now, deadline)

    monkeypatch.setattr(run, "BoardStrategy", DeadlineBoard)
    env.clock.t = datetime(2026, 9, 23, 10, 0)
    env.run(once=True)
    eng, custom = FakeEngine.instances[0], fake_custom.instances[0]
    expected = 1007.0 + config.TICK_JEV_BUDGET                    # 从卖完那一刻起算
    assert deadlines == [("board", expected)]
    assert custom.buy_calls[0][1] == expected
    assert len(eng.buy_calls) == 1                                # 引擎买入阶段只刷新情绪，不吃预算
    assert config.TICK_JEV_BUDGET == 40.0


def test_sell_phase_error_skips_only_that_strategys_buy_phase(env, fake_custom):
    seed_concepts(env.db)
    seed_custom(env, "一号")
    seed_custom(env, "二号")
    fake_custom.boom = {"c1"}
    env.clock.t = datetime(2026, 9, 23, 10, 0)
    env.run(once=True)
    by_sid = {s.sid: s for s in fake_custom.instances}
    assert by_sid["c1"].buy_calls == []                           # 卖出出错：这一分钟不买（同旧「整轮作废」）
    assert len(by_sid["c2"].buy_calls) == 1
    assert len(FakeEngine.instances[0].buy_calls) == 1
    assert "自定义策略 c1 本轮出错：RuntimeError('custom boom')" in env.log_text()


def test_tick_jev_budget_exhausted_candidates_checked_next_minute(env, monkeypatch):
    monkeypatch.setattr("jevq.config.REBOUND_MAIN_RET5_MAX", 0.0)    # 测 Jev 时限，不测 10-14 的主账户跌幅门槛
    """预算用完：超跌反弹剩下的候选这一分钟不判（不记否决/错误），下一分钟接着判。
    09:30 就能判，说明同一分钟引擎先刷好了情绪（没有情绪超跌反弹只在尾盘买）；情绪每 10 分钟刷一次。"""
    events, clock = [], [0.0]
    codes = ("600001", "600002", "600003")
    judge = _real_engine(env, monkeypatch, events, clock, step=30.0, codes=codes)
    env.feed["quotes"] = {c: quote(c, pct=-1.0, ts=datetime(2026, 9, 23, 9, 29)) for c in codes}
    path = env.run()
    assert path is not None, env.log_text()
    assert sorted(judge.calls) == sorted(codes) and len(judge.calls) == 3     # 每只只判一次
    conn = db.connect(env.db)
    try:
        rows = conn.execute("SELECT code, verdict, substr(ts, 12, 5) FROM decisions "
                            "WHERE strategy='rebound' AND code != '' ORDER BY id").fetchall()
        refreshed = [m for (m,) in conn.execute(
            "SELECT substr(ts, 12, 5) FROM decisions WHERE verdict='sentiment' AND strategy='market' ORDER BY id")]
    finally:
        conn.close()
    # 09:30：0→30、30→60，第三只时已过 40 秒预算；09:31 新预算里判第三只
    assert [(c, v) for c, v, _ in rows] == [(c, "reject") for c in judge.calls]
    assert [m for _, _, m in rows] == ["09:30", "09:30", "09:31"]
    assert refreshed[:3] == ["09:30", "09:40", "09:50"] and len(refreshed) == 24 == events.count(("sentiment",))


def test_missing_trade_day_in_closes_is_backfilled_before_open(env, monkeypatch):
    """后台 9/14 没跑：库里整天没有 9/14 的收盘价。盘前按交易日历找出缺口，对全股票池补那一天。"""
    days = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18),
            date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]
    monkeypatch.setattr(run.akdata, "trade_days", lambda: days)
    seed_concepts(env.db)
    calls = []
    monkeypatch.setattr(run.akdata, "fetch_daily_sina",
                        lambda code, **kw: calls.append(code) or [("2026-09-14", 9.9, 1200.0)])
    env.clock.t = datetime(2026, 9, 23, 8, 50)             # 后台 08:50 启动；缺口补数 09:05 收手

    def wait_until(t, stop):
        env.clock.t = max(env.clock.t, datetime.combine(env.clock.t.date(), t))
        return True

    monkeypatch.setattr(run, "wait_until", wait_until)

    class Clock(datetime):                                    # backfill_closes 用 datetime.now() 判 deadline
        @classmethod
        def now(cls, tz=None):
            return env.clock.t

    monkeypatch.setattr(run.closes, "datetime", Clock)
    state: dict = {}
    env.run(state=state)
    assert calls == ["600001"]
    assert "收盘价缺口 1 天：2026-09-14" in env.log_text()
    assert "不完整" not in (state.get("warning") or "")
    conn = db.connect(env.db)
    try:
        assert conn.execute("SELECT close, volume FROM closes WHERE code='600001' AND date='2026-09-14'"
                            ).fetchone() == (9.9, 1200.0)
    finally:
        conn.close()


def test_missing_trade_day_that_cannot_be_filled_warns(env, monkeypatch):
    days = [date(2026, 9, 14), *DAYS]
    days = sorted({*days, *(date.fromisoformat(d) for d in CLOSE_DATES)})
    monkeypatch.setattr(run.akdata, "trade_days", lambda: days)
    seed_concepts(env.db)
    state: dict = {}
    env.run(state=state)                                     # 夹具里新浪默认永远失败
    assert "收盘价有 1 天不完整" in state.get("warning", "")


# ---- 超跌反弹接入运行循环 ----

class SpyRebound(run.ReboundStrategy):
    instances: list = []
    boom_once = False

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []
        SpyRebound.instances.append(self)

    def sell_step(self, quotes, now):
        self.calls.append(bool(quotes))
        if SpyRebound.boom_once:
            SpyRebound.boom_once = False
            raise RuntimeError("rebound boom")
        return super().sell_step(quotes, now)


@pytest.fixture
def spy_rebound(monkeypatch):
    SpyRebound.instances, SpyRebound.boom_once = [], False
    monkeypatch.setattr(run, "ReboundStrategy", SpyRebound)
    return SpyRebound


def test_rebound_driven_every_minute_and_exception_isolated(env, spy_rebound):
    spy_rebound.boom_once = True
    FakeEngine.behavior = feeding_tick
    env.feed["quotes"] = {"600001": quote("600001", pct=-1, ts=datetime(2026, 9, 23, 10, 0))}
    path = env.run()
    assert path is not None and Path(path).exists(), env.log_text()
    [strat] = spy_rebound.instances
    assert strat.enabled
    assert len(strat.calls) == 240 and all(strat.calls)
    assert len(FakeEngine.instances[0].ticks) == 240                  # 引擎照常每分钟取行情
    assert "超跌反弹本轮出错：RuntimeError('rebound boom')" in env.log_text()
    assert "超跌反弹：近 " in env.log_text()


def test_rebound_sell_only_when_board_exclusion_fails(env, spy_rebound, monkeypatch):
    monkeypatch.setattr(run.closes, "consecutive_board_codes",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db boom")))
    env.run(once=True)
    [strat] = spy_rebound.instances
    assert not strat.enabled
    assert "超跌反弹连板排除计算失败，今日只卖不买" in env.log_text()


def test_rebound_reads_sentiment_from_engine(env, spy_rebound):
    env.run(once=True)
    [strat] = spy_rebound.instances
    FakeEngine.instances[0].sentiment_label = "冰点"
    assert strat.sentiment_provider() == "冰点"


def test_rebound_gets_volume_history_from_closes(env, spy_rebound):
    """成交量条件要用库里的历史成交量：盘前用 history_features 算好交给超跌反弹。"""
    vols = [900.0, 800.0, 700.0, 600.0, 500.0, 400.0]
    seed_closes(env.db, "600001", volumes=vols)
    env.run(once=True)
    [strat] = spy_rebound.instances
    assert strat.enabled
    assert strat.features["600001"]["vols"] == vols
    assert strat.features["600001"]["vols_until"] == "2026-09-22"


def test_rebound_sell_only_when_volume_history_fails(env, spy_rebound, monkeypatch):
    monkeypatch.setattr(run, "history_features",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db boom")))
    env.run(once=True)
    [strat] = spy_rebound.instances
    assert not strat.enabled
    assert "超跌反弹成交量历史计算失败，今日只卖不买" in env.log_text()


# ---- 实盘跟单：意图写入 / trading_day ----

class _FillEngine:
    """_run_minute 用的最小假引擎：sell_step 返回预设成交，buy_step 恒不成交。"""

    def __init__(self, fills):
        self._fills = fills

    def sell_step(self, now):
        return self._fills

    def buy_step(self, now, deadline=None):
        return []


def test_run_minute_writes_live_intent_for_live_account_fill_only():
    conn = db.connect(":memory:")
    t = datetime(2026, 9, 29, 9, 31)
    fills = [Fill("regime_main", "600001", "sell", 9.5, 500, 5.0, t, "止损"),
             Fill("main", "600002", "sell", 8.0, 100, 5.0, t, "止损")]
    run._run_minute(_FillEngine(fills), None, [], t, conn, lambda s: None, Notifier(None),
                    {"600001": "甲", "600002": "乙"}, {})
    assert [r[0] for r in conn.execute("SELECT code FROM live_intents")] == ["600001"]


def test_run_minute_regime_follows_a_source_fill_and_only_it_reaches_live():
    """情绪轮动：扫首板主账户这一分钟买了，情绪火热 → 轮动同价跟买，实盘意图只写轮动这一笔。"""
    from factories import quote
    from jevq.strategies.regime import RegimeStrategy
    conn = db.connect(":memory:")
    t = datetime(2026, 10, 12, 9, 41)
    q = quote("600001", pct=9.0, ts=t)

    class Eng(_FillEngine):
        last_quotes = {"600001": q}
        last_fetch_minute = t.strftime("%Y-%m-%d %H:%M")

    fills = [Fill("board_main", "600001", "buy", q.price, 2700, 7.5, t, "扫板9.0%")]
    regime = RegimeStrategy(conn, Ledger(conn), lambda: "火热")
    out = run._run_minute(Eng(fills), None, [], t, conn, lambda s: None, Notifier(None), {"600001": "甲"}, {},
                          regime=regime)
    assert [(f.account, f.side) for f in out] == [("board_main", "buy"), ("regime_main", "buy")]
    assert conn.execute("SELECT code, side, reason FROM live_intents").fetchall() == [
        ("600001", "buy", "轮动[火热]跟扫首板｜扫板9.0%")]


def test_run_minute_add_intents_exception_does_not_block_trading_or_push(monkeypatch):
    conn = db.connect(":memory:")
    t = datetime(2026, 9, 29, 9, 31)
    fills = [Fill("rebound_main", "600001", "sell", 9.5, 500, 5.0, t, "止损")]
    logs: list[str] = []

    class BoomNotifier:
        def __init__(self):
            self.sent = []

        def send(self, title, content):
            self.sent.append((title, content))

    def boom(conn_arg, *a, **kw):
        conn_arg.execute("INSERT INTO live_state(key, value) VALUES ('half', 'written')")
        raise RuntimeError("db locked")

    monkeypatch.setattr(run.live_store, "add_intents", boom)
    notifier = BoomNotifier()
    result = run._run_minute(_FillEngine(fills), None, [], t, conn, logs.append, notifier,
                             {"600001": "甲"}, {"rebound_main": "超跌·主"})
    assert result == fills                                      # 成交结果照常返回，不受影响
    assert any("实盘意图写入失败：RuntimeError('db locked')" in line for line in logs)
    assert len(notifier.sent) == 1 and notifier.sent[0][0] == "【超跌·主 卖出】甲 @9.5"   # 推送照常
    assert conn.execute("SELECT COUNT(*) FROM live_state WHERE key='half'").fetchone()[0] == 0  # 半截写入已回滚


def test_run_day_sets_trading_day_state_when_continuing(env):
    env.run(once=True)
    conn = db.connect(env.db)
    assert live_store.get_state(conn, "trading_day") == "2026-09-23"


def test_run_day_once_on_non_trading_day_does_not_set_trading_day_state(env):
    env.clock.t = datetime(2026, 9, 26, 9, 29)   # 周六，不在交易日历里；once=True 也照常跑一轮
    env.run(once=True)
    conn = db.connect(env.db)
    assert live_store.get_state(conn, "trading_day") is None


def test_custom_strategy_reads_sentiment_from_engine(env, fake_custom):
    """自定义策略的「市场情绪」条件读的也是引擎在买入阶段刷新的情绪（扫首板不用情绪）。"""
    seed_custom(env)
    env.run(once=True)
    [strat] = fake_custom.instances
    FakeEngine.instances[0].sentiment_label = "火热"
    assert strat.env_provider() == {"sentiment": "火热", "overseas": "中性"}



def test_catalyst_skipped_when_started_after_deadline(env, monkeypatch):
    """bug B：盘中重启不抓新闻，交易循环不能等它。"""
    calls = []
    monkeypatch.setattr(run.config, "CATALYST_ENABLED", True)
    monkeypatch.setattr(run.config, "CATALYST_DEADLINE", time(0, 0))
    monkeypatch.setattr(run.catalyst, "run", lambda *a, **k: calls.append(a) or {})
    env.run()
    assert calls == [] and "隔夜消息催化跳过" in env.log_text()


def test_turn_gate_attached_and_warns_when_claude_missing(env, monkeypatch):
    """用户 2026-10-14：拐点必须 Claude 同意；本机找不到 claude 要提醒（那天拐点不会确认）。"""
    monkeypatch.setattr(run.config, "TURN_CLAUDE_REQUIRED", True)
    env.run()
    eng = FakeEngine.instances[0]
    assert type(getattr(eng, "turn_gate", None)).__name__ == "ClaudeTurnJudge"
    assert "找不到本机 claude" in env.log_text()


def test_turn_push_names_claude_when_it_decides():
    assert run._turn_opinions({"by": "规则+Claude", "claude_prob": 0.72, "claude_reason": "修复", "prob": 0.8}) \
        == "Claude 判修复拐点 0.72（修复），Jev 0.8"
    assert run._turn_opinions({"by": "规则", "prob": 0.6}) == "Jev 判修复拐点 0.6"
