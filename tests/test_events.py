"""持有期内有财报、大额解禁的票主账户不买（用户 2026-10-19）。"""
from datetime import date

from jevq import config, db
from jevq.data import events

CAL = [date(2026, 10, d) for d in (19, 20, 21, 22, 23, 26, 27, 28, 29, 30)]
MON = date(2026, 10, 19)


def test_report_periods_are_the_last_two_quarter_ends():
    assert events.report_periods(MON) == ["20260930", "20260630"]
    assert events.report_periods(date(2027, 4, 10)) == ["20270331", "20261231"]
    assert events.report_periods(date(2027, 1, 5)) == ["20261231", "20260930"]


def test_exit_day_counts_trading_days_and_falls_back_to_weekdays():
    assert events.exit_day(MON, 3, CAL) == date(2026, 10, 22)
    assert events.exit_day(date(2026, 10, 23), 1, CAL) == date(2026, 10, 26)
    assert events.exit_day(date(2026, 10, 23), 1, None) == date(2026, 10, 26)          # 周五 → 周一


def _filter(reports=None, unlocks=None, today=MON):
    return events.EventFilter(reports or {}, unlocks or {}, today, CAL)


def test_report_inside_holding_window_blocks_outside_passes():
    f = _filter({"600001": [("2026-10-21", "20260930")], "600002": [("2026-10-29", "20260930")]})
    assert f.blocked("600001", 3).startswith("持有期内要发财报：2026-09 期报告预约 2026-10-21")
    assert f.blocked("600002", 3) is None
    assert f.blocked("600001", 1) is None                         # 扫首板只拿到 10-20
    assert _filter({"600001": [("2026-10-19", "20260930")]}).blocked("600001", 1)   # 当天晚上发也算


def test_unlock_needs_size_and_must_be_after_today():
    f = _filter(unlocks={"600001": [("2026-10-20", 0.05)], "600002": [("2026-10-19", 0.2)]})
    assert "大额解禁" in f.blocked("600001", 2)
    assert f.blocked("600002", 2) is None                         # 今天已经解禁了，价格里已经有了


def test_load_skips_disclosed_reports_and_small_unlocks():
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO report_dates VALUES (?, '20260930', ?, ?, '2026-10-19')",
                     [("600001", "2026-10-20", 0), ("600002", "2026-10-20", 1)])
    conn.executemany("INSERT INTO unlocks VALUES (?, '2026-10-20', ?, '首发', '2026-10-19')",
                     [("600003", 0.10), ("600004", 0.001)])
    conn.commit()
    f = events.EventFilter.load(conn, MON, CAL)
    assert set(f.reports) == {"600001"} and set(f.unlocks) == {"600003"}


def test_refresh_keeps_old_data_per_source_on_failure():
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO report_dates VALUES ('600009', '20260930', '2026-10-30', 0, '2026-10-18')")
    conn.commit()

    def boom(*a):
        raise TimeoutError("x")
    got = events.refresh(conn, MON, fetch_reports=boom,
                         fetch_unlock=lambda a, b: [("600001", "2026-10-21", 0.05, "首发")], log=lambda *_: None)
    assert got == {"reports": 0, "unlocks": 1}
    assert conn.execute("SELECT code FROM report_dates").fetchall() == [("600009",)]
    got = events.refresh(conn, MON, fetch_reports=lambda p: [("600002", p, "2026-10-28", False)],
                         fetch_unlock=boom, log=lambda *_: None)
    assert got["reports"] == 2 and conn.execute("SELECT COUNT(*) FROM unlocks").fetchone()[0] == 1


def test_strategies_skip_event_blocked_stocks():
    from datetime import datetime

    from factories import quote

    from jevq.ledger import Ledger
    from jevq.strategies.rebound import ReboundStrategy

    class Judge:
        last_trace = None

        def check(self, code, *a):
            return "pass", "ok"
    prev = date(2026, 9, 23)
    uni = {"600001": "甲", "600002": "乙", "600003": "丙"}
    shrink = {"vols": [100.0, 400.0, 300.0, 200.0, 150.0], "vols_until": prev.isoformat()}
    conn = db.connect(":memory:")
    strat = ReboundStrategy(conn, Ledger(conn), Judge(), Judge(), uni, {"600001": -5.0, "600002": -15.0, "600003": -8.0},
                            set(), prev, lambda: "正常", features={c: shrink for c in uni})
    strat.event_filter = events.EventFilter({"600002": [("2026-09-25", "20260630")]}, {}, date(2026, 9, 24), None)
    t = datetime(2026, 9, 24, 10, 0)
    fills = strat.on_minute({c: quote(c, pct=-1.0, amount=2e8, ts=t, volume=10) for c in uni}, t)
    assert ("rebound_main", "600003") in {(f.account, f.code) for f in fills}
    assert "财报" in conn.execute("SELECT detail FROM decisions WHERE verdict='skip'").fetchone()[0]
    assert config.REBOUND_MAIN_MAX_HOLD_DAYS == 3
