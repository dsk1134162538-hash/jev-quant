from datetime import date, datetime

from jevq import db
from jevq.data.board_events import bucket_of, fill_next_day, record_day, summary
from factories import quote

PREV = date(2026, 9, 22)
TODAY = date(2026, 9, 23)


def conn():
    return db.connect(":memory:")


# ---- bucket_of ----

def test_bucket_boundaries():
    assert bucket_of("092500") == "早盘"
    assert bucket_of("103000") == "上午"
    assert bucket_of("113000") == "午盘"
    assert bucket_of("133000") == "下午"
    assert bucket_of("140000") == "尾盘"


def test_bucket_call_auction_seal_is_morning_session():
    # 09:25 集合竞价直接封死，早盘桶没有下限
    assert bucket_of("092500") == "早盘"


def test_bucket_just_before_each_boundary_stays_in_earlier_bucket():
    assert bucket_of("102959") == "早盘"
    assert bucket_of("112959") == "上午"
    assert bucket_of("132959") == "午盘"
    assert bucket_of("135959") == "下午"


def test_bucket_late_tail_session():
    assert bucket_of("145959") == "尾盘"


# ---- record_day ----

def test_record_day_only_keeps_first_board():
    c = conn()
    rows = [
        {"代码": "600001", "名称": "甲", "首次封板时间": "093000", "连板数": 1},
        {"代码": "600002", "名称": "乙", "首次封板时间": "094500", "连板数": 2},
    ]
    n = record_day(c, TODAY, rows, {})
    assert n == 1
    codes = [r[0] for r in c.execute("SELECT code FROM board_events").fetchall()]
    assert codes == ["600001"]


def test_record_day_uses_first_concept_or_empty():
    c = conn()
    rows = [
        {"代码": "600001", "名称": "甲", "首次封板时间": "093000", "连板数": 1},
        {"代码": "600002", "名称": "乙", "首次封板时间": "094500", "连板数": 1},
    ]
    record_day(c, TODAY, rows, {"600001": ["机器人", "华为汽车"]})
    got = dict(c.execute("SELECT code, concept FROM board_events").fetchall())
    assert got == {"600001": "机器人", "600002": ""}


def test_record_day_sets_bucket_and_is_pad_with_zeros():
    c = conn()
    rows = [{"代码": "1", "名称": "甲", "首次封板时间": "93000", "连板数": 1}]
    record_day(c, TODAY, rows, {})
    code, bucket = c.execute("SELECT code, bucket FROM board_events").fetchone()
    assert code == "000001"
    assert bucket == "早盘"


def test_record_day_does_not_clobber_already_filled_row():
    c = conn()
    rows = [{"代码": "600001", "名称": "甲", "首次封板时间": "093000", "连板数": 1}]
    record_day(c, PREV, rows, {})
    c.execute("UPDATE board_events SET next_open_pct=1.0, next_close_pct=2.0, next_board=1 "
             "WHERE date=? AND code=?", (PREV.isoformat(), "600001"))
    c.commit()
    record_day(c, PREV, rows, {})   # 重复调用（比如程序重跑）
    row = c.execute("SELECT next_open_pct, next_close_pct, next_board FROM board_events "
                    "WHERE date=? AND code=?", (PREV.isoformat(), "600001")).fetchone()
    assert row == (1.0, 2.0, 1)


def test_record_day_no_first_board_returns_zero():
    c = conn()
    rows = [{"代码": "600002", "名称": "乙", "首次封板时间": "094500", "连板数": 2}]
    assert record_day(c, TODAY, rows, {}) == 0


# ---- fill_next_day ----

def test_fill_next_day_computes_open_close_board():
    c = conn()
    record_day(c, PREV, [{"代码": "600001", "名称": "甲", "首次封板时间": "093000", "连板数": 1}], {})
    q = quote("600001", pct=3.0, open_pct=1.0, pre_close=10.0, ts=datetime(2026, 9, 23, 15, 0))
    n = fill_next_day(c, PREV, {"600001": q}, TODAY)
    assert n == 1
    row = c.execute("SELECT next_open_pct, next_close_pct, next_board FROM board_events "
                    "WHERE date=? AND code=?", (PREV.isoformat(), "600001")).fetchone()
    assert row == (1.0, 3.0, 0)


