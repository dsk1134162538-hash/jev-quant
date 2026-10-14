from datetime import datetime

from jevq.models import Quote
from jevq.rules.metrics import avg_pct, body_pct, committee, outer_ratio, seal_amount, seal_ratio


def q(**kw):
    base = dict(code="600001", name="测试股份", price=11.0, pre_close=10.0, open=10.5,
                high=11.0, low=10.4, volume=100000, amount=1e8, limit_up=11.0, limit_down=9.0,
                ts=datetime(2026, 9, 24, 10, 0))
    base.update(kw)
    return Quote(**base)


def test_committee_computed_and_none_when_missing():
    quote = q(bid_vols=(500, 300), ask_vols=(200, 100))
    assert committee(quote) == round((800 - 300) / 1100 * 100, 2)
    assert committee(q(bid_vols=(), ask_vols=())) is None
    assert committee(q(bid_vols=(0,), ask_vols=(0,))) is None


def test_outer_ratio_computed_and_none_when_missing():
    quote = q(outer_vol=800, inner_vol=200)
    assert outer_ratio(quote) == 80.0
    assert outer_ratio(q(outer_vol=0, inner_vol=0)) is None


def test_avg_pct_computed_and_none_when_missing():
    quote = q(avg_price=10.5, pre_close=10.0)
    assert avg_pct(quote) == 5.0
    assert avg_pct(q(avg_price=0, pre_close=10.0)) is None
    assert avg_pct(q(avg_price=10.5, pre_close=0)) is None


def test_body_pct_computed_and_none_when_missing():
    quote = q(price=11.0, open=10.5, pre_close=10.0)
    assert body_pct(quote) == 5.0
    assert body_pct(q(pre_close=0)) is None


def test_seal_amount_only_when_sealed_at_limit_up():
    sealed = q(price=11.0, limit_up=11.0, bid_vols=(1000, 0, 0, 0, 0))
    assert seal_amount(sealed) == round(1000 * 100 * 11.0 / 1e8, 4)
    not_sealed = q(price=10.9, limit_up=11.0, bid_vols=(1000, 0, 0, 0, 0))
    assert seal_amount(not_sealed) is None
    no_limit = q(price=0, limit_up=0, bid_vols=(1000,))
    assert seal_amount(no_limit) is None
    no_bid = q(price=11.0, limit_up=11.0, bid_vols=())
    assert seal_amount(no_bid) is None


def test_seal_ratio_computed_and_none_when_not_sealed():
    sealed = q(price=11.0, limit_up=11.0, bid_vols=(1000, 0, 0, 0, 0), amount=1e8)
    sa = seal_amount(sealed)
    assert seal_ratio(sealed) == round(sa * 1e8 / 1e8, 4)
    not_sealed = q(price=10.9, limit_up=11.0, bid_vols=(1000,), amount=1e8)
    assert seal_ratio(not_sealed) is None
    zero_amount = q(price=11.0, limit_up=11.0, bid_vols=(1000,), amount=0)
    assert seal_ratio(zero_amount) is None


# ---- 10-14 用户定：盘中全天量按日内成交量曲线折算（原来线性折算，开盘半小时高估 2.4 倍，深跌的活跃票全被判成放量） ----
from datetime import time as _t  # noqa: E402

from jevq.rules.metrics import intraday_fraction, projected_volume  # noqa: E402


def test_intraday_fraction_follows_real_u_shaped_profile():
    assert intraday_fraction(30) == 0.3004 and intraday_fraction(120) == 0.6038 and intraday_fraction(240) == 1.0
    assert 0.3004 < intraday_fraction(32) < 0.3316                      # 两个点之间线性插值
    assert intraday_fraction(250) == 1.0


def test_projection_uses_profile_not_straight_line():
    assert round(projected_volume(300, _t(10, 0))) == round(300 / 0.3004)        # 原来是 300 / 30 × 240 = 2400
    assert round(projected_volume(600, _t(11, 30))) == round(600 / 0.6038)
    assert round(projected_volume(600, _t(13, 0))) == round(600 / 0.6038)        # 午休不算
    assert projected_volume(100, _t(9, 44)) is None                               # 09:45 前仍不判断
