"""阶梯缩量 / 阶梯放量 / 连跌天：交易分钟折算、取值函数、字段注册、试跑预览。"""
from datetime import datetime, time

import pytest

from factories import quote
from jevq.rules.custom import fields_meta, validate
from jevq.rules.metrics import elapsed_trading_minutes, projected_volume, vol_step
from jevq.strategies.custom import make_getter, preview

TODAY_TS = datetime(2026, 9, 24, 10, 0)
YESTERDAY = "2026-09-23"


# ---- 已过交易分钟（午休不计）与全天量折算 ----

@pytest.mark.parametrize("t, minutes", [
    (time(9, 45), 15), (time(10, 0), 30), (time(11, 30), 120), (time(12, 15), 120),
    (time(13, 0), 120), (time(13, 30), 150), (time(14, 59), 239), (time(15, 0), 240), (time(16, 30), 240),
])
def test_elapsed_trading_minutes_excludes_lunch(t, minutes):
    assert elapsed_trading_minutes(t) == minutes


def test_projected_volume_none_before_0945():
    assert projected_volume(1000, time(9, 44)) is None
    assert projected_volume(1000, time(9, 0)) is None
    assert projected_volume(1000, time(9, 45)) == 1000 / 15 * 240


def test_projected_volume_lunch_and_after_close():
    assert projected_volume(1500, time(13, 30)) == 1500 / 150 * 240
    assert projected_volume(1500, time(12, 0)) == 1500 / 120 * 240
    assert projected_volume(1500, time(15, 30)) == 1500           # 收盘后就是全天量
    assert projected_volume(0, time(10, 0)) is None                # 没成交（停牌）→ 不判断


# ---- vol_step：严格单调，每一步都要满足 ----

def test_vol_step_down_and_up():
    assert vol_step([300.0, 200.0, 150.0], 100.0, 3, down=True) is True
    assert vol_step([300.0, 200.0, 150.0], 100.0, 3, down=False) is False
    assert vol_step([100.0, 200.0, 300.0], 400.0, 3, down=False) is True
    assert vol_step([100.0, 200.0, 300.0], 400.0, 3, down=True) is False


def test_vol_step_equal_volumes_are_not_steps():
    assert vol_step([300.0, 200.0, 200.0], 100.0, 3, down=True) is False
    assert vol_step([300.0, 200.0, 150.0], 150.0, 3, down=True) is False
    assert vol_step([100.0, 200.0, 300.0], 300.0, 3, down=False) is False


def test_vol_step_uses_only_last_n_history_days():
    # 更早的一天（500 → 50 反而放量）不在 N=2 的窗口里
    assert vol_step([50.0, 500.0, 300.0, 200.0], 100.0, 2, down=True) is True
    assert vol_step([50.0, 500.0, 300.0, 200.0], 100.0, 4, down=True) is False


def test_vol_step_missing_data_is_none():
    assert vol_step([300.0, 200.0], 100.0, 3, down=True) is None          # 历史不足 N 天
    assert vol_step([300.0, None, 150.0], 100.0, 3, down=True) is None    # 窗口里有缺口 / NULL
    assert vol_step([None, 200.0, 150.0], 100.0, 2, down=True) is True    # 缺口在窗口外不影响
    assert vol_step([300.0, 200.0, 150.0], None, 3, down=True) is None    # 今天还不能折算
    assert vol_step([300.0, 200.0, 150.0], 100.0, None, down=True) is None


# ---- 取值函数 ----

def _get(feat, volume, t, ts=TODAY_TS):
    q = quote("600001", volume=volume, ts=ts)
    return make_getter("600001", q, features=feat, amount_ranks={}, concept_heats={}, env={}, t=t)


def test_getter_step_down_projects_today_volume():
    feat = {"vols": [400.0, 300.0, 200.0, 150.0], "vols_until": YESTERDAY}
    get = _get(feat, volume=12, t=time(10, 0))                 # 12 / 30 × 240 = 96 手
    assert get("vol_step_down", 3) is True
    assert get("vol_step_up", 3) is False
    get = _get(feat, volume=25, t=time(10, 0))                 # 折算 200 ≥ 150，不再缩量
    assert get("vol_step_down", 3) is False


def test_getter_step_up_after_lunch():
    feat = {"vols": [100.0, 200.0, 300.0], "vols_until": YESTERDAY}
    # 13:30 已过 150 分钟：250 / 150 × 240 = 400 > 300
    assert _get(feat, volume=250, t=time(13, 30))("vol_step_up", 3) is True
    # 同样 250 手若按不扣午休的 240 分钟算只有 250，会误判为不放量——这里确认扣了午休
    assert _get(feat, volume=180, t=time(13, 30))("vol_step_up", 3) is False   # 288 < 300