def test_fill_next_day_marks_board_when_sealed():
    c = conn()
    record_day(c, PREV, [{"代码": "600001", "名称": "甲", "首次封板时间": "093000", "连板数": 1}], {})
    q = quote("600001", pct=10.0, pre_close=10.0, ts=datetime(2026, 9, 23, 15, 0))   # 涨停
    fill_next_day(c, PREV, {"600001": q}, TODAY)
    board = c.execute("SELECT next_board FROM board_events WHERE code='600001'").fetchone()[0]
    assert board == 1


def test_fill_next_day_ignores_quotes_not_from_today():
    c = conn()
    record_day(c, PREV, [{"代码": "600001", "名称": "甲", "首次封板时间": "093000", "连板数": 1}], {})
    stale = quote("600001", pct=3.0, pre_close=10.0, ts=datetime(2026, 9, 22, 15, 0))
    n = fill_next_day(c, PREV, {"600001": stale}, TODAY)
    assert n == 0
    row = c.execute("SELECT next_close_pct FROM board_events WHERE code='600001'").fetchone()
    assert row == (None,)


def test_fill_next_day_ignores_codes_missing_from_quotes():
    c = conn()
    record_day(c, PREV, [{"代码": "600001", "名称": "甲", "首次封板时间": "093000", "连板数": 1}], {})
    n = fill_next_day(c, PREV, {}, TODAY)
    assert n == 0


def test_fill_next_day_only_touches_unfilled_rows_of_prev_day():
    c = conn()
    record_day(c, PREV, [{"代码": "600001", "名称": "甲", "首次封板时间": "093000", "连板数": 1}], {})
    other_day = date(2026, 9, 21)
    record_day(c, other_day, [{"代码": "600002", "名称": "乙", "首次封板时间": "093000", "连板数": 1}], {})
    q1 = quote("600001", pct=1.0, pre_close=10.0, ts=datetime(2026, 9, 23, 15, 0))
    q2 = quote("600002", pct=1.0, pre_close=10.0, ts=datetime(2026, 9, 23, 15, 0))
    n = fill_next_day(c, PREV, {"600001": q1, "600002": q2}, TODAY)
    assert n == 1   # 只回填 prev_day 的记录，不动更早那天的
    row = c.execute("SELECT next_close_pct FROM board_events WHERE code='600002'").fetchone()
    assert row == (None,)


# ---- summary ----

def test_summary_aggregates_by_bucket_only_filled_rows():
    c = conn()
    record_day(c, PREV, [
        {"代码": "600001", "名称": "甲", "首次封板时间": "093000", "连板数": 1},   # 早盘
        {"代码": "600002", "名称": "乙", "首次封板时间": "093500", "连板数": 1},   # 早盘
        {"代码": "600003", "名称": "丙", "首次封板时间": "103500", "连板数": 1},   # 上午，不回填
    ], {})
    q1 = quote("600001", pct=2.0, open_pct=1.0, pre_close=10.0, ts=datetime(2026, 9, 23, 15, 0))
    q2 = quote("600002", pct=10.0, open_pct=5.0, pre_close=10.0, ts=datetime(2026, 9, 23, 15, 0))
    fill_next_day(c, PREV, {"600001": q1, "600002": q2}, TODAY)
    rows = {r["bucket"]: r for r in summary(c)}
    assert set(rows) == {"早盘"}   # 上午那条没回填，不进汇总
    morning = rows["早盘"]
    assert morning["count"] == 2
    assert morning["avg_next_open"] == round((1.0 + 5.0) / 2, 2)
    assert morning["avg_next_close"] == round((2.0 + 10.0) / 2, 2)
    assert morning["board_rate"] == 50.0   # 两条里一条封了涨停


def test_summary_empty_table_returns_empty_list():
    c = conn()
    assert summary(c) == []
