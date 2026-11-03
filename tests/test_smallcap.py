"""小市值轮动（dj 2026-11-03「1.可以」；research/size_study.py：成交额最小 10%、每 20 个交易日调仓、选 20 只、扣 0.3% 滑点，
年化 +12%、最大回撤 −34%，逐年极不均匀，未校正幸存者偏差）。对照基线，不是主力。"""
from datetime import date, datetime, timedelta

from factories import position, quote

from jevq import config, db
from jevq.ledger import Ledger
from jevq.rules.smallcap import amount60_from_db, decide_sc_sell, pick_smallcap
from jevq.strategies.smallcap import SmallcapStrategy

ACCOUNT = config.STRATEGIES["smallcap"]["accounts"][0]
T1450 = datetime(2026, 11, 3, 14, 50, 10)
TODAY = date(2026, 11, 3)


def _days(n=60, end=date(2026, 12, 31)):
    out, d = [], date(2026, 9, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_amount60_is_the_average_turnover_and_needs_enough_history():
    conn = db.connect(":memory:")
    days = _days(50)
    rows = []
    for d in days:
        rows.append((d.isoformat(), "600001", 10.0, 1000))             # 成交额 10×1000×100 = 100 万
        rows.append((d.isoformat(), "600002", 20.0, 2000))             # 400 万
    rows += [(d.isoformat(), "600003", 10.0, 1000) for d in days[-10:]]    # 只有 10 天历史
    conn.executemany("INSERT INTO closes(date, code, close, volume) VALUES (?, ?, ?, ?)", rows)
    got = amount60_from_db(conn, ["600001", "600002", "600003"], date(2026, 12, 31))
    assert got == {"600001": 1_000_000, "600002": 4_000_000}


def _q(code, price=10.0, pct=1.0, ts=T1450):
    return quote(code, pct=pct, pre_close=round(price / (1 + pct / 100), 2), ts=ts)


def test_pick_takes_from_the_smallest_tenth_and_skips_untradable():
    amounts = {f"6000{i:02d}": float(i + 1) * 1e6 for i in range(100)}          # 600000 最小 … 600099 最大
    quotes = {c: _q(c) for c in amounts}
    quotes["600001"] = _q("600001", price=2.5, pct=1.0)                        # 股价 <3
    quotes["600002"] = quote("600002", pct=10.0, pre_close=10.0, ts=T1450)     # 涨停
    quotes["600003"] = _q("600003", ts=datetime(2026, 11, 2, 15))              # 旧行情
    got = pick_smallcap(quotes, amounts, TODAY, 5)
    codes = {q.code for q in got}
    assert len(got) == 5 and codes <= {f"6000{i:02d}" for i in range(10)}      # 最小 10% = 前 10 只
    assert not codes & {"600001", "600002", "600003"}
    assert [q.code for q in got] == [q.code for q in pick_smallcap(dict(reversed(list(quotes.items()))), amounts, TODAY, 5)]


def test_sell_after_twenty_trading_days_near_the_close_or_when_overdue():
    days = _days()
    buy = days[0]
    pos = position("600001", account=ACCOUNT, buy_date=buy)
    q = quote("600001", pct=1.0)
    due = days[config.SC_HOLD_DAYS]
    at = lambda d, h, m: datetime.combine(d, datetime.min.time()).replace(hour=h, minute=m)   # noqa: E731
    assert not decide_sc_sell(pos, q, at(days[config.SC_HOLD_DAYS - 1], 14, 55), days).sell
    assert not decide_sc_sell(pos, q, at(due, 14, 40), days).sell
    assert decide_sc_sell(pos, q, at(due, 14, 50), days).sell
    assert decide_sc_sell(pos, q, at(days[config.SC_HOLD_DAYS + 1], 9, 31), days).sell


def _strategy(conn, amounts, days, can_buy=True):
    ledger = Ledger(conn)
    return SmallcapStrategy(conn, ledger, amounts, trade_days=days, can_buy=can_buy), ledger


def test_buys_a_basket_when_empty_once_a_day_and_rotates_after_the_hold_period():
    conn = db.connect(":memory:")
    days = _days()
    amounts = {f"6000{i:02d}": float(i + 1) * 1e6 for i in range(100)}
    s, ledger = _strategy(conn, amounts, days)
    quotes = {c: _q(c) for c in amounts}
    t0 = datetime.combine(days[30], datetime.min.time()).replace(hour=14, minute=50, second=10)
    quotes = {c: _q(c, ts=t0) for c in amounts}
    fills = s.buy_step(quotes, t0)
    assert len(fills) == 10                                                   # 最小 10% 只有 10 只：篮子不会超过池子
    assert {f.account for f in fills} == {ACCOUNT} and all(f.shares * f.price <= config.SC_PER_TRADE for f in fills)
    assert s.buy_step(quotes, t0 + timedelta(minutes=1)) == []                # 当天不再买
    assert IceLike(s).restart_safe(quotes, t0 + timedelta(minutes=2)) == []   # 重启也不重复
    # 持满 20 个交易日：先卖，空仓后同一分钟补买新一批
    t1 = datetime.combine(days[30 + config.SC_HOLD_DAYS], datetime.min.time()).replace(hour=14, minute=50, second=10)
    q1 = {c: _q(c, ts=t1) for c in amounts}
    sold = s.sell_step(q1, t1)
    assert len(sold) == len(fills) and all(f.side == "sell" for f in sold)
    assert len(s.buy_step(q1, t1)) == len(fills)


def test_does_not_buy_while_holding_or_before_1450_or_when_buying_is_off():
    conn = db.connect(":memory:")
    days = _days()
    amounts = {f"6000{i:02d}": float(i + 1) * 1e6 for i in range(100)}
    t = datetime.combine(days[30], datetime.min.time()).replace(hour=14, minute=40)
    quotes = {c: _q(c, ts=t) for c in amounts}
    s, ledger = _strategy(conn, amounts, days)
    assert s.buy_step(quotes, t) == []                                          # 还没到 14:50
    ledger.buy(ACCOUNT, quotes["600050"], t, "x", budget=5000)                  # 手里还有一只没卖掉的
    assert s.buy_step({c: _q(c, ts=t.replace(hour=14, minute=50)) for c in amounts}, t.replace(hour=14, minute=50)) == []
    s2, _ = _strategy(db.connect(":memory:"), amounts, days, can_buy=False)
    assert s2.buy_step({c: _q(c, ts=t.replace(hour=14, minute=50)) for c in amounts}, t.replace(hour=14, minute=50)) == []


class IceLike:
    """用同一个库重新构造一个策略实例，模拟 daemon 重启。"""
    def __init__(self, s):
        self.s = s

    def restart_safe(self, quotes, now):
        again = SmallcapStrategy(self.s.conn, self.s.ledger, self.s.amount60, trade_days=self.s.calendar)
        return again.buy_step(quotes, now)
