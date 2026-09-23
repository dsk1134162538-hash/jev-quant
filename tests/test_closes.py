from datetime import date, datetime, timedelta

from jevq import db
from jevq.data.closes import backfill_closes, ret5_from_db, save_closes

DATES = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17",
         "2026-09-18", "2026-09-21", "2026-09-22"]
TODAY = date(2026, 9, 23)


def conn():
    return db.connect(":memory:")


def seed(c, code, dates=DATES, prices=None):
    prices = prices or [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6]
    c.executemany("INSERT INTO closes(date, code, close) VALUES (?, ?, ?)",
                  [(d, code, p) for d, p in zip(dates, prices)])
    c.commit()


# ---- ret5_from_db ----

def test_ret5_uses_most_recent_6_dates():
    c = conn()
    seed(c, "600001")
    ret5 = ret5_from_db(c, ["600001"], TODAY)
    # 最近 6 个日期：09-15..09-22，close 10.1 -> 10.6
    assert ret5 == {"600001": round((10.6 / 10.1 - 1) * 100, 2)}


def test_ret5_missing_6th_date_excludes_code():
    c = conn()
    seed(c, "600001")
    # 缺最近 6 个日期里最早的一个（09-15），只有 5 条
    seed(c, "600002", dates=DATES[2:], prices=[10.2, 10.3, 10.4, 10.5, 10.6])
    ret5 = ret5_from_db(c, ["600001", "600002"], TODAY)
    assert list(ret5) == ["600001"]


def test_ret5_fewer_than_6_dates_in_table_returns_empty():
    c = conn()
    seed(c, "600001", dates=DATES[:5], prices=[10.0, 10.1, 10.2, 10.3, 10.4])
    assert ret5_from_db(c, ["600001"], TODAY) == {}


def test_ret5_ignores_rows_on_or_after_today():
    c = conn()
    seed(c, "600001")
    c.execute("INSERT INTO closes(date, code, close) VALUES (?, ?, ?)", (TODAY.isoformat(), "600001", 99.0))
    c.commit()
    ret5 = ret5_from_db(c, ["600001"], TODAY)
    assert ret5 == {"600001": round((10.6 / 10.1 - 1) * 100, 2)}   # today 的一行没被当成"最新"


# ---- save_closes ----

def test_save_closes_skips_non_positive_prices():
    c = conn()
    n = save_closes(c, TODAY, {"600001": 10.5, "600002": 0.0, "600003": -1.0, "600004": None})
    assert n == 1
    rows = c.execute("SELECT code, close FROM closes").fetchall()
    assert rows == [("600001", 10.5)]


def test_save_closes_insert_or_replace():
    c = conn()
    save_closes(c, TODAY, {"600001": 10.0})
    save_closes(c, TODAY, {"600001": 10.9})
    rows = c.execute("SELECT close FROM closes WHERE date=? AND code='600001'",
                     (TODAY.isoformat(),)).fetchall()
    assert rows == [(10.9,)]


# ---- backfill_closes ----

def test_backfill_skips_codes_already_full():
    c = conn()
    seed(c, "600001")
    calls = []

    def fetch(code):
        calls.append(code)
        return []

    n = backfill_closes(c, ["600001", "600002"], TODAY, fetch, pause=0)
    assert calls == ["600002"]
    assert n == 1


def test_backfill_writes_rows_before_today_only():
    c = conn()

    def fetch(code):
        return [("2026-09-20", 10.0), ("2026-09-21", 10.1), (TODAY.isoformat(), 99.0)]

    backfill_closes(c, ["600001"], TODAY, fetch, pause=0)
    rows = sorted(c.execute("SELECT date, close FROM closes WHERE code='600001'").fetchall())
    assert rows == [("2026-09-20", 10.0), ("2026-09-21", 10.1)]


def test_backfill_stops_after_consecutive_failures(capsys):
    c = conn()
    codes = [f"60000{i}" for i in range(5)]
    calls = []

    def fetch(code):
        calls.append(code)
        raise RuntimeError("501")

    n = backfill_closes(c, codes, TODAY, fetch, pause=0, max_consecutive_failures=3)
    assert len(calls) == 3
    assert n == 0
    assert "K 线接口连续失败 3 次，停止补数（可能被限流）" in capsys.readouterr().out


def test_backfill_deadline_already_passed_processes_nothing():
    c = conn()
    calls = []

    def fetch(code):
        calls.append(code)
        return []

    n = backfill_closes(c, ["600001", "600002"], TODAY, fetch, pause=0,
                        deadline=datetime.now() - timedelta(seconds=1))
    assert calls == []
    assert n == 0
