"""情绪轮动（用户 2026-10-09）：按大盘情绪跟扫首板/低吸/超跌反弹主账户的成交，实盘跟它。"""
from datetime import datetime

from factories import quote

from jevq import config, db
from jevq.ledger import Ledger
from jevq.models import Fill
from jevq.strategies.regime import ACCOUNT, RegimeStrategy, allowed_sources

DAY = datetime(2026, 10, 12)


def at(h, m):
    return DAY.replace(hour=h, minute=m)


def test_allowed_sources_follow_the_sentiment_map():
    assert allowed_sources("火热", False, at(9, 40)) == ("board",)
    assert allowed_sources("正常", False, at(9, 40)) == ("board",)              # 10:30 前只等扫首板
    assert allowed_sources("正常", False, at(10, 30)) == ("board", "lowbuy")
    assert allowed_sources("偏弱", False, at(10, 0)) == ("lowbuy",)
    assert allowed_sources("冰点", False, at(10, 0)) == ()
    assert allowed_sources(None, False, at(10, 0)) == ()
    assert allowed_sources("冰点", True, at(13, 10)) == ("rebound",)           # 拐点抄底
    assert allowed_sources("偏弱", True, at(13, 10)) == ("rebound", "lowbuy")


class S:
    def __init__(self, sentiment="正常", turn=False):
        self.sentiment, self.turn = sentiment, turn


def _regime(state):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    return RegimeStrategy(conn, ledger, lambda: state.sentiment, lambda: state.turn), conn, ledger


def _q(code, price, t):
    q = quote(code, pct=0.0, ts=t)
    return type(q)(**{**q.__dict__, "price": price})


def test_follows_board_buy_on_hot_day_and_sells_when_board_sells():
    st = S("火热")
    r, conn, ledger = _regime(st)
    t = at(9, 41)
    quotes = {"600001": _q("600001", 10.9, t)}
    buy = Fill("board_main", "600001", "buy", 10.9, 2700, 7.5, t, "扫板9.1%")
    [got] = r.mirror([buy, Fill("board_shadow", "600001", "buy", 10.9, 2700, 7.5, t, "影子")], quotes.get, t)
    assert (got.account, got.side, got.price) == (ACCOUNT, "buy", 10.9)
    assert got.reason == "轮动[火热]跟扫首板｜扫板9.1%"
    t2 = datetime(2026, 10, 13, 10, 0)
    quotes = {"600001": _q("600001", 11.5, t2)}
    [sold] = r.mirror([Fill("board_main", "600001", "sell", 11.5, 2700, 9.0, t2, "冲高回落")], quotes.get, t2)
    assert sold.side == "sell" and sold.price == 11.5 and sold.reason == "跟扫首板卖｜冲高回落"
    assert ledger.positions(ACCOUNT) == [] and conn.execute("SELECT COUNT(*) FROM regime_sources").fetchone()[0] == 0


def test_ignores_sources_the_sentiment_does_not_allow():
    r, conn, ledger = _regime(S("偏弱"))
    t = at(9, 41)
    quotes = {"600001": _q("600001", 10.9, t), "600002": _q("600002", 9.9, t)}
    assert r.mirror([Fill("board_main", "600001", "buy", 10.9, 100, 5, t, "扫板")], quotes.get, t) == []
    [got] = r.mirror([Fill("lowbuy_main", "600002", "buy", 9.9, 100, 5, t, "低吸-1%")], quotes.get, t)
    assert got.code == "600002" and "跟低吸" in got.reason


def test_ice_day_buys_nothing_until_turn_then_rebound():
    st = S("冰点")
    r, conn, ledger = _regime(st)
    t = at(13, 10)
    quotes = {"600003": _q("600003", 5.0, t)}
    f = Fill("rebound_main", "600003", "buy", 5.0, 100, 5, t, "拐点抄底｜超跌-1%")
    assert r.mirror([f], quotes.get, t) == []
    st.turn = True
    [got] = r.mirror([f], quotes.get, t)
    assert got.reason.startswith("轮动[冰点·拐点]跟超跌反弹")


def test_one_position_and_priority_within_the_same_minute():
    r, conn, ledger = _regime(S("正常"))
    t = at(10, 31)
    quotes = {c: _q(c, 10.0, t) for c in ("600001", "600002")}
    fills = [Fill("lowbuy_main", "600002", "buy", 10.0, 100, 5, t, "低吸"),
             Fill("board_main", "600001", "buy", 10.0, 100, 5, t, "扫板")]
    [got] = r.mirror(fills, quotes.get, t)
    assert got.code == "600001"                                     # 正常：扫首板优先
    assert r.mirror([Fill("lowbuy_main", "600009", "buy", 10.0, 100, 5, t, "低吸")],
                    {"600009": _q("600009", 10.0, t)}.get, t) == []   # 满仓不再买


