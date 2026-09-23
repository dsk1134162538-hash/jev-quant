from datetime import date, timedelta

import pandas as pd

from jevq import db
from jevq.data.akdata import Notice, filter_universe, notices_from_frame, summarize_pools
from jevq.data.notice_store import notices_for, sync_notices

DAYS = [date(2026, 6, 1) + timedelta(days=i) for i in range(120)]


def test_filter_universe():
    today = DAYS[-1]
    rows = [
        ("600001", "老股份", date(2010, 1, 1)),
        ("000002", "*ST退市", date(2010, 1, 1)),
        ("300001", "创业板", date(2010, 1, 1)),
        ("688001", "科创板", date(2010, 1, 1)),
        ("600003", "次新股", DAYS[-59]),     # 含今天 59 个交易日
        ("600004", "够60天", DAYS[-60]),
    ]
    assert filter_universe(rows, today, DAYS) == {"600001": "老股份", "600004": "够60天"}


def test_notices_from_frame():
    df = pd.DataFrame([
        {"代码": "600001", "名称": "甲", "公告标题": "甲:关于股东减持计划的公告", "公告类型": "股东减持",
         "公告日期": "2026-09-22", "网址": "https://data.eastmoney.com/notices/detail/600001/AN202609221.html"},
        {"代码": "300001", "名称": "乙", "公告标题": "乙:年报", "公告类型": "年报",
         "公告日期": "2026-09-22", "网址": "https://x/AN202609222.html"},
    ])
    assert notices_from_frame(df) == [
        Notice("AN202609221", "600001", "甲:关于股东减持计划的公告", "股东减持", "2026-09-22")]


def test_summarize_pools():
    assert summarize_pools(60, 3, 20, 6) == {"涨停家数": 60, "跌停家数": 3, "炸板率%": 25.0, "最高连板": 6}
    assert summarize_pools(0, 0, 0, 0)["炸板率%"] == 0.0


def test_sync_and_query_notices():
    conn = db.connect(":memory:")
    today = date(2026, 9, 23)
    calls = []

    def fake_fetch(d):
        calls.append(d)
        return [Notice(f"AN{d:%Y%m%d}", "600001", f"公告{d}", "其他", d.isoformat())]

    assert sync_notices(conn, today, fetch=fake_fetch, lookback=3) == 4
    assert len(calls) == 4
    calls.clear()
    sync_notices(conn, today, fetch=fake_fetch, lookback=3)
    assert calls == [today]                           # 历史日期已拉过，只重拉今天
    got = notices_for(conn, "600001", today, lookback=2)
    assert [n.date for n in got] == ["2026-09-23", "2026-09-22", "2026-09-21"]
