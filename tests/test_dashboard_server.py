import importlib.util
import sys
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).parents[1] / "dashboard" / "server.py"
SPEC = importlib.util.spec_from_file_location("dashboard_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


def quote(code, name, price=10.0, pre_close=10.0):
    return SimpleNamespace(code=code, name=name, price=price, pre_close=pre_close,
                           volume=1234, amount=1_234_000.0, ts=datetime(2026, 10, 27, 12, 2))


def quotes():
    return {"000001": quote("000001", "上证指数", 3692.58, 3697.52),
            "399001": quote("399001", "深证成指", 11496.44, 11534.38),
            "399006": quote("399006", "创业板指", 2626.31, 2638.23),
            "000559": quote("000559", "万向钱潮", 12.23, 12.71),
            "603081": quote("603081", "大丰实业", 10.50, 11.30),
            "603933": quote("603933", "睿能科技", 18.36, 20.37)}


def account_db(tmp_path):
    path = tmp_path / "paper.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cash(account TEXT PRIMARY KEY, amount REAL)")
    conn.execute("CREATE TABLE positions(account TEXT, code TEXT, shares INTEGER, cost REAL)")
    conn.execute("CREATE TABLE fills(account TEXT, ts TEXT)")
    conn.execute("INSERT INTO cash VALUES ('ai_experiment', 8000)")
    conn.execute("INSERT INTO positions VALUES ('ai_experiment', '000559', 100, 1000)")
    conn.commit()
    conn.close()
    return path


class EmptyMarket:
    def payload(self):
        return {
            "marketBreadth": {"available": False, "complete": False, "total": 0},
            "sectorBreadth": {"available": False, "complete": False, "total": 0},
        }


def dashboard_service(*args, **kwargs):
    db_path = kwargs.get("db_path") or args[0]
    kwargs["market_service"] = EmptyMarket()
    kwargs["decision_journal"] = server.DecisionJournal(Path(db_path).parent / "ai_decisions.jsonl")
    return server.DashboardService(*args, **kwargs)


def test_phase_distinguishes_lunch_break_from_market_close():
    assert server.phase(datetime(2026, 10, 27, 12, 21)) == "午间休市"
    assert server.phase(datetime(2026, 10, 27, 13, 0)) == "盘中"
    assert server.phase(datetime(2026, 10, 27, 15, 0)) == "盘后"


def test_payload_combines_live_quotes_and_readonly_account(tmp_path):
    service = dashboard_service(db_path=account_db(tmp_path), quote_fetch=lambda _: quotes())
    payload = service.payload()
    assert payload["live"]["connected"] is True
    assert payload["evidence"]["complete"] is True
    assert payload["markets"][0]["value"] == "3,692.58"
    assert payload["markets"][0]["change"] == "-0.13%"
    assert payload["observations"][0]["verdict"] == "行情已更新，等待 AI 判断"
    account = payload["account"]
    assert account["available"] is True and account["total"] == pytest.approx(9223)
    assert account["positionPct"] == pytest.approx(1223 / 9223 * 100)
    assert all(item["name"] != "真实委托" or item["value"] == "未启用" for item in payload["systemItems"])


def test_sqlite_is_opened_readonly(tmp_path):
    conn = server.readonly_connect(account_db(tmp_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO cash VALUES ('forbidden', 1)")
    finally:
        conn.close()


def test_quote_failure_keeps_last_good_data_and_marks_stale(tmp_path):
    calls = iter([quotes(), RuntimeError("network down")])

    def fetch(_):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    service = dashboard_service(db_path=account_db(tmp_path), quote_fetch=fetch, cache_seconds=0)
    service.payload()
    payload = service.payload()
    assert payload["live"]["connected"] is True and payload["live"]["stale"] is True
    assert payload["live"]["error"] == "network down"
    assert payload["markets"][0]["value"] == "3,692.58"


def test_missing_ai_account_does_not_invent_positions(tmp_path):
    path = account_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM cash")
    conn.execute("DELETE FROM positions")
    conn.commit()
    conn.close()
    payload = dashboard_service(db_path=path, quote_fetch=lambda _: quotes()).payload()
    assert payload["account"]["available"] is False
    assert payload["account"]["positions"] == 0
    assert "无 AI 账户" in payload["account"]["source"]


def test_a_second_server_cannot_bind_the_same_port_so_duplicate_launches_exit():
    """2026-11-03：start-dashboard.bat 每运行一次就起一个面板服务器。Windows 下 ThreadingHTTPServer 默认能和别的进程共绑同一端口，
    第二个不报错也不退出，常驻在后台各自刷新行情，一个吃 1~2 GB（同时跑了 4 个、共 5.8 GB）。必须独占端口。"""
    from http.server import BaseHTTPRequestHandler
    first = server.ExclusiveHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = first.server_address[1]
    try:
        with pytest.raises(OSError):
            server.ExclusiveHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    finally:
        first.server_close()
    server.ExclusiveHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler).server_close()      # 释放后能再绑


def test_main_exits_quietly_before_loading_anything_when_the_port_is_taken(monkeypatch, capsys):
    from http.server import BaseHTTPRequestHandler
    holder = server.ExclusiveHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = holder.server_address[1]

    def boom(*a, **k):
        raise AssertionError("端口被占时不该去加载行情服务")
    monkeypatch.setattr(server, "DashboardService", boom)
    monkeypatch.setattr(sys, "argv", ["server.py", "--port", str(port)])
    try:
        with pytest.raises(SystemExit) as exc:
            server.main()
    finally:
        holder.server_close()
    assert exc.value.code == 0 and str(port) in capsys.readouterr().out
