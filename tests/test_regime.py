"""情绪轮动（用户 2026-10-09）：按大盘情绪跟扫首板/低吸/超跌反弹主账户的成交，实盘跟它。"""
from datetime import date, datetime

from factories import position, quote

from jevq import db
from jevq.ledger import Ledger
from jevq.models import Fill
from jevq.rules.lowbuy import decide_lowbuy_sell, lowbuy_mode, rank_lowbuy
from jevq.strategies.lowbuy import LowbuyStrategy
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


# ---- 低吸（9-23 版规则重新上线）----

def test_lowbuy_rules_match_the_0923_design():
    assert lowbuy_mode("正常", "中性") == "intraday"
    assert lowbuy_mode("偏弱", "中性") == "tail" and lowbuy_mode("正常", "偏弱") == "tail"
    qs = [quote("600001", pct=-1.0), quote("600002", pct=-2.0), quote("600003", pct=-4.0), quote("600004", pct=1.0)]
    ranked = rank_lowbuy(qs, {"600001": 3.0, "600002": 8.0, "600003": 9.0, "600004": 9.0})
    assert [q.code for q in ranked] == ["600002", "600001"]          # 5 日涨得多的优先（强势股回调）
    p = position("600001", account="lowbuy_main", buy_date=date(2026, 10, 9))
    d1 = datetime(2026, 10, 12, 9, 31)
    assert decide_lowbuy_sell(p, quote("600001", pct=1.5, open_pct=1.5, ts=d1), d1).reason == "高开止盈"
    assert decide_lowbuy_sell(p, quote("600001", pct=-3.0, open_pct=-0.5, ts=d1), d1).reason == "止损"
    assert decide_lowbuy_sell(p, quote("600001", pct=1.0, open_pct=0.2, ts=d1), d1).reason == "摸到+1%"
    assert decide_lowbuy_sell(p, quote("600001", pct=-0.1, open_pct=-1.5, ts=d1), d1).reason == "回到0轴"
    assert decide_lowbuy_sell(p, quote("600001", pct=-1.0, open_pct=-2.0, low_pct=-2.6, ts=d1), d1).reason == "反弹"
    assert not decide_lowbuy_sell(p, quote("600001", pct=0.5, open_pct=0.2, ts=d1), d1).sell
    eod = datetime(2026, 10, 12, 14, 55)
    assert decide_lowbuy_sell(p, quote("600001", pct=0.5, open_pct=0.2, ts=eod), eod).reason == "尾盘清仓"
    assert decide_lowbuy_sell(p, quote("600001", pct=0.5, ts=d1), d1,
                              prev_trade_day=date(2026, 10, 10)).reason == "隔夜超期"


class Judge:
    def __init__(self, verdicts=None):
        self.verdicts, self.last_trace = verdicts or {}, None

    def check(self, code, *args):
        return self.verdicts.get(code, ("pass", "消息面中性"))


def test_lowbuy_strategy_buys_main_after_jev_and_shadow_without():
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    strat = LowbuyStrategy(conn, ledger, Judge({"600002": ("reject", "新增减持")}), Judge(),
                           {"600001": "甲", "600002": "乙"}, {"600001": 3.0, "600002": 8.0}, date(2026, 10, 9),
                           lambda: "正常", lambda: "中性")
    t = at(10, 0)
    qs = {c: quote(c, pct=-1.0, ts=t, amount=2e8) for c in ("600001", "600002")}
    fills = strat.on_minute(qs, t)
    assert {(f.account, f.code) for f in fills} == {("lowbuy_shadow", "600002"), ("lowbuy_main", "600001")}
    main = next(f for f in fills if f.account == "lowbuy_main")
    assert main.reason.startswith("低吸-1.0%｜5日3.0%")


def test_lowbuy_waits_for_tail_on_weak_days():
    conn = db.connect(":memory:")
    strat = LowbuyStrategy(conn, Ledger(conn), Judge(), Judge(), {"600001": "甲"}, {"600001": 3.0},
                           date(2026, 10, 9), lambda: "偏弱", lambda: "中性")
    assert strat.buy_step({"600001": quote("600001", pct=-1.0, ts=at(10, 0))}, at(10, 0)) == []
    assert len(strat.buy_step({"600001": quote("600001", pct=-1.0, ts=at(14, 50))}, at(14, 50))) == 2
