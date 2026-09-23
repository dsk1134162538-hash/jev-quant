from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import run
from factories import quote
from jevq import db
from jevq.ledger import Ledger
from jevq.runlock import hold_run_lock

DAYS = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]
CLOSE_DATES = ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22"]


def seed_closes(dbp, code, prices=None):
    """给 code 灌 6 个交易日的收盘价，让 5 日涨幅覆盖率天然是 100%（默认 5.0%，和旧 mock 值保持一致）。"""
    prices = prices or [10.0, 10.0, 10.0, 10.0, 10.0, 10.5]
    conn = db.connect(dbp)
    conn.executemany("INSERT OR REPLACE INTO closes(date, code, close) VALUES (?, ?, ?)",
                     [(d, code, p) for d, p in zip(CLOSE_DATES, prices)])
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

    def __init__(self, conn, ledger, notice_judge, news_judge, market, fetch_quotes, fetch_pools,
                 universe, ret5, prev_day, overseas, overseas_trace=None):
        self.conn, self.ledger = conn, ledger
        self.universe, self.ret5, self.prev_day, self.overseas = universe, ret5, prev_day, overseas
        self.ticks, self.last_quotes, self.sentiment_label = [], {}, "正常"
        FakeEngine.instances.append(self)

    def tick(self, now):
        self.ticks.append(now)
        if FakeEngine.behavior:
            return FakeEngine.behavior(self, now) or []
        return []


class Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "data" / "jevq.sqlite"
    reports = tmp_path / "reports"
    monkeypatch.setattr(run, "DB_PATH", str(dbp))
    monkeypatch.setattr(run, "REPORT_DIR", str(reports))
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    clock = Clock(datetime(2026, 9, 23, 9, 29))
    monkeypatch.setattr(run, "now", clock.now)
    monkeypatch.setattr(run, "seconds_to_next_minute", clock.step)
    monkeypatch.setattr(run.akdata, "trade_days", lambda: DAYS)
    monkeypatch.setattr(run.akdata, "load_universe", lambda today, days: {"600001": "甲"})
    seed_closes(str(dbp), "600001")
    # 默认永远失败：模拟 K 线接口被 WAF 封锁，同时保证测试不会真的发请求。
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
    monkeypatch.setattr(run, "MarketJudge", FakeMarket)
    monkeypatch.setattr(run, "Engine", FakeEngine)
    FakeMarket.fail = False
    FakeEngine.instances, FakeEngine.behavior = [], None
    logs: list[str] = []

    e = Env()
    e.db, e.reports, e.clock, e.feed, e.logs = str(dbp), reports, clock, feed, logs
    e.run = lambda **kw: run.run_day(log=logs.append, **kw)
    e.log_text = lambda: "\n".join(logs)
    return e


def hold(env, price=9.9, buy_day=date(2026, 9, 22)):
    conn = db.connect(env.db)
    Ledger(conn).buy("main", quote("600001", pct=round((price / 10 - 1) * 100, 2)),
                     datetime.combine(buy_day, datetime.min.time()).replace(hour=10), "x")
    conn.close()


def main_equity_check(env, price):
    conn = db.connect(env.db)
    try:
        ledger = Ledger(conn)
        shares = ledger.positions("main")[0].shares
        expected = round(ledger.cash("main") + shares * price, 2)
        value = conn.execute("SELECT value FROM equity WHERE account='main'").fetchone()[0]
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
    assert eng.universe == {} and eng.ret5 == {}
    assert "只卖模式：今日不开新仓" in env.log_text()
    assert len(eng.ticks) == 240


def test_calendar_failure_disables_stale_before(env, monkeypatch):
    def fail():
        raise TimeoutError("sina 超时")

    monkeypatch.setattr(run.akdata, "trade_days", fail)
    env.run()
    eng = FakeEngine.instances[0]
    assert eng.stale_before is None
    assert "交易日历不可用：停用「隔夜超期」规则" in env.log_text()


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
    assert eng.universe == {} and eng.ret5 == {}
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
    env.feed["quotes"] = {"600001": quote("600001", pct=5.0, name="甲")}
    env.clock.t = datetime(2026, 9, 23, 15, 10)
    path = env.run()
    assert path is not None and Path(path).exists()
    assert all(not e.ticks for e in FakeEngine.instances)
    assert loaded == []                                  # 收盘后不再准备股票池
    main_equity_check(env, 10.5)
    assert "600001 甲" in Path(path).read_text(encoding="utf-8")


# ---- Task 19：本地收盘价库 ----

def test_insufficient_ret5_coverage_sets_warning_and_skips_buys(env, monkeypatch):
    monkeypatch.setattr(run.closes, "ret5_from_db", lambda conn, codes, today: {})
    state: dict = {}
    env.run(state=state)
    eng = FakeEngine.instances[0]
    assert eng.ret5 == {}
    assert state.get("warning") == "5 日涨幅数据不足，今日只卖不买"
    assert "5 日涨幅覆盖不足（0/1），今日只卖不买" in env.log_text()
    assert len(eng.ticks) == 240                          # 只卖不买不代表停跑，照常盯盘


def test_report_saves_closes_before_writing_it(env):
    env.feed["quotes"] = {"600001": quote("600001", pct=2.0, name="甲")}
    env.run()
    conn = db.connect(env.db)
    try:
        row = conn.execute("SELECT close FROM closes WHERE date=? AND code='600001'",
                           ("2026-09-23",)).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == pytest.approx(10.2)
    assert "收盘价存库" in env.log_text()


def test_backfill_flag_runs_without_jev_key(env, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    calls = []
    monkeypatch.setattr(run.tencent, "fetch_kline",
                        lambda code, session=None: calls.append(code) or [])
    run.run_backfill(log=env.logs.append)
    assert "股票池 1 只" in env.log_text()
    assert "补数" in env.log_text()


def test_backfill_flag_holds_runner_lock(env):
    with hold_run_lock(Path(env.db).parent / "runner.lock"):
        with pytest.raises(RuntimeError, match="已有另一个交易进程在运行"):
            run.run_backfill(log=env.logs.append)
