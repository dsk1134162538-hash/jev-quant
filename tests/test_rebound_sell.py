"""超跌反弹卖出：相对买入价止损 -2%、浮盈 ≥ +1.5% 后从当日最高回落 0.8 点止盈、最多拿 2 个交易日。"""
from datetime import date, datetime

from factories import position

from jevq.models import Quote
from jevq.rules.rebound_sell import decide_rebound_sell

BUY_DAY = date(2026, 9, 23)
D1 = datetime(2026, 9, 24, 10, 0)


def pos(buy_price=10.0):
    return position("600001", account="rebound_main", buy_price=buy_price, buy_date=BUY_DAY)


def q(price, high=None, open_=None, pre_close=10.0, ts=D1):
    high = max(price, high or price, open_ or price)
    return Quote(code="600001", name="甲", price=price, pre_close=pre_close, open=open_ or price, high=high,
                 low=min(price, open_ or price), volume=1000, amount=1e7, limit_up=round(pre_close * 1.1, 2),
                 limit_down=round(pre_close * 0.9, 2), ts=ts)


def test_no_sell_on_buy_day():
    assert not decide_rebound_sell(pos(), q(9.0), datetime(2026, 9, 23, 14, 56), hold_days=0).sell


def test_stop_loss_relative_to_buy_price():
    d = decide_rebound_sell(pos(), q(9.80), D1, hold_days=1)
    assert d.sell and d.reason == "止损"
    assert not decide_rebound_sell(pos(), q(9.81), D1, hold_days=1).sell


def test_low_open_bounce_must_sell_even_at_a_loss():
    """用户定的：低开 ≤ -1.5% 后从当天最低拉起 0.8 个点必须卖（买入 9.80，持仓还亏着也卖）。"""
    d = decide_rebound_sell(pos(9.80), q(9.73, open_=9.63, pre_close=9.78), D1, hold_days=1)
    assert d.sell and d.reason == "低开反抽"


def test_low_open_bounce_measured_from_day_low():
    # 开盘 -1.5%，先砸到 -2.5%（9.75），反弹到 -1.7%（9.83）：从最低拉起 0.8 → 卖
    qq = q(9.83, open_=9.85, pre_close=10.0)
    qq = type(qq)(**{**qq.__dict__, "low": 9.75})
    d = decide_rebound_sell(pos(9.90), qq, D1, hold_days=1)
    assert d.sell and d.reason == "低开反抽"
    # 只拉起 0.7 → 不卖
    qq = type(qq)(**{**qq.__dict__, "price": 9.82})
    assert not decide_rebound_sell(pos(9.90), qq, D1, hold_days=1).sell


def test_low_open_bounce_needs_low_open():
    # 开盘 -1.4%，从最低拉起 1 个点：不是低开，不触发
    qq = q(9.96, open_=9.86, pre_close=10.0)
    assert not decide_rebound_sell(pos(9.90), qq, D1, hold_days=1).sell


def test_low_open_bounce_ignores_buy_pressure():
    d = decide_rebound_sell(pos(9.80), q(9.73, open_=9.63, pre_close=9.78), D1, hold_days=1, buy_pressure=True)
    assert d.sell and d.reason == "低开反抽"


def test_trailing_not_armed_below_start():
    # 最高 +1.4%，回落到 +0.5%：还没启动跟踪止盈
    assert not decide_rebound_sell(pos(), q(10.05, high=10.14), D1, hold_days=1).sell


def test_trailing_take_profit_after_pullback():
    # 最高 +2.5%，回落到 +1.7%（回落 0.8 点）→ 卖
    d = decide_rebound_sell(pos(), q(10.17, high=10.25), D1, hold_days=1)
    assert d.sell and d.reason == "跟踪止盈"
    # 回落 0.7 点 → 继续拿
    assert not decide_rebound_sell(pos(), q(10.18, high=10.25), D1, hold_days=1).sell


def test_buy_pressure_delays_trailing_take_profit():
    assert not decide_rebound_sell(pos(), q(10.17, high=10.25), D1, hold_days=1, buy_pressure=True).sell


def test_back_to_cost_sells_even_with_buy_pressure():
    """保底：启动过跟踪止盈（最高 +2.5%）的票，跌回成本价就卖，主买多也卖。"""
    d = decide_rebound_sell(pos(), q(10.0, high=10.25), D1, hold_days=1, buy_pressure=True)
    assert d.sell and d.reason == "回到成本"


def test_stop_loss_ignores_buy_pressure():
    d = decide_rebound_sell(pos(), q(9.80), D1, hold_days=1, buy_pressure=True)
    assert d.sell and d.reason == "止损"


def test_first_day_eod_keeps_second_day_eod_clears():
    eod = datetime(2026, 9, 24, 14, 55)
    assert not decide_rebound_sell(pos(), q(10.05), eod, hold_days=1).sell
    d = decide_rebound_sell(pos(), q(10.05), eod.replace(day=25), hold_days=2)
    assert d.sell and d.reason == "到期清仓"


def test_missed_days_sell_at_open():
    d = decide_rebound_sell(pos(), q(10.05), datetime(2026, 9, 28, 9, 30), hold_days=3)
    assert d.sell and d.reason == "隔夜超期"


