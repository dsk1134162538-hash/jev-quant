from datetime import date

from jevq import db
from jevq.data import custom_store

DAYS = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24),
        date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29)]
TODAY = date(2026, 9, 23)                 # 交易日；下一交易日是 9-24


def spec(name="尾盘弱转强", capital=30000):
    return {"name": name, "capital": capital,
            "select": {"mode": "all", "conditions": [{"field": "pct", "op": "between", "value": [-2, 0]}]},
            "rank": {"field": "amount", "order": "desc"},
            "entry": {"mode": "all", "conditions": [{"field": "time", "op": "between",
                                                     "value": ["14:30", "14:55"]}]},
            "exits": [{"when": {"conditions": [{"field": "pct", "op": "<=", "value": -3}]}, "reason": "止损"}],
            "jev": {"notices": True, "news": True}}


def conn():
    return db.connect(":memory:")


def test_next_id_starts_at_c1_and_increments():
    c = conn()
    assert custom_store.next_id(c) == "c1"
    custom_store.save(c, spec(), TODAY, DAYS)
    assert custom_store.next_id(c) == "c2"


def test_save_new_strategy_is_c1_version_1_active_next_trade_day():
    c = conn()
    sid, version = custom_store.save(c, spec(), TODAY, DAYS)
    assert (sid, version) == ("c1", 1)
    row = c.execute("SELECT active_from, enabled FROM custom_strategies WHERE id=? AND version=?",
                    (sid, version)).fetchone()
    assert row == ("2026-09-24", 1)


def test_save_invalid_spec_returns_errors_and_inserts_nothing():
    c = conn()
    bad = spec()
    bad["exits"] = []                     # 没有止损，校验必挂
    sid, errors = custom_store.save(c, bad, TODAY, DAYS)
    assert sid is None
    assert errors and any("止损" in e or "卖出" in e for e in errors)
    assert c.execute("SELECT COUNT(*) FROM custom_strategies").fetchone()[0] == 0


def test_save_existing_sid_bumps_version():
    c = conn()
    sid, v1 = custom_store.save(c, spec(), TODAY, DAYS)
    sid2, v2 = custom_store.save(c, spec(name="改名"), TODAY, DAYS, sid=sid)
    assert sid2 == sid and v2 == v1 + 1


def test_new_version_not_active_until_next_trade_day():
    c = conn()
    sid, _ = custom_store.save(c, spec(name="旧版本"), TODAY, DAYS)
    custom_store.save(c, spec(name="新版本"), TODAY, DAYS, sid=sid)
    still_today = custom_store.active_for(c, TODAY)
    assert still_today == []                            # today 之前存的版本，active_from 是 9-24，今天还没生效
    next_day = custom_store.active_for(c, date(2026, 9, 24))
    assert len(next_day) == 1 and next_day[0]["name"] == "新版本" and next_day[0]["can_buy"] is True


def test_disable_writes_enabled_zero_version_effective_next_trade_day():
    c = conn()
    sid, v1 = custom_store.save(c, spec(), TODAY, DAYS)              # v1 的 active_from 是 9-24
    day_v1_active = date(2026, 9, 24)
    v2, errors = custom_store.disable(c, sid, day_v1_active, DAYS)    # 停用发生在 v1 已经在跑的那天
    assert errors == [] and v2 == v1 + 1
    row = c.execute("SELECT enabled, active_from FROM custom_strategies WHERE id=? AND version=?",
                    (sid, v2)).fetchone()
    assert row == (0, "2026-09-25")
    # 停用生效前（v1 在跑的当天）还照常开新仓；到停用的 active_from 那天才不再出现在 active_for 里。
    assert [s["version"] for s in custom_store.active_for(c, day_v1_active)] == [v1]
    assert custom_store.active_for(c, date(2026, 9, 25)) == []


