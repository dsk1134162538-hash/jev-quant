"""家里只读面板：公司导出 → 家里重建数据库 → 面板看到同样的账户、成交、净值；写操作一律拦截。"""
import json

from jevq import db
from jevq.app.home_view import BLOCKED_MSG, ReadOnlyApi, build_db
from jevq.ledger import Ledger
from jevq.sync.export import export_day
from datetime import date

DAY = date(2026, 9, 28)


def _company_db(path):
    conn = db.connect(str(path))
    Ledger(conn)
    conn.execute("UPDATE cash SET amount = 20000.0 WHERE account = 'main'")
    conn.execute("INSERT INTO positions VALUES ('main', '600001', 1000, 10005.0, 10.0, '2026-09-28')")
    conn.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
                 "VALUES ('main', '600001', 'buy', 10.0, 1000, 5.0, '2026-09-28T10:00:00', '低吸-1.5%')")
    conn.execute("INSERT INTO equity VALUES ('2026-09-28', 'main', 30100.0)")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) "
                 "VALUES ('2026-09-28T10:00:00', '600001', 'pass', '消息面中性', '{\"secret\": 1}', 'lowbuy')")
    conn.execute("INSERT INTO custom_strategies VALUES ('c1', 1, '尾盘弱转强', ?, '2026-09-25', '2026-09-28', 1)",
                 (json.dumps({"name": "尾盘弱转强", "capital": 50000,
                              "select": {"conditions": [{"field": "pct", "op": ">=", "value": 3}]},
                              "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -3}]}}]}),))
    conn.execute("INSERT INTO cash VALUES ('c1_main', 50000.0)")
    conn.commit()
    return conn


def _home(tmp_path):
    conn = _company_db(tmp_path / "company.sqlite")
    export_day(conn, DAY, out_dir=str(tmp_path / "exports"))
    conn.close()
    as_of = build_db(tmp_path / "exports", tmp_path / "home.sqlite")
    return ReadOnlyApi(db_path=str(tmp_path / "home.sqlite"), as_of=as_of, report_dir=str(tmp_path / "reports")), as_of


def test_rebuilt_db_shows_same_accounts_fills_and_curve(tmp_path):
    api, as_of = _home(tmp_path)
    assert as_of == "2026-09-28"
    s = api.status()
    main = next(a for a in s["accounts"] if a["id"] == "main")
    assert main["cash"] == 20000.0 and [p["code"] for p in main["positions"]] == ["600001"]
    assert "家里只读：数据截至 2026-09-28" in s["warning"]
    assert [f["code"] for f in api.fills("2026-09-28")] == ["600001"]
    assert api.equity_curve()["main"] == [["2026-09-28", 30100.0]]
    assert [d["code"] for d in api.decisions("2026-09-28")] == ["600001"]


def test_decision_trace_is_not_in_home_db(tmp_path):
    api, _ = _home(tmp_path)
    conn = db.connect_readonly(str(tmp_path / "home.sqlite"))
    assert conn.execute("SELECT trace FROM decisions").fetchone()[0] is None
    conn.close()


def test_custom_strategy_shows_name_and_capital_but_no_conditions(tmp_path):
    api, _ = _home(tmp_path)
    rules = api.rules_list()["rules"]
    assert [(r["id"], r["name"]) for r in rules] == [("c1", "尾盘弱转强")]
    assert rules[0]["spec"]["capital"] == 50000.0
    assert rules[0]["spec"]["select"]["conditions"] == []


def test_writes_are_blocked(tmp_path):
    api, _ = _home(tmp_path)
    assert api.start() == {"ok": False, "msg": BLOCKED_MSG}
    assert api.rule_create({"name": "x"})["errors"] == [BLOCKED_MSG]
    assert api.rule_update("c1", {"name": "x"})["errors"] == [BLOCKED_MSG]
    assert api.rule_disable("c1")["errors"] == [BLOCKED_MSG]
    conn = db.connect_readonly(str(tmp_path / "home.sqlite"))
    assert conn.execute("SELECT COUNT(*) FROM custom_strategies").fetchone()[0] == 1
    conn.close()


def test_rebuild_replaces_old_db_and_handles_missing_exports(tmp_path):
    (tmp_path / "empty").mkdir()
    assert build_db(tmp_path / "empty", tmp_path / "home.sqlite") is None
    api = ReadOnlyApi(db_path=str(tmp_path / "home.sqlite"), as_of=None, report_dir=str(tmp_path / "r"))
    assert "还没有公司的导出数据" in api.status()["warning"]
    _home(tmp_path)                                   # 再建一次覆盖旧库，不报主键冲突


class _R:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode = stdout, returncode


def test_pull_latest_only_on_clean_master():
    import home_panel
    calls = []

    def git(*args, timeout=30):
        calls.append(args)
        return {"rev-parse": _R("master\n"), "status": _R(""), "pull": _R()}[args[0]]

    assert home_panel.pull_latest(git) == ""
    assert ("pull", "--ff-only", "origin", "master") in calls


def test_pull_latest_skips_other_branch_or_dirty_tree_or_failure():
    import home_panel
    assert "home-wip" in home_panel.pull_latest(lambda *a, timeout=30: _R("home-wip\n"))
    dirty = {"rev-parse": _R("master"), "status": _R(" M run.py")}
    assert "未提交" in home_panel.pull_latest(lambda *a, timeout=30: dirty[a[0]])
    fail = {"rev-parse": _R("master"), "status": _R(""), "pull": _R(returncode=1)}
    assert "失败" in home_panel.pull_latest(lambda *a, timeout=30: fail[a[0]])
