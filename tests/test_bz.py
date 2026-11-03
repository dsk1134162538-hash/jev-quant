"""补涨（dj 2026-11-03，research/theme_ladder.py）：题材龙头 ≥5 板，同题材昨日首板，竞价高开 0~3% 开盘价买，次日 14:55 后卖。"""
from datetime import date, datetime

from factories import quote

from jevq import config, db
from jevq.ledger import Ledger
from jevq.strategies.relay import BzStrategy

T930 = datetime(2026, 11, 3, 9, 30, 20)
MAIN, SHADOW = config.STRATEGIES["bz"]["accounts"]
CAL = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]


class Judge:
    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {}

    def check(self, code, *args):
        return self.verdicts.get(code, ("pass", "无雷"))


def _q(code, open_pct):
    return quote(code, pct=open_pct, open_pct=open_pct, pre_close=10.0, ts=T930)


def test_bz_buys_small_gap_up_followers_of_a_five_board_leader_at_the_open_price():
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    pool = {"600001": {"theme": "机器人", "leader": 6}, "600002": {"theme": "机器人", "leader": 6},
            "600003": {"theme": "算力", "leader": 5}}
    s = BzStrategy(conn, ledger, Judge({"600002": ("reject", "立案调查")}), Judge(), pool, trade_days=CAL)
    quotes = {"600001": _q("600001", 1.5), "600002": _q("600002", 2.0), "600003": _q("600003", 4.0)}   # 600003 高开 4%：不买
    fills = s.buy_step(quotes, T930)
    assert sorted((f.account, f.code, f.price) for f in fills) == [(MAIN, "600001", 10.15), (SHADOW, "600001", 10.15),
                                                                    (SHADOW, "600002", 10.2)]
    assert all("补涨｜机器人 龙头6板" in f.reason for f in fills)
    q_next = quote("600001", pct=1.0, ts=datetime(2026, 11, 4, 14, 56))
    sold = s.sell_step({"600001": q_next}, datetime(2026, 11, 4, 14, 56))
    assert sorted(f.account for f in sold) == [MAIN, SHADOW]


def test_bz_accounts_have_their_own_capital():
    ledger = Ledger(db.connect(":memory:"))
    assert ledger.cash(MAIN) == config.BZ_CAPITAL and ledger.free_slots(SHADOW) == config.BZ_MAX_POSITIONS
    assert ledger.cash("aiflex_main") == config.FLEX_CAPITAL and ledger.free_slots("aiflex_main") == config.FLEX_MAX_POSITIONS
