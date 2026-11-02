"""高标竞价接力（dj 2026-11-02）：昨天 3 板以上、今天竞价高开 3~6% 的主板票，开盘价买，下一交易日收盘前卖。
回测 research/dragon_relay.py：训练 +1.12%/笔、验证 +1.86%/笔。主账户过 Jev 公告/消息排雷，影子只按规则。"""
from datetime import date, datetime

from factories import position, quote

from jevq import config, db
from jevq.data.closes import limit_streaks
from jevq.ledger import Ledger
from jevq.rules.relay import decide_relay_sell, relay_candidates
from jevq.strategies.relay import RelayStrategy

TODAY = date(2026, 11, 3)
T930 = datetime(2026, 11, 3, 9, 30, 20)
MAIN, SHADOW = config.STRATEGIES["relay"]["accounts"]
CAL = [date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4),
       date(2026, 11, 5)]


def _closes(conn, code, seq):
    """seq：从旧到新的 (date, close, pre_close)。"""
    conn.executemany("INSERT INTO closes(date, code, close, pre_close) VALUES (?, ?, ?, ?)",
                     [(d, code, c, p) for d, c, p in seq])


def test_limit_streaks_count_consecutive_limit_up_closes_before_today():
    conn = db.connect(":memory:")
    _closes(conn, "600001", [("2026-10-28", 10.0, 9.8), ("2026-10-29", 11.0, 10.0), ("2026-10-30", 12.1, 11.0),
                             ("2026-11-02", 13.31, 12.1)])                      # 3 连板
    _closes(conn, "600002", [("2026-10-30", 11.0, 10.0), ("2026-11-02", 11.5, 11.0)])   # 断板
    _closes(conn, "600003", [("2026-11-02", 11.0, 10.0), ("2026-11-03", 12.1, 11.0)])   # 今天的不算
    assert limit_streaks(conn, ["600001", "600002", "600003"], TODAY) == {"600001": 3, "600003": 1}


def test_limit_streaks_fall_back_to_previous_close_when_pre_close_is_missing():
    # 10-30 的收盘价是 15:32 补抓的，pre_close 是空的（2026-11-02 live 库）：不能因此把连板算断
    conn = db.connect(":memory:")
    _closes(conn, "002708", [("2026-10-29", 11.0, 10.0), ("2026-10-30", 12.1, None), ("2026-11-02", 13.31, 12.1)])
    assert limit_streaks(conn, ["002708"], TODAY) == {"002708": 3}


def _q(code, open_pct, pct=None, ts=T930, pre=10.0):
    return quote(code, pct=open_pct if pct is None else pct, open_pct=open_pct, pre_close=pre, ts=ts)


def test_candidates_need_three_boards_and_a_three_to_six_percent_open_ranked_by_streak():
    quotes = {"600001": _q("600001", 4.0), "600002": _q("600002", 5.5), "600003": _q("600003", 2.0),
              "600004": _q("600004", 7.0), "600005": _q("600005", 4.0), "600006": _q("600006", 3.0),
              "300007": _q("300007", 4.0), "600008": _q("600008", 4.0, ts=datetime(2026, 11, 2, 15))}
    streaks = {"600001": 3, "600002": 5, "600003": 4, "600004": 3, "600005": 2, "600006": 4, "300007": 5,
               "600008": 6}
    got = relay_candidates(quotes, streaks, TODAY)
    assert [q.code for q, s in got] == ["600002", "600006", "600001"]


def test_sell_the_next_trading_day_near_the_close_or_at_once_if_overdue():
    pos = position("600001", account=MAIN, buy_date=date(2026, 11, 3))
    q = quote("600001", pct=2.0, ts=datetime(2026, 11, 4, 14, 55))
    assert not decide_relay_sell(pos, q, datetime(2026, 11, 3, 14, 56), CAL).sell        # 买入当天 T+1 卖不了
    assert not decide_relay_sell(pos, q, datetime(2026, 11, 4, 14, 50), CAL).sell
    assert decide_relay_sell(pos, q, datetime(2026, 11, 4, 14, 55), CAL).sell
    assert decide_relay_sell(pos, q, datetime(2026, 11, 5, 9, 31), CAL).sell             # 漏了收盘：下一天开盘就走


class Judge:
    def __init__(self, verdicts):
        self.verdicts = verdicts

    def check(self, code, *args):
        return self.verdicts.get(code, ("pass", "无雷"))


def _strategy(conn, notice=None, can_buy=True):
    ledger = Ledger(conn)
    streaks = {"600001": 3, "600002": 5, "600006": 4, "600009": 4}
    return RelayStrategy(conn, ledger, Judge(notice or {}), Judge({}), streaks, trade_days=CAL, can_buy=can_buy), ledger


def test_buys_at_the_open_price_once_per_day_and_main_skips_jev_rejects():
    conn = db.connect(":memory:")
    s, ledger = _strategy(conn, notice={"600002": ("reject", "立案调查")})
    quotes = {"600001": _q("600001", 4.0, pct=6.0), "600002": _q("600002", 5.5), "600006": _q("600006", 3.0),
              "600009": _q("600009", 4.5)}
    fills = s.buy_step(quotes, T930)
    main = sorted((f.code, f.price) for f in fills if f.account == MAIN)
    shadow = sorted(f.code for f in fills if f.account == SHADOW)
    assert shadow == ["600002", "600006", "600009"]                         # 最多 3 只，按连板数
    assert main == [("600001", 10.4), ("600006", 10.3), ("600009", 10.45)]   # 600002 被排雷；按开盘价成交
    assert all(f.shares * f.price <= config.RELAY_PER_TRADE for f in fills)
    assert s.buy_step(quotes, datetime(2026, 11, 3, 9, 31)) == []           # 当天不再买


def test_main_retries_next_minute_when_the_jev_budget_ran_out():
    # 09:30 扫首板先判 Jev，时间预算可能用完；主账户下一分钟（还在 09:33 前）补判，影子不重复买
    conn = db.connect(":memory:")
    s, _ = _strategy(conn)
    quotes = {"600006": _q("600006", 3.0)}
    first = s.buy_step(quotes, T930, deadline=0.0)
    assert [f.account for f in first] == [SHADOW]
    again = s.buy_step(quotes, datetime(2026, 11, 3, 9, 31), deadline=None)
    assert [(f.account, f.price) for f in again] == [(MAIN, 10.3)]
    assert s.buy_step(quotes, datetime(2026, 11, 3, 9, 32)) == []


def test_no_buys_after_the_opening_minutes_or_on_sell_only_days():
    conn = db.connect(":memory:")
    s, _ = _strategy(conn)
    quotes = {"600006": _q("600006", 3.0, ts=datetime(2026, 11, 3, 9, 40))}
    assert s.buy_step(quotes, datetime(2026, 11, 3, 9, 40)) == []
    s2, _ = _strategy(db.connect(":memory:"), can_buy=False)
    assert s2.buy_step({"600006": _q("600006", 3.0)}, T930) == []


def test_relay_accounts_hold_several_positions_with_their_own_capital():
    ledger = Ledger(db.connect(":memory:"))
    assert ledger.cash(MAIN) == config.RELAY_CAPITAL and ledger.free_slots(MAIN) == config.RELAY_MAX_POSITIONS
    assert ledger.free_slots("board_main") == config.MAX_POSITIONS
