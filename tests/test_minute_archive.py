import sqlite3
from datetime import date, datetime

from dashboard import minute_archive as ma
from jevq.models import Quote

DAY = date(2026, 11, 2)


def _q(code, high, ts=datetime(2026, 11, 2, 15, 0)):
    return Quote(code=code, name=code, price=high, pre_close=10.0, open=10.0, high=high, low=10.0, volume=1,
                 amount=1, limit_up=11.0, limit_down=9.0, ts=ts)


def test_only_stocks_that_touched_nine_percent_today_are_saved_once(tmp_path):
    quotes = {"600001": _q("600001", 10.95), "600002": _q("600002", 10.5),
              "600003": _q("600003", 11.0, ts=datetime(2026, 10, 30, 15, 0)),        # 停牌/旧行情
              "600004": _q("600004", 11.0)}
    calls = []

    def minutes(code):
        calls.append(code)
        if code == "600004":
            raise RuntimeError("timeout")
        return [("09:30", 10.1, 100), ("09:31", 10.9, 300)]
    db = tmp_path / "m.sqlite"
    out = ma.archive_day(DAY, list(quotes), lambda codes: quotes, minutes, db, workers=2)
    assert out == {"touched": 2, "followed": 0, "saved": 1, "failed": ["600004"]}
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT code, hhmm, price, cum_volume FROM minute_bars ORDER BY hhmm").fetchall() == \
        [("600001", "09:30", 10.1, 100), ("600001", "09:31", 10.9, 300)]
    assert conn.execute("SELECT bars, limit_up FROM minute_days").fetchone() == (2, 11.0)
    conn.close()
    ma.archive_day(DAY, list(quotes), lambda codes: quotes, minutes, db, workers=2)
    assert calls.count("600001") == 1                      # 存过的不重拉


def test_next_day_path_of_yesterdays_touched_stocks_is_saved_too(tmp_path):
    # 次日卖法（开盘 / 保本挂单 / 10 点 / 收盘）要用持有那天的分时：前一交易日摸过 9% 的票今天也存
    db = tmp_path / "m.sqlite"
    bars = lambda code: [("09:30", 10.1, 100)]                                       # noqa: E731
    ma.archive_day(date(2026, 10, 30), ["600001"], lambda c: {"600001": _q("600001", 10.95,
                   ts=datetime(2026, 10, 30, 15, 0))}, bars, db)
    today = {"600001": _q("600001", 10.2), "600002": _q("600002", 10.3)}           # 今天都没摸 9%
    out = ma.archive_day(DAY, list(today), lambda c: today, bars, db)
    assert out["touched"] == 0 and out["followed"] == 1 and out["saved"] == 1
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT code FROM minute_days WHERE date='2026-11-02'").fetchall() == [("600001",)]
    conn.close()
