"""冰点修复（dj 2026-11-03，知识库「冰点、修复和拐点」；research/kb_events.py：连续第 2 个冰点日的尾盘买一篮子、持有 3 天，
21 个事件均 +2.34%、胜 81%、最差 −9.1%，一年 2~3 次）。冰点日 = 主板跌停 ≥10 家且炸板率 ≥40%（和回测同口径，用 14:50 的主板行情算）。"""
import json
from datetime import date, datetime

from factories import position, quote

from jevq import config, db
from jevq.ledger import Ledger
from jevq.rules.ice import decide_ice_sell, ice_basket, is_ice, market_state
from jevq.strategies.ice import IceStrategy

TODAY = date(2026, 11, 3)
T1450 = datetime(2026, 11, 3, 14, 50, 10)
ICE = config.STRATEGIES["ice"]["accounts"][0]
CAL = [date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4),
       date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)]


def _q(code, pct, ts=T1450, high_pct=None, price=None):
    q = quote(code, pct=pct, pre_close=10.0, ts=ts, name=f"股{code}")
    return q


def _panic_quotes(n_down=12, n_touch_broke=10, n_sealed=10, n_normal=60, ts=T1450):
    """跌停 n_down 家；摸过涨停没封住 n_touch_broke 家；封住 n_sealed 家；其余普通。"""
    qs, i = {}, 600000
    for _ in range(n_down):
        i += 1
        qs[str(i)] = _q(str(i), -10.0, ts)
    for _ in range(n_touch_broke):
        i += 1
        q = quote(str(i), pct=7.0, pre_close=10.0, ts=ts)
        qs[str(i)] = type(q)(**{**q.__dict__, "high": 11.0})                      # 最高摸到涨停价 11.0，收 10.7
    for _ in range(n_sealed):
        i += 1
        qs[str(i)] = _q(str(i), 10.0, ts)
    for _ in range(n_normal):
        i += 1
        qs[str(i)] = _q(str(i), 1.0, ts)
    return qs


def test_market_state_counts_limit_down_and_break_rate_on_main_board_quotes():
    st = market_state(_panic_quotes(), TODAY)
    assert (st["dt"], st["zt"], st["touch"]) == (12, 10, 20) and st["rate"] == 50.0
    assert is_ice(st)
    assert not is_ice(market_state(_panic_quotes(n_down=9), TODAY))               # 跌停不到 10 家
    assert not is_ice(market_state(_panic_quotes(n_touch_broke=5), TODAY))        # 炸板率 33%
    assert market_state({}, TODAY) is None and not is_ice(None)


def test_basket_is_deterministic_main_board_priced_tradable_and_capped():
    qs = _panic_quotes()
    qs["300001"] = _q("300001", 1.0)                                              # 创业板
    qs["600999"] = quote("600999", pct=1.0, pre_close=2.0, ts=T1450)              # 股价 <3
    qs["600998"] = quote("600998", pct=1.0, pre_close=10.0, ts=datetime(2026, 11, 2, 15))   # 旧行情
    a, b = ice_basket(qs, TODAY, 20), ice_basket(dict(reversed(list(qs.items()))), TODAY, 20)
    assert [q.code for q in a] == [q.code for q in b] and len(a) == 20            # 同一天同一池 → 同一篮子
    codes = {q.code for q in a}
    assert not codes & {"300001", "600999", "600998"}
    assert all(not q.pct <= -10.0 and not q.pct >= 10.0 for q in a)               # 涨跌停的不买


def test_sell_on_the_third_trading_day_after_buy_near_the_close_or_when_overdue():
    pos = position("600001", account=ICE, buy_date=date(2026, 11, 3))
    q = quote("600001", pct=1.0, ts=datetime(2026, 11, 6, 14, 56))
    assert not decide_ice_sell(pos, q, datetime(2026, 11, 5, 14, 56), CAL).sell     # 第 2 个交易日
    assert not decide_ice_sell(pos, q, datetime(2026, 11, 6, 14, 50), CAL).sell     # 第 3 个交易日但没到 14:55
    assert decide_ice_sell(pos, q, datetime(2026, 11, 6, 14, 55), CAL).sell
    assert decide_ice_sell(pos, q, datetime(2026, 11, 9, 9, 31), CAL).sell          # 过期：下一天开盘就走


def _state_row(conn, day, dt, rate):
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, '', 'state', 'x', ?, 'ice')",
                 (f"{day} 14:50:00", json.dumps({"dt": dt, "zt": 5, "touch": 10, "rate": rate})))
    conn.commit()


def test_buys_the_basket_only_on_the_second_consecutive_ice_day_and_only_once():
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    s = IceStrategy(conn, ledger, CAL)
    qs = _panic_quotes()
    # 昨天不是冰点 → 今天只记状态，不买
    _state_row(conn, "2026-11-02", 3, 10.0)
    assert s.buy_step(qs, T1450) == []
    assert conn.execute("SELECT verdict FROM decisions WHERE strategy='ice' AND substr(ts,1,10)='2026-11-03'").fetchall() == [("state",)]
    # 昨天也是冰点 → 买一篮子
    conn2 = db.connect(":memory:")
    ledger2 = Ledger(conn2)
    s2 = IceStrategy(conn2, ledger2, CAL)
    _state_row(conn2, "2026-11-02", 15, 55.0)
    fills = s2.buy_step(qs, T1450)
    assert len(fills) == config.ICE_BASKET and {f.account for f in fills} == {ICE}
    assert all(f.shares * f.price <= config.ICE_PER_TRADE for f in fills)
    assert s2.buy_step(qs, datetime(2026, 11, 3, 14, 52)) == []                    # 当天不再买
    s3 = IceStrategy(conn2, ledger2, CAL)                                            # 重启后也不重复
    assert s3.buy_step(qs, datetime(2026, 11, 3, 14, 53)) == []


def test_no_buy_before_1450_without_yesterdays_state_or_with_stale_quotes():
    conn = db.connect(":memory:")
    s = IceStrategy(conn, Ledger(conn), CAL)
    _state_row(conn, "2026-11-02", 15, 55.0)
    assert s.buy_step(_panic_quotes(), datetime(2026, 11, 3, 14, 40)) == []           # 还没到尾盘
    conn2 = db.connect(":memory:")
    s2 = IceStrategy(conn2, Ledger(conn2), CAL)
    assert s2.buy_step(_panic_quotes(), T1450) == []                                   # 没有昨天的状态：不知道是不是连续，不买
    conn3 = db.connect(":memory:")
    _state_row(conn3, "2026-11-02", 15, 55.0)
    s3 = IceStrategy(conn3, Ledger(conn3), CAL, can_buy=False)
    assert s3.buy_step(_panic_quotes(), T1450) == []
