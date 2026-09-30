from datetime import date

from jevq import accounts, db
from jevq.data import custom_store

TODAY = date(2026, 9, 23)
NEXT_DAY = date(2026, 9, 24)
DAYS = [date(2026, 9, 22), TODAY, NEXT_DAY, date(2026, 9, 25)]


def spec(name="尾盘弱转强"):
    return {"name": name, "capital": 30000,
            "select": {"mode": "all", "conditions": [{"field": "pct", "op": "between", "value": [-2, 0]}]},
            "entry": {"conditions": []},
            "exits": [{"when": {"conditions": [{"field": "pct", "op": "<=", "value": -3}]}, "reason": "止损"}],
            "jev": {"notices": True, "news": True}}


BUILTIN = ("board_main", "board_shadow", "rebound_main", "rebound_shadow")


def test_all_accounts_is_builtin_four_with_no_custom_strategies():
    conn = db.connect(":memory:")
    assert accounts.all_accounts(conn, TODAY) == BUILTIN          # 低吸 main/shadow 已下线


def test_all_accounts_includes_active_custom_strategy_next_day():
    conn = db.connect(":memory:")
    custom_store.save(conn, spec(), TODAY, DAYS)
    assert accounts.all_accounts(conn, TODAY) == BUILTIN
    assert accounts.all_accounts(conn, NEXT_DAY) == (*BUILTIN, "c1_main", "c1_shadow")


def test_account_names_uses_custom_strategy_name():
    conn = db.connect(":memory:")
    custom_store.save(conn, spec(name="尾盘弱转强"), TODAY, DAYS)
    names = accounts.account_names(conn, NEXT_DAY)
    assert names["c1_main"] == "尾盘弱转强·主(Jev)"
    assert names["c1_shadow"] == "尾盘弱转强·影子"
    assert names["board_main"] == "首板·主(Jev)"                  # 内置账户名不变
    # 低吸下线：历史成交还要显示名字
    assert names["main"] == "低吸·主(已下线)" and names["shadow"] == "低吸·影子(已下线)"


def test_account_strategy_maps_custom_accounts_to_id():
    conn = db.connect(":memory:")
    custom_store.save(conn, spec(), TODAY, DAYS)
    strategy = accounts.account_strategy(conn, NEXT_DAY)
    assert strategy["c1_main"] == "c1" and strategy["c1_shadow"] == "c1"
    assert strategy["rebound_main"] == "rebound"
    assert "main" not in strategy and "shadow" not in strategy


def test_disabled_but_held_custom_strategy_still_in_all_accounts():
    conn = db.connect(":memory:")
    sid, _ = custom_store.save(conn, spec(), TODAY, DAYS)
    custom_store.disable(conn, sid, NEXT_DAY, DAYS)
    conn.execute("INSERT INTO cash(account, amount) VALUES (?, 30000)", (f"{sid}_main",))
    conn.execute("INSERT INTO positions(account, code, shares, cost, buy_price, buy_date) "
                "VALUES (?, '600001', 1000, 10000, 10.0, '2026-09-24')", (f"{sid}_main",))
    conn.commit()
    later = date(2026, 9, 25)                              # 停用版本生效日
    assert f"{sid}_main" in accounts.all_accounts(conn, later)
    assert f"{sid}_shadow" in accounts.all_accounts(conn, later)
