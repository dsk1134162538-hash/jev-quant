"""超跌反弹：连板排除、排序、买入时机、策略运行时。"""
import json
from datetime import date, datetime

from factories import position, quote

from jevq import config, db
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


def test_rank_rebound_needs_min_shrink_days_and_puts_four_day_shrink_first():
    qs = [quote(c, pct=-1.0) for c in ("600001", "600002", "600003", "600004", "600005")]
    ret5 = {"600001": -5.0, "600002": -18.0, "600003": -8.0, "600004": -12.0, "600005": -9.0}
    days = {"600001": 4, "600002": 3, "600003": 5, "600004": 2}          # 600005 量能数据不全（None）
    ranked = rank_rebound(qs, ret5, set(), shrink_days=lambda q: days.get(q.code))
    # 600004 只缩 2 天、600005 不知道：不入选；≥4 天的（600003、600001）先于 3 天的 600002，
    # 哪怕 600002 的 5 日跌得更多；同档内 5 日涨幅从低到高
    assert [q.code for q in ranked] == ["600003", "600001", "600002"]


def test_rank_rebound_without_shrink_data_source_keeps_old_behaviour():
    qs = [quote("600001", pct=-1.0), quote("600002", pct=-1.0)]
    ranked = rank_rebound(qs, {"600001": -5.0, "600002": -18.0}, set())
    assert [q.code for q in ranked] == ["600002", "600001"]


def test_rank_rebound_min_shrink_days_zero_turns_the_volume_rule_off(monkeypatch):
    monkeypatch.setattr(config, "REBOUND_MIN_SHRINK_DAYS", 0)
    qs = [quote("600001", pct=-1.0), quote("600002", pct=-1.0)]
    ranked = rank_rebound(qs, {"600001": -5.0, "600002": -18.0}, set(), shrink_days=lambda q: None)
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


def _feat(*vols):
    """成交量历史（手，升序，最后一项是昨天）；行情日 2026-09-24，今天的量按已开盘分钟折算。"""
    return {"vols": list(vols), "vols_until": PREV_DAY.isoformat()}


# 测试行情每只成交 10 手：10:00 折算 10 / 30 × 240 = 80 手；14:50 折算 10 / 230 × 240 ≈ 10.4 手
SHRINK2 = _feat(100.0, 300.0, 200.0)                  # 80 < 200 < 300，再往前放量：连续缩量 2 天
SHRINK3 = _feat(250.0, 300.0, 200.0, 150.0)           # 3 天
SHRINK4 = _feat(100.0, 400.0, 300.0, 200.0, 150.0)    # 4 天
ALL_SHRINK4 = {c: SHRINK4 for c in UNIVERSE}


def make(ret5=None, excluded=(), sentiment="正常", notice=None, enabled=True, features=ALL_SHRINK4):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    state = {"sentiment": sentiment}
    strat = ReboundStrategy(conn, ledger, notice or Judge(), Judge(), UNIVERSE,
                            ret5 if ret5 is not None else {"600001": -5.0, "600002": -15.0, "600003": -8.0},
                            set(excluded), PREV_DAY, lambda: state["sentiment"], enabled=enabled,
                            features=features)
    return strat, conn, ledger, state


def _quotes(t, **pcts):
    return {c: quote(c, pct=p, amount=2e8, ts=t, volume=10) for c, p in pcts.items()}


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
    assert [(f.side, f.reason) for f in fills] == [("sell", "硬止损")]   # 主账户 -3.5% 不等确认


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


def _flow_q(t, price, high, outer, inner):
    q = quote("600002", pct=round((price / 10 - 1) * 100, 2), ts=t)
    return type(q)(**{**q.__dict__, "price": price, "high": high, "outer_vol": outer, "inner_vol": inner})


