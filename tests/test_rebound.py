"""超跌反弹：连板排除、排序、买入时机、策略运行时。"""
import json
from datetime import date, datetime

from factories import position, quote

from jevq import db
from jevq.data.closes import consecutive_board_codes
from jevq.ledger import Ledger
from jevq.rules.buy import rank_rebound, rebound_mode
from jevq.strategies.rebound import ReboundStrategy

TODAY = date(2026, 9, 24)
PREV_DAY = date(2026, 9, 23)
DAY = datetime(2026, 9, 24)


def at(h, m, d=DAY):
    return d.replace(hour=h, minute=m)


def _dates(n):
    """TODAY 之前的 n 个"交易日"（测试里连续自然日即可）。"""
    from datetime import timedelta
    return [(TODAY - timedelta(days=n - i)).isoformat() for i in range(n)]


def _seed(conn, code, prices, dates):
    conn.executemany("INSERT INTO closes(date, code, close) VALUES (?, ?, ?)",
                     [(d, code, p) for d, p in zip(dates, prices) if p is not None])
    conn.commit()


# ---- 连板排除 ----

def test_consecutive_board_codes_finds_two_board_streak_within_lookback():
    conn = db.connect(":memory:")
    dates = _dates(41)
    flat = [10.0] * 41
    two = [10.0] * 41
    two[10], two[11] = 11.0, 12.1              # 连续两天涨停
    one = [10.0] * 41
    one[10] = 11.0                             # 单个涨停
    _seed(conn, "600001", two, dates)
    _seed(conn, "600002", one, dates)
    _seed(conn, "600003", flat, dates)
    codes, n = consecutive_board_codes(conn, ["600001", "600002", "600003"], TODAY)
    assert codes == {"600001"}
    assert n == 41


def test_consecutive_board_codes_ignores_streak_outside_lookback():
    conn = db.connect(":memory:")
    dates = _dates(50)
    p = [10.0] * 50
    p[1], p[2] = 11.0, 12.1                    # 在最近 41 个日期之外
    _seed(conn, "600001", p, dates)
    codes, n = consecutive_board_codes(conn, ["600001"], TODAY)
    assert codes == set()
    assert n == 41


def test_consecutive_board_codes_does_not_bridge_data_gap():
    """缺数据的日子是断点：涨停、缺一天、涨停 不算连板。"""
    conn = db.connect(":memory:")
    dates = _dates(10)
    p = [10.0, 11.0, None, 12.1, 12.1, 12.1, 12.1, 12.1, 12.1, 12.1]
    _seed(conn, "600001", p, dates)
    _seed(conn, "600009", [10.0] * 10, dates)   # 让全局日期表里有缺口那天
    codes, n = consecutive_board_codes(conn, ["600001"], TODAY)
    assert codes == set()
    assert n == 10


def test_consecutive_board_codes_with_short_history_uses_what_it_has():
    conn = db.connect(":memory:")
    assert consecutive_board_codes(conn, ["600001"], TODAY) == (set(), 0)


# ---- 排序与买入时机 ----

def test_rank_rebound_filters_ret5_range_and_excluded_and_sorts_ascending():
    qs = [quote("600001", pct=-1.0), quote("600002", pct=-2.0), quote("600003", pct=-1.5),
          quote("600004", pct=-0.5), quote("600005", pct=-4.0), quote("600006", pct=-1.0)]
    ret5 = {"600001": -5.0, "600002": -18.0, "600003": -25.0, "600004": 3.0, "600005": -10.0,
            "600006": -12.0}
    ranked = rank_rebound(qs, ret5, excluded={"600006"})
    # 600003 跌超 20%、600004 5 日上涨、600005 当日跌超 3%、600006 连板排除
    assert [q.code for q in ranked] == ["600002", "600001"]


def test_rank_rebound_bounds_are_inclusive():
    qs = [quote("600001", pct=0.0), quote("600002", pct=-3.0)]
    ranked = rank_rebound(qs, {"600001": 0.0, "600002": -20.0}, excluded=set())
    assert [q.code for q in ranked] == ["600002", "600001"]


def test_rebound_mode_only_looks_at_sentiment():
    assert rebound_mode("正常") == "intraday"
    assert rebound_mode("火热") == "intraday"
    assert rebound_mode("偏弱") == "tail"
    assert rebound_mode("冰点") == "tail"
    assert rebound_mode(None) == "tail"


# ---- 运行时 ----

class Judge:
    def __init__(self, verdicts=None):
        self.verdicts, self.calls, self.last_trace = verdicts or {}, [], None

    def check(self, code, *args):
        self.calls.append(code)
        return self.verdicts.get(code, ("pass", "ok"))


UNIVERSE = {"600001": "甲", "600002": "乙", "600003": "丙"}


def make(ret5=None, excluded=(), sentiment="正常", notice=None, enabled=True):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    state = {"sentiment": sentiment}
    strat = ReboundStrategy(conn, ledger, notice or Judge(), Judge(), UNIVERSE,
                            ret5 if ret5 is not None else {"600001": -5.0, "600002": -15.0, "600003": -8.0},
                            set(excluded), PREV_DAY, lambda: state["sentiment"], enabled=enabled)
    return strat, conn, ledger, state


