"""低吸（9-23 版规则 10-09 重新上线并细化，用户同意）：+5%~+20% 的强势股、缩量回调、属于主线；跟踪止盈。"""
from datetime import date, datetime

from factories import position, quote

from jevq import db
from jevq.ledger import Ledger
from jevq.rules.board import mainline_concepts
from jevq.rules.lowbuy import decide_lowbuy_sell, lowbuy_mode, rank_lowbuy
from jevq.strategies.lowbuy import LowbuyStrategy

DAY = datetime(2026, 10, 12)
D1 = datetime(2026, 10, 12, 9, 31)
BUY = date(2026, 10, 9)


def at(h, m):
    return DAY.replace(hour=h, minute=m)


def test_mode_waits_for_tail_on_weak_days_or_weak_overseas():
    assert lowbuy_mode("正常", "中性") == "intraday"
    assert lowbuy_mode("偏弱", "中性") == "tail" and lowbuy_mode("正常", "偏弱") == "tail"
    assert lowbuy_mode(None, "中性") == "tail"


def test_rank_keeps_strong_pullbacks_that_shrink_and_belong_to_the_mainline():
    qs = [quote(c, pct=-1.0) for c in ("600001", "600002", "600003", "600004", "600005")]
    ret5 = {"600001": 8.0, "600002": 15.0, "600003": 3.0, "600004": 25.0, "600005": 12.0}
    assert [q.code for q in rank_lowbuy(qs, ret5)] == ["600002", "600005", "600001"]   # 5~20% 内、涨得多的优先
    shrink = {"600001": 1, "600002": 0, "600005": None}
    assert [q.code for q in rank_lowbuy(qs, ret5, shrink=lambda q: shrink.get(q.code))] == ["600001"]
    assert [q.code for q in rank_lowbuy(qs, ret5, mainline=lambda c: c != "600002")] == ["600005", "600001"]
    assert rank_lowbuy([quote("600001", pct=-3.5)], ret5) == []                        # 不在 0~-3%


def test_mainline_concepts_need_five_strong_or_an_article_mention():
    t = datetime(2026, 10, 12, 10, 0)
    qs = {f"6000{i:02d}": quote(f"6000{i:02d}", pct=6.0, ts=t) for i in range(1, 9)}
    cmap = {c: (["机器人"] if i < 5 else ["电力"]) for i, c in enumerate(sorted(qs))}   # 机器人 5 只、电力 3 只
    assert mainline_concepts(qs, cmap, t.date()) == {"机器人"}
    assert mainline_concepts(qs, cmap, t.date(), article_hot=lambda c, m: "点名" if c == "电力" else "") == {
        "机器人", "电力"}


def _pos(buy_price=10.0):
    return position("600001", account="lowbuy_main", buy_price=buy_price, buy_date=BUY)


def test_sell_stop_and_low_open_rules_unchanged():
    assert decide_lowbuy_sell(_pos(), quote("600001", pct=-3.0, open_pct=-0.5, ts=D1), D1).reason == "止损"
    assert decide_lowbuy_sell(_pos(), quote("600001", pct=-0.1, open_pct=-1.5, ts=D1), D1).reason == "回到0轴"
    assert decide_lowbuy_sell(_pos(), quote("600001", pct=-1.0, open_pct=-2.0, low_pct=-2.6, ts=D1), D1).reason == "反弹"
    eod = datetime(2026, 10, 12, 14, 55)
    assert decide_lowbuy_sell(_pos(), quote("600001", pct=0.5, open_pct=0.2, ts=eod), eod).reason == "尾盘清仓"
    assert decide_lowbuy_sell(_pos(), quote("600001", pct=0.5, ts=D1), D1,
                              prev_trade_day=date(2026, 10, 10)).reason == "隔夜超期"


def test_trailing_take_profit_replaces_selling_at_plus_one():
    """原来高开 +1.5% 开盘就卖、平开摸到 +1% 就卖；现在冲高后回落 0.8 个点才卖（相对买入价 10.0）。"""
    assert not decide_lowbuy_sell(_pos(), quote("600001", pct=1.5, open_pct=1.5, ts=D1), D1).sell
    q = quote("600001", pct=2.5, open_pct=1.0, ts=D1)
    q = type(q)(**{**q.__dict__, "high": 10.33})                     # 最高 +3.3%，现价 +2.5%：回落 0.8
    assert decide_lowbuy_sell(_pos(), q, D1).reason == "跟踪止盈"
    q = type(q)(**{**q.__dict__, "price": 10.0})
    assert decide_lowbuy_sell(_pos(), q, D1).reason == "回到成本"


