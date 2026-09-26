from datetime import date, datetime, timedelta

from jevq import db
from jevq.data.closes import backfill_closes, prev_closes_from_db, recent_limit_up_codes, ret5_from_db, save_closes

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
        return [("2026-09-22", 10.0)]

    n = backfill_closes(c, ["600001", "600002"], TODAY, fetch, pause=0)
    assert calls == ["600002"]
    assert n == 1


def test_backfill_writes_rows_before_today_only():
    c = conn()

    def fetch(code):
        return [("2026-09-20", 10.0), ("2026-09-21", 10.1), (TODAY.isoformat(), 99.0)]

    n = backfill_closes(c, ["600001"], TODAY, fetch, pause=0)
    rows = sorted(c.execute("SELECT date, close FROM closes WHERE code='600001'").fetchall())
    assert rows == [("2026-09-20", 10.0), ("2026-09-21", 10.1)]
    assert n == 1


def test_backfill_call_that_writes_nothing_is_not_counted():
    c = conn()
    calls = []

    def fetch(code):
        calls.append(code)
        return [(TODAY.isoformat(), 99.0)]   # 全部是 today 及以后的行，一条都写不进去

    n = backfill_closes(c, ["600001"], TODAY, fetch, pause=0)
    assert calls == ["600001"]
    assert n == 0
    assert c.execute("SELECT COUNT(*) FROM closes").fetchone()[0] == 0


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


# ---- prev_closes_from_db（前日收，给首板判断用）----

def test_prev_closes_returns_close_of_day_before_prev_day():
    c = conn()
    seed(c, "600001")
    assert prev_closes_from_db(c, ["600001", "600009"], TODAY, date(2026, 9, 22)) ==         {"600001": 10.5, "600009": None}


def test_prev_closes_empty_when_latest_date_is_not_prev_day():
    c = conn()
    seed(c, "600001", dates=DATES[:-1], prices=[10.0] * 6)       # 最新只到 09-21，昨天 09-22 没存
    assert prev_closes_from_db(c, ["600001"], TODAY, date(2026, 9, 22)) == {}


def test_prev_closes_empty_table_is_empty():
    assert prev_closes_from_db(conn(), ["600001"], TODAY, date(2026, 9, 22)) == {}


# ---- recent_limit_up_codes（近 N 日是否涨停过，首板过滤用）----

BASE = date(2026, 10, 5)


def _offset_dates(n):
    """从 BASE 往前数 n 个连续日历日（offset 1 = 昨天……offset n = n 天前），升序返回。"""
    return [(BASE - timedelta(days=i)).isoformat() for i in range(n, 0, -1)]


def test_recent_limit_up_5_days_ago_is_in_set():
    c = conn()
    dates = _offset_dates(12)          # index j ↔ offset(12 - j)：index 0 是 offset 12（最老），index 11 是 offset 1（昨天）
    prices = [10.0] * 12
    prices[7] = round(prices[6] * 1.1, 2)      # index 7 = offset 5（10 天窗口内），相对 index 6（offset 6）涨停
    c.executemany("INSERT INTO closes(date, code, close) VALUES (?, '600001', ?)",
                  list(zip(dates, prices)))
    c.commit()
    assert recent_limit_up_codes(c, ["600001"], BASE) == {"600001"}


def test_recent_limit_up_11_days_ago_is_excluded():
    c = conn()
    dates = _offset_dates(12)          # index 0 = offset 12（最老），index 1 = offset 11
    prices = [10.0] * 12
    prices[1] = 11.0    # 涨停发生在 offset 12→11 之间（11 天前），超出 10 天窗口
    c.executemany("INSERT INTO closes(date, code, close) VALUES (?, '600001', ?)",
                  list(zip(dates, prices)))
    c.commit()
    # 只有最近 11 个日期（offset 1..11）会被取到（days+1=11），offset 12（index 0）那天取不到，
    # 涨停判断不到，正确地被排除在外。
    assert recent_limit_up_codes(c, ["600001"], BASE) == set()


def test_recent_limit_up_best_effort_with_only_3_dates():
    c = conn()
    dates = [(BASE - timedelta(days=3)).isoformat(), (BASE - timedelta(days=2)).isoformat(),
             (BASE - timedelta(days=1)).isoformat()]
    prices = [10.0, 11.0, 11.0]        # 第一天到第二天涨停
    c.executemany("INSERT INTO closes(date, code, close) VALUES (?, '600001', ?)",
                  list(zip(dates, prices)))
    c.commit()
    assert recent_limit_up_codes(c, ["600001"], BASE) == {"600001"}


