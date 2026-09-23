from datetime import date, datetime

from factories import position, quote
from jevq.rules.board_sell import decide_board_sell

BUY = date(2026, 9, 23)


def at(h, m):
    return datetime(2026, 9, 24, h, m)


def qq(pct, high_pct=None, **kw):
    x = quote(pct=pct, ts=datetime(2026, 9, 24, 10, 0), **kw)
    if high_pct is not None:
        from dataclasses import replace
        x = replace(x, high=round(x.pre_close * (1 + high_pct / 100), 2))
    return x


def d(q_, now=at(10, 0), buy=BUY, prev=None):
    return decide_board_sell(position(buy_date=buy), q_, now, prev)


def test_same_day_and_preopen_hold():
    assert not d(qq(-5), buy=date(2026, 9, 24)).sell
    assert not d(qq(-5), now=at(9, 25)).sell


def test_stop_loss():
    r = d(qq(-3.0, high_pct=1))
    assert r.sell and r.reason == "止损"


def test_sealed_limit_up_holds_even_at_eod():
    assert not d(qq(10.0, high_pct=10.0), now=at(14, 56)).sell


def test_broken_board_sells():
    r = d(qq(8.0, high_pct=10.0))
    assert r.sell and r.reason == "炸板"


def test_pullback_two_points():
    assert not d(qq(4.5, high_pct=6.0)).sell
    r = d(qq(4.0, high_pct=6.0))
    assert r.sell and r.reason == "冲高回落"


def test_eod_liquidation_when_not_sealed():
    r = d(qq(1.0, high_pct=1.5), now=at(14, 55))
    assert r.sell and r.reason == "尾盘清仓"


def test_stale_position_sells_unless_sealed():
    r = d(qq(2.0, high_pct=2.0), buy=date(2026, 9, 21), prev=date(2026, 9, 23))
    assert r.sell and r.reason == "隔夜超期"
    assert not d(qq(10.0, high_pct=10.0), buy=date(2026, 9, 21), prev=date(2026, 9, 23)).sell