def test_unknown_hold_days_clears_at_eod():
    """交易日历不可用（hold_days=None）：保守处理，当天尾盘清仓。"""
    assert decide_rebound_sell(pos(), q(10.05), datetime(2026, 9, 24, 14, 55), hold_days=None).sell
    assert not decide_rebound_sell(pos(), q(10.05), datetime(2026, 9, 24, 10, 0), hold_days=None).sell


# ---- 主账户（实盘跟的账户）：止损要连续 2 分钟确认 + 问 Jev 今天走向；-3.5% 硬止损（用户 2026-10-09 定的）----

class Trend:
    """假的「问 Jev 今天走向」：记调用次数。"""
    def __init__(self, label):
        self.label, self.calls = label, 0

    def __call__(self):
        self.calls += 1
        return self.label


def test_stop_waits_for_second_minute_below_line():
    """10-09 雪迪龙：买 7.13，10:14 瞬间 6.98（-2.1%），下一分钟回到 6.99：第一分钟不卖、不问 Jev。"""
    t = Trend("单边下跌")
    d = decide_rebound_sell(pos(), q(9.80), D1, hold_days=1, stop_minutes=1, trend=t)
    assert not d.sell and "待确认" in d.reason
    assert t.calls == 0


def test_confirmed_stop_sells_when_jev_sees_no_rebound():
    t = Trend("单边下跌")
    d = decide_rebound_sell(pos(), q(9.80), D1, hold_days=1, stop_minutes=2, trend=t)
    assert d.sell and d.reason == "止损（单边下跌）"
    assert t.calls == 1


def test_confirmed_stop_holds_when_jev_sees_rebound():
    for label in ("探底回升", "低开高走"):
        d = decide_rebound_sell(pos(), q(9.80), D1, hold_days=1, stop_minutes=3, trend=Trend(label))
        assert not d.sell and d.reason == f"止损暂缓（{label}）"


def test_confirmed_stop_sells_when_jev_fails():
    """Jev 超时/出错（返回 None）→ 照原规则卖，宁可卖错不能拿着亏。"""
    d = decide_rebound_sell(pos(), q(9.80), D1, hold_days=1, stop_minutes=2, trend=Trend(None))
    assert d.sell and d.reason == "止损（Jev 没判出来）"


def test_hard_stop_sells_at_once_without_asking_jev():
    t = Trend("探底回升")
    d = decide_rebound_sell(pos(), q(9.65), D1, hold_days=1, stop_minutes=1, trend=t)
    assert d.sell and d.reason == "硬止损"
    assert t.calls == 0
    assert not decide_rebound_sell(pos(), q(9.66), D1, hold_days=1, stop_minutes=3, trend=Trend("探底回升")).sell


def test_low_open_bounce_holds_when_jev_sees_rising_day():
    d = decide_rebound_sell(pos(9.80), q(9.73, open_=9.63, pre_close=9.78), D1, hold_days=1,
                            stop_minutes=0, trend=Trend("低开高走"))
    assert not d.sell and d.reason == "低开反抽暂缓（低开高走）"
    d = decide_rebound_sell(pos(9.80), q(9.73, open_=9.63, pre_close=9.78), D1, hold_days=1,
                            stop_minutes=0, trend=Trend("冲高回落"))
    assert d.sell and d.reason == "低开反抽（冲高回落）"


def test_jev_asked_once_when_stop_and_low_open_both_apply():
    # 买 10.0，昨收 10.0，开 9.80（-2%），最低 9.70，现价 9.79（拉起 0.9 点，持仓 -2.1%）
    qq = q(9.79, open_=9.80, pre_close=10.0)
    qq = type(qq)(**{**qq.__dict__, "low": 9.70})
    t = Trend("探底回升")
    d = decide_rebound_sell(pos(), qq, D1, hold_days=1, stop_minutes=2, trend=t)
    assert not d.sell and t.calls == 1


def test_eod_clear_still_sells_while_stop_is_held():
    eod = datetime(2026, 9, 25, 14, 55)
    d = decide_rebound_sell(pos(), q(9.80, ts=eod), eod, hold_days=2, stop_minutes=5, trend=Trend("探底回升"))
    assert d.sell and d.reason == "到期清仓"


def test_back_to_cost_is_not_held_by_jev():
    t = Trend("探底回升")
    d = decide_rebound_sell(pos(), q(10.0, high=10.25), D1, hold_days=1, stop_minutes=0, trend=t)
    assert d.sell and d.reason == "回到成本" and t.calls == 0


def test_trailing_pullback_can_be_widened():
    """10-14 用户定：主账户回落 1.5 点才卖（调用方传 pullback），不传还是 0.8 点。"""
    p = q(10.17, high=10.25)                                           # 最高 +2.5%，现价 +1.7%
    assert decide_rebound_sell(pos(), p, D1, hold_days=1).reason == "跟踪止盈"
    assert not decide_rebound_sell(pos(), p, D1, hold_days=1, pullback=1.5).sell
    assert decide_rebound_sell(pos(), q(10.10, high=10.25), D1, hold_days=1, pullback=1.5).reason == "跟踪止盈"