def test_disabled_but_still_holding_returned_by_holders_with_can_buy_false():
    c = conn()
    sid, _ = custom_store.save(c, spec(), TODAY, DAYS)
    enabled_day = date(2026, 9, 24)
    custom_store.disable(c, sid, enabled_day, DAYS)               # 停用版本次日（9-25）才生效
    c.execute("INSERT INTO cash(account, amount) VALUES (?, 30000)", (f"{sid}_main",))
    c.execute("INSERT INTO positions(account, code, shares, cost, buy_price, buy_date) "
             "VALUES (?, '600001', 1000, 10000, 10.0, '2026-09-24')", (f"{sid}_main",))
    c.commit()
    disabled_day = date(2026, 9, 25)
    assert custom_store.active_for(c, disabled_day) == []          # 停用生效，不再开新仓
    held = custom_store.holders(c, disabled_day)
    assert len(held) == 1
    assert held[0]["id"] == sid and held[0]["version"] == 1 and held[0]["can_buy"] is False


def test_holders_excludes_ids_without_positions():
    c = conn()
    custom_store.save(c, spec(), TODAY, DAYS)
    assert custom_store.holders(c, date(2026, 9, 24)) == []


def test_active_for_caps_at_five_and_orders_by_id():
    c = conn()
    for i in range(5):
        custom_store.save(c, spec(name=f"策略{i}"), TODAY, DAYS)
    result = custom_store.save(c, spec(name="第六个"), TODAY, DAYS)
    assert result == (None, ["同时启用的自定义策略最多 5 个"])
    active = custom_store.active_for(c, date(2026, 9, 24))
    assert [s["id"] for s in active] == ["c1", "c2", "c3", "c4", "c5"]


def test_history_returns_all_versions_in_order():
    c = conn()
    sid, _ = custom_store.save(c, spec(name="v1"), TODAY, DAYS)
    custom_store.save(c, spec(name="v2"), TODAY, DAYS, sid=sid)
    custom_store.disable(c, sid, TODAY, DAYS)
    hist = custom_store.history(c, sid)
    assert [h["version"] for h in hist] == [1, 2, 3]
    assert hist[0]["name"] == "v1" and hist[1]["name"] == "v2"
    assert hist[2]["enabled"] is False


def test_save_with_no_future_trade_day_returns_error_instead_of_raising():
    c = conn()
    sid, errors = custom_store.save(c, spec(), date(2026, 9, 29), DAYS)   # 交易日列表里没有更晚的日子
    assert sid is None and errors
    assert c.execute("SELECT COUNT(*) FROM custom_strategies").fetchone()[0] == 0


def test_disable_with_no_future_trade_day_returns_error_instead_of_raising():
    c = conn()
    sid, _ = custom_store.save(c, spec(), TODAY, DAYS)
    version, errors = custom_store.disable(c, sid, date(2026, 9, 29), DAYS)
    assert version is None and errors


def test_disable_unknown_strategy_returns_error_instead_of_raising():
    c = conn()
    version, errors = custom_store.disable(c, "c99", TODAY, DAYS)
    assert version is None and errors == ["未知策略 c99"]


def test_save_rejects_capital_change_on_existing_strategy():
    c = conn()
    sid, _ = custom_store.save(c, spec(capital=30000), TODAY, DAYS)
    new_sid, errors = custom_store.save(c, spec(capital=20000), TODAY, DAYS, sid=sid)
    assert new_sid is None
    assert errors == [custom_store.CAPITAL_FROZEN_ERROR]
    # 没有写入新版本
    assert [h["version"] for h in custom_store.history(c, sid)] == [1]


def test_save_allows_unchanged_capital_on_existing_strategy():
    c = conn()
    sid, v1 = custom_store.save(c, spec(capital=30000), TODAY, DAYS)
    sid2, v2 = custom_store.save(c, spec(name="改名", capital=30000), TODAY, DAYS, sid=sid)
    assert sid2 == sid and v2 == v1 + 1


def test_capital_of_returns_version_one_capital_and_none_for_unknown():
    c = conn()
    sid, _ = custom_store.save(c, spec(capital=25000), TODAY, DAYS)
    custom_store.save(c, spec(name="改名", capital=25000), TODAY, DAYS, sid=sid)  # 版本 2，资金不变
    assert custom_store.capital_of(c, sid) == 25000
    assert custom_store.capital_of(c, "c99") is None
