"""Engine._fetch 该拉哪些持仓行情，改由构造时注入的 accounts_provider() 决定；
默认值必须是内置四个账户（低吸/扫首板的既有行为不能变）。"""
from datetime import date, datetime

from factories import quote
from jevq import db
from jevq.engine import Engine
from jevq.ledger import Ledger


class Judge:
    def check(self, *args):
        return "pass", "ok"


class Market:
    def sentiment(self, stats):
        return "正常"


def _engine(conn, ledger, fetch, accounts_provider=None):
    universe = {"600001": "x"}
    return Engine(conn, ledger, Judge(), Judge(), Market(), fetch, lambda d: {}, universe,
                 {"600001": 5.0}, date(2026, 9, 22), "中性", accounts_provider=accounts_provider)


def test_default_accounts_provider_fetches_builtin_four_accounts_positions():
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    ledger.buy("board_main", quote("600099", pct=-1.0), datetime(2026, 9, 23, 10, 0), "x")
    seen = []

    def fetch(codes):
        seen.append(sorted(codes))
        return {c: quote(c, pct=-1.0) for c in codes}

    eng = _engine(conn, ledger, fetch)
    eng.tick(datetime(2026, 9, 23, 10, 0))
    assert "600099" in seen[0]                       # board_main 是内置四个账户之一，默认就该被看到


def test_custom_accounts_provider_used_instead_of_default():
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    ledger.ensure_account("c1_main", 30000)
    ledger.buy("c1_main", quote("600077", pct=-1.0), datetime(2026, 9, 23, 10, 0), "x")
    seen = []

    def fetch(codes):
        seen.append(sorted(codes))
        return {c: quote(c, pct=-1.0) for c in codes}

    eng = _engine(conn, ledger, fetch, accounts_provider=lambda: ("c1_main", "c1_shadow"))
    eng.tick(datetime(2026, 9, 23, 10, 0))
    assert "600077" in seen[0]                        # 只有注入的 provider 能让引擎看到这个自定义账户的持仓


def test_default_accounts_provider_ignores_accounts_not_in_builtin_four():
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    ledger.ensure_account("c1_main", 30000)
    ledger.buy("c1_main", quote("600055", pct=-1.0), datetime(2026, 9, 23, 10, 0), "x")
    seen = []

    def fetch(codes):
        seen.append(sorted(codes))
        return {c: quote(c, pct=-1.0) for c in codes}

    eng = _engine(conn, ledger, fetch)                # 不注入 provider：默认只看内置四个账户
    eng.tick(datetime(2026, 9, 23, 10, 0))
    assert "600055" not in seen[0]