def test_getter_none_before_0945_and_without_history():
    feat = {"vols": [300.0, 200.0, 150.0], "vols_until": YESTERDAY}
    assert _get(feat, volume=10, t=time(9, 40))("vol_step_down", 3) is None
    assert _get({}, volume=10, t=time(10, 0))("vol_step_down", 3) is None
    assert _get({"vols": [200.0, 150.0], "vols_until": YESTERDAY}, 10, time(10, 0))("vol_step_down", 3) is None


def test_getter_uses_stored_volume_when_quote_is_last_history_day():
    # 周末/盘前预览：行情就是历史最后一天（2026-09-24），那天的量用库里的全天量当「今天」，
    # 不再按现在的钟点折算行情里的量（周六 10:00 折算会把全天量放大 8 倍），也不把同一天算两次
    feat = {"vols": [400.0, 300.0, 200.0, 150.0], "vols_until": "2026-09-24"}
    get = _get(feat, volume=10, t=time(10, 0))
    assert get("vol_step_down", 3) is True            # 400 > 300 > 200 > 150
    assert get("vol_step_up", 3) is False
    assert get("vol_step_down", 4) is None            # 最后一天当今天后历史只剩 3 天，不够 N=4
    feat = {"vols": [400.0, 300.0, 200.0, 250.0], "vols_until": "2026-09-24"}
    assert _get(feat, volume=10, t=time(10, 0))("vol_step_down", 3) is False
    feat = {"vols": [400.0, 300.0, 200.0, None], "vols_until": "2026-09-24"}
    assert _get(feat, volume=10, t=time(10, 0))("vol_step_down", 3) is None


def test_getter_none_when_quote_is_older_than_history():
    feat = {"vols": [300.0, 200.0, 150.0], "vols_until": "2026-09-25"}
    assert _get(feat, volume=10, t=time(10, 0))("vol_step_down", 2) is None


def _streak(feat, pct, ts=TODAY_TS):
    q = quote("600001", pct=pct, ts=ts)
    get = make_getter("600001", q, features=feat, amount_ranks={}, concept_heats={}, env={}, t=time(10, 0))
    return get("up_streak"), get("down_streak")


STREAK_FEAT = {"up_streak": 2, "down_streak": 0, "vols_until": YESTERDAY}


def test_streaks_include_today_when_today_up():
    # 先数到昨天为止连涨了 2 天，今天也在涨就再加 1；今天涨 → 连跌断了
    assert _streak(STREAK_FEAT, pct=1.5) == (3, 0)


def test_streaks_include_today_when_today_down():
    feat = {"up_streak": 0, "down_streak": 4, "vols_until": YESTERDAY}
    assert _streak(feat, pct=-2) == (0, 5)
    assert _streak(STREAK_FEAT, pct=-2) == (0, 1)          # 昨天还在连涨，今天跌 → 连跌第 1 天


def test_streaks_flat_today_breaks_both():
    assert _streak({"up_streak": 2, "down_streak": 3, "vols_until": YESTERDAY}, pct=0) == (0, 0)


def test_streaks_history_only_when_quote_not_after_history():
    # 周末/盘前预览：行情日期就是历史最后一天，今天的涨跌已经算在历史里了，不能再加一次
    feat = {"up_streak": 2, "down_streak": 0, "vols_until": "2026-09-24"}
    assert _streak(feat, pct=1.5) == (2, 0)
    assert _streak({"up_streak": 2, "down_streak": 0}, pct=1.5) == (2, 0)   # 没有历史日期 → 只看历史


def test_getter_down_streak_from_features():
    assert _streak({"up_streak": 0, "down_streak": 4, "vols_until": YESTERDAY}, pct=-1)[1] == 5


# ---- 字段注册 / 面板元数据 / 校验 ----

def test_fields_meta_exposes_new_fields():
    meta = {m["key"]: m for m in fields_meta()}
    assert meta["down_streak"]["label"] == "连跌天" and meta["down_streak"]["group"] == "历史"
    assert meta["down_streak"]["kind"] == "num" and meta["down_streak"]["unit"] == "天"
    assert meta["down_streak"]["default"] == {"op": ">=", "value": 2}
    for key, label in (("vol_step_down", "阶梯缩量"), ("vol_step_up", "阶梯放量")):
        m = meta[key]
        assert m["label"] == label and m["group"] == "量能" and m["kind"] == "bool"
        assert m["params"] == [2, 3, 4, 5]
        assert m["scopes"] == ["select", "entry", "exit"]
        assert m["default"] == {"op": "is", "value": True, "param": {"n": 3}}


def _spec(select, entry=()):
    return {"name": "量能", "capital": 30000,
            "select": {"conditions": list(select)}, "entry": {"conditions": list(entry)},
            "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -3},
                                               {"field": "vol_step_up", "op": "is", "value": True,
                                                "param": {"n": 2}}], "mode": "any"}, "reason": "x"},
                      {"when": {"conditions": [{"field": "hold_days", "op": ">=", "value": 3}]}}]}


