"""弱转强（dj 2026-11-03，知识库「弱转强」，7 年日线回测：昨天炸板且炸板前 ≥2 连板、今天竞价高开 0~6%、开盘价买、
次日收盘卖，日均 +0.72%，偏弱的苗头）。主账户过 Jev 排雷，影子只按规则；实盘不跟。"""
from datetime import date, datetime

from factories import position, quote

from jevq import config, db
from jevq.data.wts_pool import build_pool
from jevq.ledger import Ledger
from jevq.rules.wts import wts_candidates
from jevq.strategies.relay import WtsStrategy

TODAY = date(2026, 11, 3)
T930 = datetime(2026, 11, 3, 9, 30, 20)
MAIN, SHADOW = config.STRATEGIES["wts"]["accounts"]
CAL = [date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]


def _q(code, open_pct, ts=T930, pre=10.0):
    return quote(code, pct=open_pct, open_pct=open_pct, pre_close=pre, ts=ts)


def test_candidates_need_a_zero_to_six_percent_open_ranked_by_prior_streak():
    pool = {"600001": 3, "600002": 5, "600003": 2, "600004": 2, "300005": 4, "600006": 2, "600007": 2}
    quotes = {"600001": _q("600001", 4.0), "600002": _q("600002", 0.5), "600003": _q("600003", -0.5),
              "600004": _q("600004", 6.5), "300005": _q("300005", 3.0), "600006": _q("600006", 3.0),
              "600007": _q("600007", 3.0, ts=datetime(2026, 11, 2, 15))}
    assert [(q.code, n) for q, n in wts_candidates(quotes, pool, TODAY)] == [("600002", 5), ("600001", 3), ("600006", 2)]


def _closes(conn, code, seq):
    conn.executemany("INSERT INTO closes(date, code, close, pre_close) VALUES (?, ?, ?, ?)",
                     [(d, code, c, p) for d, c, p in seq])


def test_pool_keeps_yesterdays_broken_boards_that_had_two_or_more_boards_before():
    conn = db.connect(":memory:")
    # 600001：10-29/10-30 两连板，11-02 摸过涨停没封住（收 13.0 < 13.31）→ 入池，前 2 板
    _closes(conn, "600001", [("2026-10-28", 10.0, 9.5), ("2026-10-29", 11.0, 10.0), ("2026-10-30", 12.1, 11.0),
                             ("2026-11-02", 13.0, 12.1)])
    # 600002：同样两连板，11-02 继续涨停 → 不是断板，不入池
    _closes(conn, "600002", [("2026-10-28", 10.0, 9.5), ("2026-10-29", 11.0, 10.0), ("2026-10-30", 12.1, 11.0),
                             ("2026-11-02", 13.31, 12.1)])
    # 600003：只有 1 板就断 → 不入池
    _closes(conn, "600003", [("2026-10-28", 10.0, 9.5), ("2026-10-29", 10.5, 10.0), ("2026-10-30", 11.55, 10.5),
                             ("2026-11-02", 11.0, 11.55)])
    # 600004：两连板后 11-02 直接大跌，最高没摸到涨停 → 是断板但不是炸板，不入池
    _closes(conn, "600004", [("2026-10-28", 10.0, 9.5), ("2026-10-29", 11.0, 10.0), ("2026-10-30", 12.1, 11.0),
                             ("2026-11-02", 11.5, 12.1)])
    highs = {"600001": 13.31, "600004": 12.2}                                   # 昨日最高价

    def fetch(code):
        return [{"day": "2026-10-30", "high": 12.1, "close": 12.1}, {"day": "2026-11-02", "high": highs[code], "close": 13.0}]
    assert build_pool(conn, ["600001", "600002", "600003", "600004"], TODAY, fetch) == {"600001": 2}


def test_pool_skips_codes_whose_daily_bars_cannot_be_fetched():
    conn = db.connect(":memory:")
    _closes(conn, "600001", [("2026-10-28", 10.0, 9.5), ("2026-10-29", 11.0, 10.0), ("2026-10-30", 12.1, 11.0),
                             ("2026-11-02", 13.0, 12.1)])

    def boom(code):
        raise TimeoutError("sina 超时")
    assert build_pool(conn, ["600001"], TODAY, boom) == {}


class Judge:
    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {}

    def check(self, code, *args):
        return self.verdicts.get(code, ("pass", "无雷"))


def _strategy(conn, notice=None, can_buy=True):
    ledger = Ledger(conn)
    pool = {"600001": 3, "600002": 2}
    return WtsStrategy(conn, ledger, Judge(notice), Judge(), pool, trade_days=CAL, can_buy=can_buy), ledger


def test_wts_buys_at_the_open_price_main_skips_jev_rejects_and_sells_next_day_near_close():
    conn = db.connect(":memory:")
    s, ledger = _strategy(conn, notice={"600002": ("reject", "立案调查")})
    quotes = {"600001": _q("600001", 4.0, pre=10.0), "600002": _q("600002", 3.0)}
    fills = s.buy_step(quotes, T930)
    assert sorted((f.account, f.code, f.price) for f in fills) == [(MAIN, "600001", 10.4), (SHADOW, "600001", 10.4),
                                                                    (SHADOW, "600002", 10.3)]
    assert all(f.reason.startswith(("弱转强", "影子｜弱转强")) for f in fills)
    assert s.buy_step(quotes, datetime(2026, 11, 3, 9, 31)) == []
    q_next = quote("600001", pct=2.0, ts=datetime(2026, 11, 4, 14, 56))
    assert s.sell_step({"600001": q_next}, datetime(2026, 11, 4, 14, 50)) == []
    sold = s.sell_step({"600001": q_next}, datetime(2026, 11, 4, 14, 56))
    assert sorted(f.account for f in sold) == [MAIN, SHADOW] and all(f.side == "sell" for f in sold)


def test_wts_accounts_have_their_own_capital():
    ledger = Ledger(db.connect(":memory:"))
    assert ledger.cash(MAIN) == config.WTS_CAPITAL and ledger.free_slots(SHADOW) == config.WTS_MAX_POSITIONS