def test_trailing_waits_while_recent_buying_dominates_then_sells():
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    _hold(conn)
    strat = ReboundStrategy(conn, ledger, Judge(), Judge(), UNIVERSE, {}, set(), PREV_DAY, lambda: "正常",
                            enabled=False, trade_days=[PREV_DAY, TODAY])
    # 10:00–10:02 冲到 10.25，外盘增量一直大于内盘
    for m, (out, inn) in enumerate([(100, 100), (160, 120), (230, 140)]):
        assert strat.on_minute({"600002": _flow_q(at(10, m), 10.25, 10.25, out, inn)}, at(10, m)) == []
    # 10:03 回落到 +1.7%（回落 0.8 点），但最近 3 分钟外盘 +150 > 内盘 +60 → 留着
    assert strat.on_minute({"600002": _flow_q(at(10, 3), 10.17, 10.25, 250, 160)}, at(10, 3)) == []
    # 10:04 最近 3 分钟外盘 +50 < 内盘 +150 → 跟踪止盈卖
    fills = strat.on_minute({"600002": _flow_q(at(10, 4), 10.17, 10.25, 210 + 0, 270)}, at(10, 4))
    assert [(f.side, f.reason) for f in fills] == [("sell", "跟踪止盈")]


def test_buy_pressure_needs_two_snapshots():
    """刚开盘/刚重启只有一个快照：没有主买优势，照常卖。"""
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    _hold(conn)
    strat = ReboundStrategy(conn, ledger, Judge(), Judge(), UNIVERSE, {}, set(), PREV_DAY, lambda: "正常",
                            enabled=False, trade_days=[PREV_DAY, TODAY])
    fills = strat.on_minute({"600002": _flow_q(at(10, 30), 10.17, 10.25, 9999, 1)}, at(10, 30))
    assert [(f.side, f.reason) for f in fills] == [("sell", "跟踪止盈")]


# ---- 成交量：至少连续缩量 3 天（含今天），4 天的优先 ----

def _bought(fills):
    return {(f.account, f.code) for f in fills if f.side == "buy"}


def test_buy_needs_three_day_shrink_and_four_day_shrink_goes_first():
    feats = {"600001": SHRINK2, "600002": SHRINK3, "600003": SHRINK4}
    strat, conn, ledger, _ = make(features=feats)
    q = {"600001": -1.0, "600002": -1.0, "600003": -1.0}
    fills = strat.on_minute(_quotes(at(10, 0), **q), at(10, 0))
    # 600001 只缩 2 天不入选；600002 的 5 日跌得比 600003 多，但 600003 缩了 4 天，先买它（只有一个仓位）
    assert _bought(fills) == {("rebound_main", "600003"), ("rebound_shadow", "600003")}
    trace = json.loads(conn.execute("SELECT trace FROM decisions WHERE strategy='rebound'").fetchone()[0])
    assert trace["candidate"]["shrink_days"] == 4


def test_three_day_shrink_is_enough_when_no_four_day_candidate():
    strat, *_ = make(features={"600002": SHRINK3})
    fills = strat.on_minute(_quotes(at(10, 0), **{"600002": -1.0}), at(10, 0))
    assert _bought(fills) == {("rebound_main", "600002"), ("rebound_shadow", "600002")}


def test_no_buy_when_volume_history_is_missing_or_today_not_projectable():
    strat, *_ = make(features={})                                            # 库里没有量能历史
    assert strat.on_minute(_quotes(at(10, 0), **{"600002": -1.0}), at(10, 0)) == []
    strat, *_ = make(features={"600002": SHRINK4})
    assert strat.on_minute(_quotes(at(9, 40), **{"600002": -1.0}), at(9, 40)) == []   # 09:45 前不折算全天量


def test_weak_market_tail_buy_uses_almost_full_day_volume():
    strat, *_ = make(sentiment="偏弱", features={"600002": SHRINK4})
    fills = strat.on_minute(_quotes(at(14, 50), **{"600002": -1.0}), at(14, 50))
    assert _bought(fills) == {("rebound_main", "600002"), ("rebound_shadow", "600002")}
    # 尾盘量已经基本走完：成交 200 手折算 ≈ 209 手 ≥ 昨天 150 手，不算缩量，不买
    strat, *_ = make(sentiment="偏弱", features={"600002": SHRINK4})
    heavy = {"600002": quote("600002", pct=-1.0, amount=2e8, ts=at(14, 50), volume=200)}
    assert strat.on_minute(heavy, at(14, 50)) == []


