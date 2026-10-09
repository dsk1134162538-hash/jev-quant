"""扫首板卖出（买入次日起，相对昨收）。2026-10-09 按 162 只首板的次日数据改（用户定）：
开盘 -3%~+3% 竞价不及预期直接卖；大低开（≤-3%）不在开盘止损，等反抽 1.5 点或 10:00 卖，-6% 硬止损；
高开 >3% 按原规则（封板拿、炸板卖、冲高回落 2 点卖、-3% 止损、14:55 清仓）。"""
from dataclasses import replace
from datetime import date, datetime

from factories import position, quote
from jevq.rules.board_sell import decide_board_sell

BUY = date(2026, 9, 23)


def at(h, m):
    return datetime(2026, 9, 24, h, m)


def qq(pct, open_pct=5.0, high_pct=None, ts=datetime(2026, 9, 24, 10, 0)):
    x = quote(pct=pct, open_pct=open_pct, ts=ts)
    if high_pct is not None:
        x = replace(x, high=round(x.pre_close * (1 + high_pct / 100), 2))
    return x


def d(q_, now=at(10, 0), buy=BUY, prev=None):
    return decide_board_sell(position(buy_date=buy), q_, now, prev)


def test_same_day_and_preopen_hold():
    assert not d(qq(-5), buy=date(2026, 9, 24)).sell
    assert not d(qq(-5), now=at(9, 25)).sell


def test_weak_open_between_minus3_and_plus3_sells_at_once():
    for op in (-2.9, 0.0, 3.0):
        r = d(qq(op, open_pct=op), now=at(9, 30))
        assert r.sell and r.reason == "竞价不及预期"


def test_weak_open_that_seals_limit_up_is_kept():
    assert not d(qq(10.0, open_pct=2.0, high_pct=10.0), now=at(9, 31)).sell


def test_big_low_open_waits_for_a_bounce_instead_of_stopping_at_the_open():
    assert not d(qq(-4.0, open_pct=-4.0), now=at(9, 30)).sell              # 原来 -3% 会在开盘止损
    r = d(qq(-2.5, open_pct=-4.0), now=at(9, 40))
    assert r.sell and r.reason == "低开反抽"                                 # 从开盘拉起 1.5 点
    r = d(qq(-3.5, open_pct=-4.0), now=at(10, 0))
    assert r.sell and r.reason == "低开到点"
    r = d(qq(-6.0, open_pct=-4.0), now=at(9, 35))
    assert r.sell and r.reason == "止损"                                     # -6% 硬止损


def test_high_open_uses_the_original_rules():
    r = d(qq(-3.0, open_pct=4.0, high_pct=4.0))
    assert r.sell and r.reason == "止损"
    r = d(qq(8.0, open_pct=5.0, high_pct=10.0))
    assert r.sell and r.reason == "炸板"
    assert not d(qq(4.5, open_pct=5.0, high_pct=6.0)).sell
    r = d(qq(4.0, open_pct=5.0, high_pct=6.0))
    assert r.sell and r.reason == "冲高回落"
    r = d(qq(4.5, open_pct=4.0, high_pct=5.0), now=at(14, 55))
    assert r.sell and r.reason == "尾盘清仓"


def test_sealed_limit_up_holds_even_at_eod():
    assert not d(qq(10.0, open_pct=6.0, high_pct=10.0), now=at(14, 56)).sell


def test_stale_position_sells_unless_sealed():
    r = d(qq(5.0, open_pct=5.0, high_pct=5.0), buy=date(2026, 9, 21), prev=date(2026, 9, 23))
    assert r.sell and r.reason == "隔夜超期"
    assert not d(qq(10.0, high_pct=10.0), buy=date(2026, 9, 21), prev=date(2026, 9, 23)).sell


def test_sealed_overnight_position_uses_normal_rules_on_t_plus_2():
    # T=09-22 买入，T+1=09-23 封死涨停隔夜持有；T+2=09-24 开盘 +4%：按正常规则判断（高开 >3% 那一组），继续持有。
    q_ = quote(pct=4.0, open_pct=4.0, ts=datetime(2026, 9, 24, 9, 31))
    assert not decide_board_sell(position(buy_date=date(2026, 9, 22)), q_, datetime(2026, 9, 24, 9, 31), None).sell
