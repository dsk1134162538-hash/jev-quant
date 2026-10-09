"""扫首板 2026-10-09 细化（用户定）：只做主线的首板；扫板那一刻同时段缩量（< 1.5 倍）不扫。"""
from datetime import date, datetime

from factories import quote

from jevq import db
from jevq.data.tencent import parse_day_minutes
from jevq.ledger import Ledger
from jevq.rules.board import Candidate, same_time_ratio, watch_candidates
from jevq.strategies.board import BoardStrategy

TODAY = date(2026, 9, 24)
TS = datetime(2026, 9, 24, 10, 0)


def q(code, pct, amount=1e8):
    return quote(code, pct=pct, amount=amount, ts=TS)


def _theme(n, lead_pct=8.5):
    """一个题材 n 只强势股，600001 是龙头（涨 8.5%、成交额最大）。"""
    quotes = {"600001": q("600001", lead_pct, amount=9e8)}
    for i in range(2, n + 1):
        quotes[f"60000{i}"] = q(f"60000{i}", 6)
    return quotes, {c: ["机器人"] for c in quotes}


def test_only_mainline_themes_with_five_strong_members():
    quotes, cmap = _theme(4)
    ret5, prev = {c: 1.0 for c in quotes}, {"600001": 9.5}
    assert watch_candidates(quotes, cmap, ret5, prev, TODAY, mainline_min=5) == []
    quotes, cmap = _theme(5)
    ret5 = {c: 1.0 for c in quotes}
    assert [c.code for c in watch_candidates(quotes, cmap, ret5, prev, TODAY, mainline_min=5)] == ["600001"]


def test_article_mention_makes_a_smaller_theme_mainline_but_not_below_hot_min():
    quotes, cmap = _theme(3)
    ret5, prev = {c: 1.0 for c in quotes}, {"600001": 9.5}
    hot = lambda concept, members: "板块：机器人"          # noqa: E731
    got = watch_candidates(quotes, cmap, ret5, prev, TODAY, article_hot=hot, mainline_min=5)
    assert [c.code for c in got] == ["600001"] and got[0].article_hot == "板块：机器人"
    quotes, cmap = _theme(2)                                   # 点名了也要 ≥3 只强势
    assert watch_candidates(quotes, cmap, {c: 1.0 for c in quotes}, prev, TODAY, article_hot=hot,
                            mainline_min=5) == []


def test_same_time_ratio_uses_prior_days_at_the_same_minute():
    days = {"2026-09-22": {"0930": 100, "0945": 1000}, "2026-09-23": {"0930": 200, "0944": 2000},
            "2026-09-24": {"0945": 99999}}
    assert same_time_ratio(4500, days, "2026-09-24", "0945") == 3.0          # (1000 + 2000) / 2 = 1500
    assert same_time_ratio(4500, {"2026-09-24": {"0945": 1}}, "2026-09-24", "0945") is None
    assert same_time_ratio(0, days, "2026-09-24", "0945") is None


def test_parse_day_minutes_reads_five_days():
    body = {"data": {"sz002658": {"data": [
        {"date": "20261009", "data": ["0930 7.13 35 24955.00", "0931 7.15 327 233121.00", "bad"]},
        {"date": "20261008", "data": ["0930 7.0 10 7000"]}]}}}
    assert parse_day_minutes(body, "sz002658") == {"2026-10-09": {"0930": 35, "0931": 327},
                                                   "2026-10-08": {"0930": 10}}


class Judge:
    last_trace = None

    def check(self, code, *args):
        return "pass", "ok"


def _board(volume, curves):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    now = datetime(2026, 9, 24, 9, 45)
    q9 = quote("600001", pct=9.2, pre_close=10.0, ts=now, volume=volume)
    strat = BoardStrategy(conn, ledger, Judge(), Judge(), lambda codes: {"600001": q9}, {"600001": "甲"},
                          {"600001": 1.0}, {"600001": ["机器人"]}, {"600001": 9.5}, date(2026, 9, 23),
                          fetch_day_minutes=lambda code: curves)
    strat.watch = {"600001": Candidate("600001", "机器人", 5, 4, {})}
    strat.verdicts = {"600001": ("pass", "ok", {"outcome": {"bought": False}})}
    return strat, conn, ledger, now


def test_shrinking_board_is_not_scanned_and_logged_once():
    curves = {"2026-09-22": {"0945": 1000}, "2026-09-23": {"0945": 1000}}
    strat, conn, ledger, now = _board(1200, curves)                # 1.2 倍：缩量
    assert strat.on_fast(now) == [] and strat.on_fast(now) == []
    rows = conn.execute("SELECT verdict, detail FROM decisions WHERE strategy='board'").fetchall()
    assert rows == [("skip", "缩量板不扫：同时段 1.2 倍（< 1.5）")]


def test_expanding_board_is_scanned_and_ratio_goes_into_the_reason():
    curves = {"2026-09-22": {"0945": 1000}, "2026-09-23": {"0945": 1000}}
    strat, conn, ledger, now = _board(6400, curves)                # 6.4 倍
    fills = strat.on_fast(now)
    main = next(f for f in fills if f.account == "board_main")
    assert main.reason == "扫板9.2%｜量6.4倍"
    detail = conn.execute("SELECT detail FROM decisions WHERE verdict='pass'").fetchone()[0]
    assert "同时段放量 6.4 倍" in detail


def test_missing_minute_history_does_not_block_the_scan():
    strat, conn, ledger, now = _board(100, {})
    assert {f.account for f in strat.on_fast(now)} == {"board_main", "board_shadow"}