def test_recent_limit_up_no_data_returns_empty():
    assert recent_limit_up_codes(conn(), ["600001"], BASE) == set()


def test_recent_limit_up_no_codes_returns_empty():
    c = conn()
    seed(c, "600001")
    assert recent_limit_up_codes(c, [], TODAY) == set()


def test_prev_closes_none_when_second_date_is_not_prev_prev_day():
    c = conn()
    seed(c, "600001", dates=["2026-09-18", "2026-09-22"], prices=[9.0, 9.5])   # 09-21 缺
    assert prev_closes_from_db(c, ["600001"], TODAY, date(2026, 9, 22), date(2026, 9, 21)) ==         {"600001": None}
    assert prev_closes_from_db(c, ["600001"], TODAY, date(2026, 9, 22)) == {"600001": 9.0}


# ---- 日成交量（手）：收盘存库 + 补数 ----

def test_save_closes_stores_volume_in_lots():
    c = conn()
    n = save_closes(c, TODAY, {"600001": 10.5, "600002": 8.0, "600003": 7.0},
                    volumes={"600001": 12345, "600002": 0})
    assert n == 3
    rows = sorted(c.execute("SELECT code, close, volume FROM closes").fetchall())
    # 量为 0（停牌/没成交）和没给量的都存 NULL，不当成真实的 0 手
    assert rows == [("600001", 10.5, 12345.0), ("600002", 8.0, None), ("600003", 7.0, None)]


def test_save_closes_without_volume_keeps_existing_volume():
    c = conn()
    save_closes(c, TODAY, {"600001": 10.0}, volumes={"600001": 500})
    save_closes(c, TODAY, {"600001": 10.9})
    assert c.execute("SELECT close, volume FROM closes").fetchone() == (10.9, 500.0)


def test_backfill_stores_volume_from_three_tuple_fetcher():
    c = conn()

    def fetch(code):
        return [("2026-09-21", 10.0, 1200.0), ("2026-09-22", 10.1, 900.0)]

    n = backfill_closes(c, ["600001"], TODAY, fetch, pause=0)
    rows = sorted(c.execute("SELECT date, close, volume FROM closes WHERE code='600001'").fetchall())
    assert rows == [("2026-09-21", 10.0, 1200.0), ("2026-09-22", 10.1, 900.0)]
    assert n == 1


def test_backfill_two_tuple_fetcher_keeps_existing_volume():
    c = conn()
    c.execute("INSERT INTO closes(date, code, close, volume) VALUES ('2026-09-22', '600001', 10.0, 800)")
    c.commit()
    backfill_closes(c, ["600001"], TODAY, lambda code: [("2026-09-22", 10.2)], pause=0)
    assert c.execute("SELECT close, volume FROM closes").fetchone() == (10.2, 800.0)


def test_backfill_fill_volume_refetches_codes_with_null_volume():
    c = conn()
    seed(c, "600001")                                  # 7 天收盘价齐全，但量全是 NULL
    seed(c, "600002")
    c.execute("UPDATE closes SET volume = 100 WHERE code = '600002'")
    c.commit()
    calls = []

    def fetch(code):
        calls.append(code)
        return [(d, 10.0, 1000.0 + i) for i, d in enumerate(DATES)]

    assert backfill_closes(c, ["600001", "600002"], TODAY, fetch, pause=0) == 0   # 默认不管量
    assert calls == []
    n = backfill_closes(c, ["600001", "600002"], TODAY, fetch, pause=0, fill_volume=True)
    assert calls == ["600001"] and n == 1              # 量已齐的 600002 不再拉
    vols = [r[0] for r in c.execute("SELECT volume FROM closes WHERE code='600001' ORDER BY date")]
    assert vols == [1000.0 + i for i in range(len(DATES))]


def test_fetch_daily_sina_converts_shares_to_lots(monkeypatch):
    import pandas as pd

    from jevq.data import akdata
    seen = {}

    def fake_daily(symbol, start_date, end_date, adjust=""):
        seen.update(symbol=symbol, start_date=start_date, end_date=end_date, adjust=adjust)
        return pd.DataFrame({"date": [date(2026, 9, 21), date(2026, 9, 22)], "close": [10.0, 10.5],
                             "volume": [123400.0, 5600.0]})

    monkeypatch.setattr(akdata.ak, "stock_zh_a_daily", fake_daily)
    rows = akdata.fetch_daily_sina("600001", today=date(2026, 9, 23))
    assert rows == [("2026-09-21", 10.0, 1234.0), ("2026-09-22", 10.5, 56.0)]
    assert seen["symbol"] == "sh600001" and seen["adjust"] == "" and seen["end_date"] == "20260923"