def _quotes(t, **pcts):
    return {c: quote(c, pct=p, amount=2e8, ts=t) for c, p in pcts.items()}


def test_intraday_buy_picks_deepest_five_day_drop():
    strat, conn, ledger, _ = make()
    fills = strat.on_minute(_quotes(at(10, 0), **{"600001": -1.0, "600002": -1.0, "600003": -1.0}), at(10, 0))
    assert {(f.account, f.code) for f in fills} == {("rebound_main", "600002"), ("rebound_shadow", "600002")}
    trace = json.loads(conn.execute("SELECT trace FROM decisions WHERE strategy='rebound'").fetchone()[0])
    assert trace["strategy"] == "rebound"
    assert trace["candidate"]["ret5"] == -15.0 and trace["candidate"]["rank"] == 1
    assert trace["market"]["mode"] == "intraday" and "overseas" not in trace["market"]


def test_weak_sentiment_waits_for_tail():
    strat, conn, ledger, state = make(sentiment="偏弱")
    q = {"600002": -1.0}
    assert strat.on_minute(_quotes(at(10, 0), **q), at(10, 0)) == []
    assert len(strat.on_minute(_quotes(at(14, 50), **q), at(14, 50))) == 2


def test_jev_reject_only_blocks_main():
    strat, conn, ledger, _ = make(notice=Judge({"600002": ("reject", "新增减持")}))
    fills = strat.on_minute(_quotes(at(10, 0), **{"600001": -1.0, "600002": -1.0}), at(10, 0))
    assert {(f.account, f.code) for f in fills} == {("rebound_shadow", "600002"), ("rebound_main", "600001")}
    row = conn.execute("SELECT verdict FROM decisions WHERE code='600002' AND strategy='rebound'").fetchone()
    assert row[0] == "reject"


def test_disabled_only_sells():
    strat, conn, ledger, _ = make(enabled=False)
    ledger.ensure_account("rebound_main", 30000)
    p = position("600001", account="rebound_main", buy_price=10.0, buy_date=PREV_DAY)
    conn.execute("INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?)",
                 (p.account, p.code, p.shares, p.cost, p.buy_price, p.buy_date.isoformat()))
    conn.commit()
    fills = strat.on_minute(_quotes(at(10, 0), **{"600001": -3.5, "600002": -1.0}), at(10, 0))
    assert [(f.side, f.reason) for f in fills] == [("sell", "止损")]


def test_stale_quotes_are_ignored():
    strat, conn, ledger, _ = make()
    old = datetime(2026, 9, 23, 15, 0)
    assert strat.on_minute(_quotes(old, **{"600002": -1.0}), at(10, 0)) == []


def test_rejected_restored_after_restart():
    notice = Judge({"600002": ("reject", "雷")})
    strat, conn, ledger, _ = make(notice=notice)
    strat.on_minute(_quotes(at(10, 0), **{"600002": -1.0}), at(10, 0))
    strat2 = ReboundStrategy(conn, ledger, notice, Judge(), UNIVERSE, {"600002": -15.0}, set(), PREV_DAY,
                             lambda: "正常")
    strat2.on_minute(_quotes(at(10, 1), **{"600002": -1.0}), at(10, 1))
    assert notice.calls == ["600002"]


def _hold(conn, code="600002", buy_price=10.0, buy_date=PREV_DAY):
    conn.execute("INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?)",
                 ("rebound_main", code, 1000, buy_price * 1000 + 5, buy_price, buy_date.isoformat()))
    conn.commit()


def test_holds_overnight_on_first_day_and_clears_on_second():
    calendar = [date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    _hold(conn)
    strat = ReboundStrategy(conn, ledger, Judge(), Judge(), UNIVERSE, {}, set(), PREV_DAY, lambda: "正常",
                            enabled=False, trade_days=calendar)
    assert strat.on_minute(_quotes(at(14, 56), **{"600002": 0.5}), at(14, 56)) == []
    d2 = datetime(2026, 9, 25, 14, 56)
    fills = strat.on_minute({"600002": quote("600002", pct=0.5, ts=d2)}, d2)
    assert [(f.side, f.reason) for f in fills] == [("sell", "到期清仓")]


def test_trailing_take_profit_in_strategy():
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    _hold(conn)
    strat = ReboundStrategy(conn, ledger, Judge(), Judge(), UNIVERSE, {}, set(), PREV_DAY, lambda: "正常",
                            enabled=False, trade_days=[PREV_DAY, TODAY])
    q = quote("600002", pct=1.7, ts=at(10, 30))
    q = type(q)(**{**q.__dict__, "high": 10.25})
    fills = strat.on_minute({"600002": q}, at(10, 30))
    assert [(f.side, f.reason) for f in fills] == [("sell", "跟踪止盈")]
