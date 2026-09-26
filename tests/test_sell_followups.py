"""卖飞统计：卖出后当日最高 / 当日收盘 / 次日最高 / 次日收盘，只记录不参与交易。"""
from datetime import date, datetime

from factories import quote

from jevq import db
from jevq.data import sell_followups as sf
from jevq.ledger import Ledger
from jevq.report import write_report

D1 = date(2026, 9, 24)
D2 = date(2026, 9, 25)


def _sell(conn, code="600001", account="rebound_main", price=10.0, ts=datetime(2026, 9, 24, 10, 0),
          reason="跟踪止盈"):
    cur = conn.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
                       "VALUES (?, ?, 'sell', ?, 1000, 5, ?, ?)",
                       (account, code, price, ts.isoformat(timespec="seconds"), reason))
    conn.commit()
    return cur.lastrowid


def _q(code, price, t, high=None):
    q = quote(code, pct=round((price / 10 - 1) * 100, 2), ts=t)
    return type(q)(**{**q.__dict__, "price": price, "high": high or price})


def test_tracker_keeps_max_price_after_sell_and_ignores_stale_quotes():
    conn = db.connect(":memory:")
    fid = _sell(conn)
    tr = sf.AfterSellTracker()
    tr.update(conn, D1, {"600001": _q("600001", 10.1, datetime(2026, 9, 24, 10, 1))})
    tr.update(conn, D1, {"600001": _q("600001", 10.4, datetime(2026, 9, 24, 11, 0))})
    tr.update(conn, D1, {"600001": _q("600001", 10.2, datetime(2026, 9, 24, 13, 0))})
    tr.update(conn, D1, {"600001": _q("600001", 99.0, datetime(2026, 9, 23, 15, 0))})   # 旧快照
    assert tr.highs[fid] == 10.4


def test_tracker_starts_at_sell_price():
    conn = db.connect(":memory:")
    fid = _sell(conn, price=10.0)
    tr = sf.AfterSellTracker()
    tr.update(conn, D1, {})
    assert tr.highs[fid] == 10.0


def test_record_day_then_fill_next_day():
    conn = db.connect(":memory:")
    fid = _sell(conn)
    n = sf.record_day(conn, D1, {"600001": _q("600001", 10.3, datetime(2026, 9, 24, 15, 0))}, {fid: 10.4})
    assert n == 1
    n = sf.fill_next_day(conn, D1, {"600001": _q("600001", 10.5, datetime(2026, 9, 25, 15, 0), high=10.8)}, D2)
    assert n == 1
    row = conn.execute("SELECT sell_price, after_high, day_close, next_date, next_high, next_close "
                       "FROM sell_followups WHERE fill_id=?", (fid,)).fetchone()
    assert row == (10.0, 10.4, 10.3, "2026-09-25", 10.8, 10.5)


def test_record_day_without_tracking_leaves_after_high_null_but_close_raises_it():
    """没跟踪到（比如盘中重启）就不编造卖后最高；但收盘价比卖出价高时，卖后最高至少是收盘价。"""
    conn = db.connect(":memory:")
    a = _sell(conn, code="600001")
    b = _sell(conn, code="600002")
    sf.record_day(conn, D1, {"600001": _q("600001", 9.9, datetime(2026, 9, 24, 15, 0)),
                             "600002": _q("600002", 10.2, datetime(2026, 9, 24, 15, 0))}, {})
    rows = dict(conn.execute("SELECT fill_id, after_high FROM sell_followups").fetchall())
    assert rows == {a: None, b: 10.2}


def test_fill_next_day_skips_stale_quotes():
    conn = db.connect(":memory:")
    fid = _sell(conn)
    sf.record_day(conn, D1, {}, {fid: 10.0})
    assert sf.fill_next_day(conn, D1, {"600001": _q("600001", 10.5, datetime(2026, 9, 24, 15, 0))}, D2) == 0


def test_codes_to_quote_includes_today_and_prev_day_sells():
    conn = db.connect(":memory:")
    _sell(conn, code="600001", ts=datetime(2026, 9, 24, 10, 0))
    _sell(conn, code="600002", ts=datetime(2026, 9, 25, 10, 0))
    _sell(conn, code="600003", ts=datetime(2026, 9, 22, 10, 0))
    assert sf.codes_to_quote(conn, D2, D1) == {"600001", "600002"}


def test_report_shows_sell_followups(tmp_path):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    fid = _sell(conn)
    sf.record_day(conn, D1, {"600001": _q("600001", 10.3, datetime(2026, 9, 24, 15, 0))}, {fid: 10.4})
    sf.fill_next_day(conn, D1, {"600001": _q("600001", 10.5, datetime(2026, 9, 25, 15, 0), high=10.8)}, D2)
    text = write_report(conn, ledger, D2, {}, "中性", "正常", tmp_path, names={"600001": "甲"}).read_text(
        encoding="utf-8")
    assert "## 卖飞回看" in text
    assert ("| 2026-09-24 | 超跌·主(Jev) | 600001 | 甲 | 跟踪止盈 | 10.00 | +4.00% | +3.00% "
            "| +8.00% | +5.00% |") in text