class Judge:
    def __init__(self, verdicts=None):
        self.verdicts, self.last_trace = verdicts or {}, None

    def check(self, code, *args):
        return self.verdicts.get(code, ("pass", "消息面中性"))


def test_strategy_buys_main_after_jev_and_shadow_without():
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    strat = LowbuyStrategy(conn, ledger, Judge({"600002": ("reject", "新增减持")}), Judge(),
                           {"600001": "甲", "600002": "乙"}, {"600001": 6.0, "600002": 8.0}, BUY,
                           lambda: "正常", lambda: "中性")
    t = at(10, 0)
    qs = {c: quote(c, pct=-1.0, ts=t, amount=2e8) for c in ("600001", "600002")}
    fills = strat.on_minute(qs, t)
    assert {(f.account, f.code) for f in fills} == {("lowbuy_shadow", "600002"), ("lowbuy_main", "600001")}
    main = next(f for f in fills if f.account == "lowbuy_main")
    assert main.reason.startswith("低吸-1.0%｜5日6.0%")


def test_strategy_waits_for_tail_on_weak_days():
    conn = db.connect(":memory:")
    strat = LowbuyStrategy(conn, Ledger(conn), Judge(), Judge(), {"600001": "甲"}, {"600001": 6.0}, BUY,
                           lambda: "偏弱", lambda: "中性")
    assert strat.buy_step({"600001": quote("600001", pct=-1.0, ts=at(10, 0))}, at(10, 0)) == []
    assert len(strat.buy_step({"600001": quote("600001", pct=-1.0, ts=at(14, 50))}, at(14, 50))) == 2


def test_strategy_only_buys_members_of_todays_mainline():
    conn = db.connect(":memory:")
    t = at(10, 30)
    hot = {f"60010{i}": quote(f"60010{i}", pct=6.0, ts=t) for i in range(5)}        # 机器人 5 只强势
    cand = {"600001": quote("600001", pct=-1.0, ts=t), "600002": quote("600002", pct=-1.0, ts=t)}
    cmap = {**{c: ["机器人"] for c in hot}, "600001": ["机器人"], "600002": ["电力"]}
    universe = {c: c for c in {**hot, **cand}}
    strat = LowbuyStrategy(conn, Ledger(conn), Judge(), Judge(), universe, {"600001": 6.0, "600002": 9.0}, BUY,
                           lambda: "正常", lambda: "中性", concept_map=cmap)
    fills = strat.buy_step({**hot, **cand}, t)
    assert {f.code for f in fills} == {"600001"}                                    # 600002 不在主线


def test_trailing_pullback_widened_for_main_shadow_keeps_point_eight():
    """10-14 用户定：低吸主账户回落 1.5 点才卖（众生药业 10-14 开盘 +1.4% 回落 0.8 被洗掉，之后 +8.9%）；影子照旧。"""
    q = quote("600001", pct=2.5, open_pct=1.0, ts=D1)
    q = type(q)(**{**q.__dict__, "high": 10.33})                       # 最高 +3.3%，现价 +2.5%：回落 0.8
    assert not decide_lowbuy_sell(_pos(), q, D1, pullback=1.5).sell
    assert decide_lowbuy_sell(_pos(), type(q)(**{**q.__dict__, "price": 10.18}), D1, pullback=1.5).reason == "跟踪止盈"
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    for acct in ("lowbuy_main", "lowbuy_shadow"):
        conn.execute("INSERT INTO positions VALUES (?, '600001', 1000, 10005, 10.0, ?)", (acct, BUY.isoformat()))
    conn.commit()
    strat = LowbuyStrategy(conn, ledger, Judge(), Judge(), {"600001": "甲"}, {}, BUY, lambda: "正常", lambda: "中性")
    fills = strat.sell_step({"600001": q}, D1)
    assert [(f.account, f.reason) for f in fills] == [("lowbuy_shadow", "跟踪止盈")]