def test_only_sells_on_the_source_it_followed():
    r, conn, ledger = _regime(S("偏弱"))
    t = at(10, 0)
    quotes = {"600002": _q("600002", 9.9, t)}
    r.mirror([Fill("lowbuy_main", "600002", "buy", 9.9, 100, 5, t, "低吸")], quotes.get, t)
    t2 = datetime(2026, 10, 13, 10, 0)
    quotes = {"600002": _q("600002", 10.1, t2)}
    assert r.mirror([Fill("board_main", "600002", "sell", 10.1, 100, 5, t2, "x")], quotes.get, t2) == []
    assert len(r.mirror([Fill("lowbuy_main", "600002", "sell", 10.1, 100, 5, t2, "摸到+1%")], quotes.get, t2)) == 1


def test_reconcile_sells_when_source_no_longer_holds_and_not_bought_today():
    r, conn, ledger = _regime(S("偏弱"))
    t = at(10, 0)
    quotes = {"600002": _q("600002", 9.9, t)}
    ledger.buy("lowbuy_main", quotes["600002"], t, "低吸")
    r.mirror([Fill("lowbuy_main", "600002", "buy", 9.9, 100, 5, t, "低吸")], quotes.get, t)
    assert r.reconcile(quotes.get, t) == []                         # 原策略还拿着、而且今天买的
    t2 = datetime(2026, 10, 13, 10, 0)
    quotes = {"600002": _q("600002", 10.1, t2)}
    assert r.reconcile(quotes.get, t2) == []                        # 原策略还拿着
    ledger.sell("lowbuy_main", ledger.positions("lowbuy_main")[0], quotes["600002"], t2, "x")   # 跟卖那一下漏了
    [got] = r.reconcile(quotes.get, t2)
    assert got.reason == "原策略已卖出，补卖" and got.price == 10.1


def test_no_buys_on_sell_only_days():
    conn = db.connect(":memory:")
    r = RegimeStrategy(conn, Ledger(conn), lambda: "火热", can_buy=False)
    t = at(9, 41)
    assert r.mirror([Fill("board_main", "600001", "buy", 10.9, 100, 5, t, "扫板")],
                    {"600001": _q("600001", 10.9, t)}.get, t) == []


# ---- 10-14 用户定：情绪轮动按大盘环境跟（全市场 5 日中位数）；盘中情绪只把关（冰点不买、拐点超跌优先） ----

def test_weak_market_follows_rebound_only():
    """大盘普跌（≤ -2%）：回测超跌反弹两日均 +1.3%~+1.7%，扫首板次日平均低开 1.5% → 只跟超跌。"""
    for s in ("火热", "正常", "偏弱"):
        assert allowed_sources(s, False, at(9, 40), market_ret5=-2.5) == ("rebound",)
    assert allowed_sources("冰点", False, at(10, 0), market_ret5=-2.5) == ("rebound",)    # 10-16 起普跌时冰点也跟


def test_neutral_or_strong_market_follows_board_then_lowbuy_after_1030():
    for s in ("火热", "正常", "偏弱"):
        assert allowed_sources(s, False, at(9, 40), market_ret5=0.3) == ("board",)
        assert allowed_sources(s, False, at(10, 30), market_ret5=2.5) == ("board", "lowbuy")
    assert allowed_sources("冰点", False, at(10, 30), market_ret5=0.3) == ()


def test_turn_still_puts_rebound_first_and_unknown_market_keeps_old_map():
    assert allowed_sources("偏弱", True, at(13, 10), market_ret5=0.3) == ("rebound", "board", "lowbuy")
    assert allowed_sources("冰点", True, at(13, 10), market_ret5=0.3) == ("rebound",)
    assert allowed_sources("偏弱", False, at(10, 0), market_ret5=None) == ("lowbuy",)      # 算不出大盘：照旧按情绪


def test_buy_reason_names_the_market_environment():
    from jevq.models import Fill
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    strat = RegimeStrategy(conn, ledger, lambda: "正常", market_ret5=-2.4)
    t = at(10, 40)
    f = Fill("rebound_main", "600001", "buy", 10.0, 1000, 5.0, t, "超跌-1.0%｜5日-8%")
    got = strat.mirror([f], lambda c: quote(c, ts=t), t)
    assert got and "大盘普跌" in got[0].reason and "跟超跌反弹" in got[0].reason


def test_ice_point_in_broad_drop_follows_rebound_only_when_switched_on(monkeypatch):
    monkeypatch.setattr(config, "REBOUND_MAIN_BUY_ON_ICE", False)
    assert allowed_sources("冰点", False, at(10, 0), market_ret5=-2.5) == ()
    monkeypatch.setattr(config, "REBOUND_MAIN_BUY_ON_ICE", True)
    assert allowed_sources("冰点", False, at(10, 0), market_ret5=-2.5) == ("rebound",)
    assert allowed_sources("冰点", False, at(10, 0), market_ret5=0.5) == ()          # 大盘不弱时冰点照样不买
