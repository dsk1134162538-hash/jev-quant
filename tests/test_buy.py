from datetime import time

import pytest

from factories import quote
from jevq.rules.buy import buy_window_open, in_buy_zone


@pytest.mark.parametrize("pct,ok", [(0.0, True), (-1.5, True), (-3.0, True), (0.1, False), (-3.1, False)])
def test_zone_bounds(pct, ok):
    assert in_buy_zone(quote(pct=pct)) is ok


def test_zone_excludes_st_and_suspended():
    assert not in_buy_zone(quote(pct=-1, name="ST测试"))
    assert not in_buy_zone(quote(pct=-1, volume=0))


def test_windows():
    assert buy_window_open("intraday", time(9, 30))
    assert buy_window_open("intraday", time(14, 56))
    assert not buy_window_open("intraday", time(14, 57))
    # 用户 2026-10-09：情绪偏弱/冰点/未知不买，不再尾盘买
    assert not buy_window_open("none", time(10, 0))
    assert not buy_window_open("none", time(14, 50))
