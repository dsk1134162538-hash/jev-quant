from datetime import date, datetime

import pytest

from factories import position, quote
from jevq.rules.sell import decide_sell

NEXT = date(2026, 9, 24)


def at(h, m):
    return datetime(2026, 9, 24, h, m)


def run(pct, open_pct, low_pct=None, when=at(9, 31), buy_date=date(2026, 9, 23)):
    return decide_sell(position(buy_date=buy_date), quote(pct=pct, open_pct=open_pct, low_pct=low_pct), when)


def test_t_plus_one_same_day_never_sells():
    assert not run(-5.0, -5.0, buy_date=NEXT).sell


def test_before_open_does_nothing():
    assert not run(2.0, 2.0, when=at(9, 25)).sell


@pytest.mark.parametrize("open_pct", [1.0, 2.5, 3.0, 4.5])
def test_high_open_sells_immediately(open_pct):
    d = run(open_pct, open_pct)
    assert d.sell and d.reason == "高开止盈"


def test_flat_open_holds_until_plus_one():
    assert not run(0.8, 0.5).sell
    d = run(1.0, 0.5)
    assert d.sell and d.reason == "摸到+1%"


def test_low_open_holds_then_sells_back_to_zero():
    assert not run(-1.0, -1.5, low_pct=-1.5).sell
    d = run(-0.2, -1.5, low_pct=-1.5)
    assert d.sell and d.reason == "回到0轴"


def test_low_open_sells_on_rebound():
    d = run(-1.3, -1.5, low_pct=-2.8)
    assert d.sell and d.reason == "反弹"


def test_low_open_minus_2_5_same_group():
    assert not run(-2.0, -2.5, low_pct=-2.5).sell


def test_open_exactly_minus_one_is_low_open_group():
    assert not run(-0.5, -1.0, low_pct=-1.0).sell
    assert run(-0.2, -1.0, low_pct=-1.0).sell


@pytest.mark.parametrize("pct,open_pct", [(-3.0, -1.5), (-3.5, 0.5), (-2.9, -3.2)])
def test_stop_loss(pct, open_pct):
    d = run(pct, open_pct, low_pct=min(pct, open_pct))
    assert d.sell and d.reason == "止损"


def test_eod_liquidation():
    d = run(-0.8, -1.5, low_pct=-1.5, when=at(14, 55))
    assert d.sell and d.reason == "尾盘清仓"
