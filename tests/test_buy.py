from datetime import time

import pytest

from factories import quote
from jevq.rules.buy import buy_mode, buy_window_open, in_buy_zone, rank_candidates


@pytest.mark.parametrize("pct,ok", [(0.0, True), (-1.5, True), (-3.0, True), (0.1, False), (-3.1, False)])
def test_zone_bounds(pct, ok):
    assert in_buy_zone(quote(pct=pct)) is ok


def test_zone_excludes_st_and_suspended():
    assert not in_buy_zone(quote(pct=-1, name="ST测试"))
    assert not in_buy_zone(quote(pct=-1, volume=0))


@pytest.mark.parametrize("sentiment,overseas,mode", [
    ("正常", "中性", "intraday"),
    ("火热", "偏强", "intraday"),
    ("偏弱", "偏强", "tail"),
    ("冰点", "中性", "tail"),
    ("火热", "偏弱", "tail"),
    (None, "中性", "tail"),
    ("正常", None, "tail"),
])
def test_buy_mode(sentiment, overseas, mode):
    assert buy_mode(sentiment, overseas) == mode


def test_windows():
    assert buy_window_open("intraday", time(9, 30))
    assert buy_window_open("intraday", time(14, 56))
    assert not buy_window_open("intraday", time(14, 57))
    assert not buy_window_open("tail", time(10, 0))
    assert buy_window_open("tail", time(14, 50))
    assert not buy_window_open("tail", time(14, 57))


def test_rank_by_5d_return_desc_and_filters():
    qs = [quote("600001", pct=-1), quote("600002", pct=-2), quote("600003", pct=1),
          quote("600004", pct=-0.5)]
    ret5 = {"600001": 3.0, "600002": 12.0, "600003": 20.0}
    ranked = [q.code for q in rank_candidates(qs, ret5)]
    assert ranked == ["600002", "600001"]   # 600003 不在区间；600004 无 5 日数据
