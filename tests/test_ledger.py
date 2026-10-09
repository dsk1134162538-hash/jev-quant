from datetime import date, datetime

import pytest

from factories import quote
from jevq import db
from jevq.ledger import Ledger, affordable_shares, buy_fee, sell_fee

T = datetime(2026, 9, 23, 14, 50)
MAIN, SHADOW = "rebound_main", "rebound_shadow"
BUILTIN = ("board_main", "board_shadow", "rebound_main", "rebound_shadow", "lowbuy_main", "lowbuy_shadow", "regime_main")


@pytest.fixture
def ledger():
    return Ledger(db.connect(":memory:"))


def test_fees():
    assert buy_fee(15000) == 5.15            # 佣金 3.75 不足 5 元按 5 元 + 过户费 0.15
    assert sell_fee(15000) == 12.65          # 5 + 印花税 7.5 + 过户费 0.15
    assert buy_fee(40000) == 10.4            # 佣金 10 + 过户费 0.4


def test_affordable_shares_leaves_room_for_fee():
    assert affordable_shares(15000, 10.0) == 1400
    assert affordable_shares(15000, 9.99) == 1500
    assert affordable_shares(15000, 151.0) == 0


def test_buy_updates_cash_and_position(ledger):
    f = ledger.buy(MAIN, quote(pct=-1.0), T, "test")
    shares = affordable_shares(30000, 9.9)
    assert f.shares == shares and f.price == 9.9
    amount = shares * 9.9
    assert ledger.cash(MAIN) == pytest.approx(30000 - amount - buy_fee(amount))
    assert ledger.holds(MAIN, "600001")
    assert ledger.cash(SHADOW) == 30000


def test_single_position_and_no_duplicate(ledger):
    assert ledger.buy(MAIN, quote("600001", pct=-1), T, "")
    assert ledger.buy(MAIN, quote("600001", pct=-1), T, "") is None
    assert ledger.buy(MAIN, quote("600002", pct=-1), T, "") is None
    assert ledger.free_slots(MAIN) == 0


def test_cannot_buy_at_limit_up_or_suspended(ledger):
    assert ledger.buy(MAIN, quote(pct=10.0), T, "") is None
    assert ledger.buy(MAIN, quote(pct=-1, volume=0), T, "") is None


def test_sell_round_trip(ledger):
    ledger.buy(MAIN, quote(pct=-1.0), T, "")
    pos = ledger.positions(MAIN)[0]
    f = ledger.sell(MAIN, pos, quote(pct=1.0), datetime(2026, 9, 24, 9, 31), "高开止盈")
    assert f.side == "sell" and f.price == 10.1
    assert not ledger.holds(MAIN, "600001")
    shares = affordable_shares(30000, 9.9)
    buy_amount, sell_amount = shares * 9.9, shares * 10.1
    expected = 30000 - buy_amount - buy_fee(buy_amount) + sell_amount - sell_fee(sell_amount)
    assert ledger.cash(MAIN) == pytest.approx(expected)


def test_cannot_sell_at_limit_down(ledger):
    ledger.buy(MAIN, quote(pct=-1.0), T, "")
    pos = ledger.positions(MAIN)[0]
    assert ledger.sell(MAIN, pos, quote(pct=-10.0), T, "止损") is None
    assert ledger.holds(MAIN, "600001")


def test_equity_and_fills_on(ledger):
    ledger.buy(MAIN, quote(pct=-1.0), T, "")
    value = ledger.equity(MAIN, {"600001": 10.0})
    shares = affordable_shares(30000, 9.9)
    assert value == pytest.approx(ledger.cash(MAIN) + shares * 10.0)
    assert len(ledger.fills_on(date(2026, 9, 23))) == 1
    assert ledger.fills_on(date(2026, 9, 24)) == []


def test_full_position_uses_all_cash(ledger):
    f = ledger.buy(MAIN, quote(pct=0.0), T, "")
    shares = affordable_shares(30000, 10.0)
    assert f.shares == shares
    assert ledger.cash(MAIN) < 1010


def test_two_ledgers_same_file_sell_once(tmp_path):
    path = str(tmp_path / "t.sqlite")
    a, b = Ledger(db.connect(path)), Ledger(db.connect(path))
    a.buy(MAIN, quote(pct=-1.0), T, "")
    cash_after_buy = a.cash(MAIN)
    pos_a, pos_b = a.positions(MAIN)[0], b.positions(MAIN)[0]
    when = datetime(2026, 9, 24, 9, 31)
    f1 = a.sell(MAIN, pos_a, quote(pct=1.0), when, "高开止盈")
    f2 = b.sell(MAIN, pos_b, quote(pct=1.0), when, "高开止盈")
    assert f1 is not None and f2 is None
    fresh = Ledger(db.connect(path))
    sells = [f for f in fresh.fills_on(date(2026, 9, 24)) if f.side == "sell"]
    assert len(sells) == 1
    amount = pos_a.shares * 10.1
    assert fresh.cash(MAIN) == pytest.approx(cash_after_buy + amount - sell_fee(amount))


def test_ledger_init_false_does_not_create_cash_rows():
    conn = db.connect(":memory:")
    Ledger(conn, init=False)
    assert conn.execute("SELECT COUNT(*) FROM cash").fetchone()[0] == 0


def test_new_ledger_seeds_builtin_accounts_only(ledger):
    """低吸下线：不再给 main/shadow 开户。"""
    assert dict(ledger.conn.execute("SELECT account, amount FROM cash")) == {a: 30000 for a in BUILTIN}


def test_ledger_keeps_retired_lowbuy_rows_untouched():
    """库里低吸 main/shadow 的现金、持仓是历史，Ledger 不删、不重置。"""
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO cash VALUES ('main', 1234.5)")
    conn.execute("INSERT INTO positions VALUES ('main', '600001', 100, 1005.1, 10.0, '2026-09-21')")
    conn.commit()
    ledger = Ledger(conn)
    assert ledger.cash("main") == 1234.5
    assert [p.code for p in ledger.positions("main")] == ["600001"]


def test_ensure_account_creates_cash_row_with_given_capital(ledger):
    ledger.ensure_account("c1_main", 20000)
    assert ledger.cash("c1_main") == 20000


def test_ensure_account_is_idempotent(ledger):
    ledger.ensure_account("c1_main", 20000)
    ledger.buy("c1_main", quote(pct=-1.0), T, "x")
    ledger.ensure_account("c1_main", 20000)          # 第二次调用不应该把现金重置回 20000
    assert ledger.cash("c1_main") < 20000


def test_ledger_init_only_seeds_builtin_not_custom_accounts():
    conn = db.connect(":memory:")
    Ledger(conn)
    assert conn.execute("SELECT COUNT(*) FROM cash").fetchone()[0] == 7
