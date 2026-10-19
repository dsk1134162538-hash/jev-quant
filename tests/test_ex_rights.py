"""除权除息不能被当成下跌（用户 2026-10-19「都做」第 2 条）。"""
from datetime import date

from jevq import db
from jevq.data import closes, events, holders

DATES = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"]
TODAY = date(2026, 10, 20)


def _seed(conn, prices, pre=None):
    for d, p in zip(DATES, prices):
        conn.execute("INSERT INTO closes(date, code, close, pre_close) VALUES (?, '600001', ?, ?)",
                     (d, p, (pre or {}).get(d)))
    conn.commit()


def test_ret5_uses_adjusted_pre_close_on_ex_date():
    conn = db.connect(":memory:")
    # 10-15 10 送 2：10.0 → 除权参考价 8.33，当天收 8.40（真实 +0.8%）
    _seed(conn, [10.0, 10.0, 10.0, 8.40, 8.40, 8.40], pre={"2026-10-15": 8.3333})
    assert abs(closes.ret5_from_db(conn, ["600001"], TODAY)["600001"] - 0.8) < 0.01
    conn2 = db.connect(":memory:")
    _seed(conn2, [10.0, 10.0, 10.0, 8.40, 8.40, 8.40])                 # 没有调整后的昨收：旧算法当成 -16%
    assert closes.ret5_from_db(conn2, ["600001"], TODAY)["600001"] == -16.0


def test_backfill_from_dividend_plan_and_never_overwrites_stored_pre_close():
    conn = db.connect(":memory:")
    _seed(conn, [10.0, 10.0, 10.0, 8.40, 8.40, 8.40])
    n = closes.backfill_ex_rights(conn, [("600001", "2026-10-15", 2.0, 1.0)])     # 10 送 2 派 1 元
    pre = conn.execute("SELECT pre_close FROM closes WHERE date='2026-10-15'").fetchone()[0]
    assert n == 1 and abs(pre - (10.0 - 0.1) / 1.2) < 1e-4
    assert closes.backfill_ex_rights(conn, [("600001", "2026-10-15", 5.0, 0)]) == 0
    assert abs(holders.mean_abs_change(conn, ["600001"], TODAY, days=5)["600001"] - 0.36) < 0.05


def test_save_closes_keeps_pre_close_and_events_backfill_window():
    conn = db.connect(":memory:")
    closes.save_closes(conn, date(2026, 10, 19), {"600001": 8.4}, volumes={"600001": 100}, pre_closes={"600001": 8.33})
    closes.save_closes(conn, date(2026, 10, 19), {"600001": 8.5})                 # 没给昨收：保留
    assert conn.execute("SELECT close, pre_close FROM closes").fetchone() == (8.5, 8.33)
    assert events.dividend_periods(TODAY) == ["20260630", "20251231"]
    got = []
    events.backfill_ex_rights(conn, TODAY, fetch=lambda p: got.append(p) or [("600001", "2026-10-19", 2.0, 0.0)],
                              log=lambda *_: None)
    assert got == ["20260630", "20251231"]