# ---- 卖出后再买：当天卖出的票要比卖出价低 1% 才允许再买 ----

def _hold_both(conn, code="600002", buy_price=10.0, buy_date=PREV_DAY):
    for account in ("rebound_main", "rebound_shadow"):
        conn.execute("INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?)",
                     (account, code, 1000, buy_price * 1000 + 5, buy_price, buy_date.isoformat()))
    conn.commit()


def _bounce_q(t, price_pct):
    """开盘 -1.8%、最低 -1.9%，现价 price_pct：现价 -1.0% 时从最低拉起 0.9 个点 → 低开反抽卖出。"""
    return {"600002": quote("600002", pct=price_pct, open_pct=-1.8, low_pct=-1.9, amount=2e8, ts=t, volume=10)}


def test_sold_stock_is_not_bought_back_until_one_percent_below_sell_price():
    strat, conn, ledger, _ = make()
    _hold_both(conn)
    fills = strat.on_minute(_bounce_q(at(10, 0), -1.0), at(10, 0))            # 9.90 卖出
    assert [(f.account, f.side, f.reason, f.price) for f in fills] == [
        ("rebound_main", "sell", "低开反抽", 9.9), ("rebound_shadow", "sell", "低开反抽", 9.9)]
    # 同一分钟没有原价买回；之后价格没比卖出价低 1% 以上（9.90 → 9.801）之前也不买
    assert strat.on_minute(_bounce_q(at(10, 1), -1.0), at(10, 1)) == []
    assert strat.on_minute(_bounce_q(at(10, 2), -1.5), at(10, 2)) == []       # 9.85
    # 9.80 ≤ 9.90 × 0.99 四舍五入到分 = 9.80：可以再买
    fills = strat.on_minute(_bounce_q(at(10, 3), -2.0), at(10, 3))
    assert _bought(fills) == {("rebound_main", "600002"), ("rebound_shadow", "600002")}
    assert {f.price for f in fills} == {9.8}


def test_rebuy_block_survives_a_restart_and_is_per_account():
    strat, conn, ledger, _ = make()
    _hold_both(conn)
    strat.on_minute(_bounce_q(at(10, 0), -1.0), at(10, 0))
    strat2 = ReboundStrategy(conn, ledger, Judge(), Judge(), UNIVERSE, {"600002": -15.0}, set(), PREV_DAY,
                             lambda: "正常", features=ALL_SHRINK4)             # 重启后从成交记录里恢复
    assert strat2.on_minute(_bounce_q(at(10, 1), -1.0), at(10, 1)) == []
    # 影子账户当天没卖过这只票（比如主账户卖的是别的仓位）：不受主账户的卖出记录影响
    conn.execute("DELETE FROM fills WHERE account='rebound_shadow'")
    conn.commit()
    fills = strat2.on_minute(_bounce_q(at(10, 2), -1.0), at(10, 2))
    assert _bought(fills) == {("rebound_shadow", "600002")}


def test_stock_sold_on_an_earlier_day_can_be_bought_again():
    strat, conn, ledger, _ = make()
    yesterday = datetime(2026, 9, 23, 10, 0)
    for account in ("rebound_main", "rebound_shadow"):
        ledger.buy(account, quote("600002", pct=-1.0, ts=yesterday), yesterday, "x")
        pos = ledger.positions(account)[0]
        ledger.sell(account, pos, quote("600002", pct=-1.0, ts=yesterday), yesterday, "止损")
    fills = strat.on_minute(_quotes(at(10, 0), **{"600002": -1.0}), at(10, 0))
    assert _bought(fills) == {("rebound_main", "600002"), ("rebound_shadow", "600002")}