def test_validate_accepts_new_fields_in_all_scopes():
    s = _spec([{"field": "vol_step_down", "op": "is", "value": True, "param": {"n": 3}},
               {"field": "down_streak", "op": ">=", "value": 2}],
              [{"field": "vol_step_up", "op": "is", "value": False, "param": {"n": 5}}])
    s["exits"][0]["when"]["mode"] = "all"
    s["exits"].insert(0, {"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -3}]}})
    errors, _ = validate(s)
    assert errors == []


def test_validate_rejects_bad_n_for_vol_steps():
    s = _spec([{"field": "vol_step_down", "op": "is", "value": True, "param": {"n": 6}}])
    errors, _ = validate(s)
    assert any("阶梯缩量" in e and "N" in e for e in errors)


# ---- 试跑预览 ----

def test_preview_shows_vol_step_and_down_streak_values():
    ts = datetime(2026, 9, 24, 10, 0)
    quotes = {"600001": quote("600001", pct=-1, volume=12, ts=ts), "600002": quote("600002", volume=12, ts=ts)}
    universe = {"600001": "甲", "600002": "乙"}
    features = {"600001": {"vols": [300.0, 200.0, 150.0], "vols_until": YESTERDAY, "down_streak": 2},
                "600002": {}}
    spec = {"select": {"mode": "any", "conditions": [
        {"field": "pct", "op": ">=", "value": -20},
        {"field": "vol_step_down", "op": "is", "value": True, "param": {"n": 3}},
        {"field": "vol_step_up", "op": "is", "value": True, "param": {"n": 3}},
        {"field": "down_streak", "op": ">=", "value": 2}]}}
    result = preview(spec, quotes, universe, features, {}, {}, ts)
    rows = {r["code"]: r["values"] for r in result["rows"]}
    assert rows["600001"]["阶梯缩量(N=3)"] == "是"
    assert rows["600001"]["阶梯放量(N=3)"] == "否"
    assert rows["600001"]["连跌天"] == "3天"
    assert rows["600002"]["阶梯缩量(N=3)"] == "—"
    assert rows["600002"]["连跌天"] == "—"


# ---- shrink_days：连续缩量天数（含今天），超跌反弹的成交量条件用 ----

def _shrink(feat, volume, t=time(10, 0), ts=TODAY_TS):
    from jevq.rules.metrics import shrink_days
    return shrink_days(feat, quote("600001", volume=volume, ts=ts), t)


def test_shrink_days_counts_consecutive_shrinks_including_today():
    # 10:00 已过 30 分钟：12 / 30 × 240 = 96 手；400 > 300 > 200 > 150 > 96 连续缩了 4 天
    assert _shrink({"vols": [400.0, 300.0, 200.0, 150.0], "vols_until": YESTERDAY}, volume=12) == 4


def test_shrink_days_stops_where_the_chain_breaks():
    # 96 < 150 < 200 < 300，再往前 300 > 100 是放量：只有 3 天
    assert _shrink({"vols": [100.0, 300.0, 200.0, 150.0], "vols_until": YESTERDAY}, volume=12) == 3
    # 今天折算 200 ≥ 昨天 150：今天没缩量，0 天
    assert _shrink({"vols": [400.0, 300.0, 200.0, 150.0], "vols_until": YESTERDAY}, volume=25) == 0


def test_shrink_days_is_capped_at_five():
    feat = {"vols": [700.0, 600.0, 500.0, 400.0, 300.0, 200.0], "vols_until": YESTERDAY}
    assert _shrink(feat, volume=12) == 5


def test_shrink_days_counts_only_confirmed_steps_when_older_history_has_a_gap():
    feat = {"vols": [None, 300.0, 200.0, 150.0], "vols_until": YESTERDAY}
    assert _shrink(feat, volume=12) == 3


def test_shrink_days_unknown_is_none():
    assert _shrink({}, volume=12) is None                                            # 没有历史
    assert _shrink({"vols": [], "vols_until": YESTERDAY}, volume=12) is None
    feat = {"vols": [300.0, 200.0, 150.0], "vols_until": YESTERDAY}
    assert _shrink(feat, volume=12, t=time(9, 44)) is None                           # 09:45 前不折算
    assert _shrink({"vols": [300.0, 200.0, 150.0], "vols_until": "2026-09-25"}, volume=12) is None   # 行情比历史还旧


def test_shrink_days_uses_stored_volume_when_quote_is_last_history_day():
    # 周末/盘前预览：行情就是历史最后一天，用库里那天的全天量当「今天」，与 vol_step 取值函数同口径
    feat = {"vols": [400.0, 300.0, 200.0, 150.0], "vols_until": "2026-09-24"}
    assert _shrink(feat, volume=10) == 3