# ---- 主账户止损：连续 2 分钟确认 + 问 Jev 走向；影子账户照旧（对照组）----

class FakeTrend:
    def __init__(self, label):
        self.label, self.calls, self.last_trace = label, [], None

    def judge(self, pos, q, now):
        self.calls.append((pos.account, pos.code, now))
        self.last_trace = {"inputs": {"现价": q.price}, "label": self.label}
        return self.label


def _stop_strat(label):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    _hold_both(conn)
    trend = FakeTrend(label)
    strat = ReboundStrategy(conn, ledger, Judge(), Judge(), UNIVERSE, {}, set(), PREV_DAY, lambda: "正常",
                            enabled=False, trade_days=[PREV_DAY, TODAY], trend_judge=trend)
    return strat, conn, trend


def _sells(fills):
    return [(f.account, f.reason) for f in fills if f.side == "sell"]


def _q(t, pct):
    return {"600002": quote("600002", pct=pct, amount=2e8, ts=t, volume=10)}


def test_main_stop_needs_two_minutes_shadow_sells_at_once():
    strat, conn, trend = _stop_strat("单边下跌")
    assert _sells(strat.on_minute(_q(at(10, 14), -2.1), at(10, 14))) == [("rebound_shadow", "止损")]
    assert trend.calls == []
    assert _sells(strat.on_minute(_q(at(10, 15), -2.1), at(10, 15))) == [("rebound_main", "止损（单边下跌）")]
    assert [c[0] for c in trend.calls] == ["rebound_main"]
    rows = conn.execute("SELECT code, verdict, detail, strategy FROM decisions WHERE verdict = 'trend'").fetchall()
    assert rows == [("600002", "trend", "止损（单边下跌）", "rebound")]


def test_minute_back_above_line_resets_the_count():
    """10-09 雪迪龙那种：10:14 瞬间跌破，10:15 回到线上，10:16 再跌破 → 还是第 1 分钟，不卖。"""
    strat, conn, trend = _stop_strat("单边下跌")
    strat.on_minute(_q(at(10, 14), -2.1), at(10, 14))
    strat.on_minute(_q(at(10, 15), -1.9), at(10, 15))
    assert _sells(strat.on_minute(_q(at(10, 16), -2.1), at(10, 16))) == []
    assert trend.calls == []


def test_same_minute_twice_does_not_count_as_two():
    strat, conn, trend = _stop_strat("单边下跌")
    strat.sell_step(_q(at(10, 14), -2.1), at(10, 14))
    assert _sells(strat.sell_step(_q(at(10, 14), -2.1), at(10, 14))) == []


def test_main_holds_when_jev_sees_rebound_and_logs_why():
    strat, conn, trend = _stop_strat("探底回升")
    strat.on_minute(_q(at(10, 14), -2.1), at(10, 14))
    assert _sells(strat.on_minute(_q(at(10, 15), -2.1), at(10, 15))) == []
    assert conn.execute("SELECT detail FROM decisions WHERE verdict = 'trend'").fetchone() == ("止损暂缓（探底回升）",)
    # 再跌到 -3.5% 硬止损：不问 Jev 直接卖
    n = len(trend.calls)
    assert _sells(strat.on_minute(_q(at(10, 16), -3.5), at(10, 16))) == [("rebound_main", "硬止损")]
    assert len(trend.calls) == n


def test_main_without_trend_judge_still_confirms_then_sells():
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    _hold(conn)
    strat = ReboundStrategy(conn, ledger, Judge(), Judge(), UNIVERSE, {}, set(), PREV_DAY, lambda: "正常",
                            enabled=False, trade_days=[PREV_DAY, TODAY])
    assert _sells(strat.on_minute(_q(at(10, 14), -2.1), at(10, 14))) == []
    assert _sells(strat.on_minute(_q(at(10, 15), -2.1), at(10, 15))) == [("rebound_main", "止损")]
