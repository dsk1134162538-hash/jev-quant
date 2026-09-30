"""手机页测试：真实 ThreadingHTTPServer 绑 127.0.0.1:0 + requests 打本机回环（不算联网）。
覆盖：暂停/恢复/立即卖出写对状态、非 JSON 415、跨站 Origin 403、JSON 解析失败 400、
/api/mode 本机可改（_is_local 单测非本机返回 False）、status 不含资金字段。
"""
import threading
from contextlib import contextmanager
from datetime import date, datetime

import pytest
import requests

from factories import quote
from jevq import config, db
from jevq.live import store as live_store
from jevq.live.store import LivePosition
from jevq.live.web import _is_local, make_handler, serve, status

NOW = datetime(2026, 9, 29, 10, 0)


def _post(url, **kw):
    # conftest.py 把 jevq.notify.requests.post（同一个 requests 模块对象）打成一调用就报错，
    # 防的是真的联网推送；这里打本机回环走独立 Session().post，不经过那个被打桩的模块级函数。
    return requests.Session().post(url, **kw)


@pytest.fixture
def conn():
    return db.connect(":memory:")


@contextmanager
def running_server(conn, clock=lambda: NOW, quotes=lambda codes: {}):
    handler_cls = make_handler(lambda: conn, clock=clock, quotes=quotes)
    srv = serve(["127.0.0.1"], 0, handler_cls)[0]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def server(conn):
    with running_server(conn) as base_url:
        yield base_url


# ---- _is_local ----

def test_is_local_accepts_loopback_only():
    assert _is_local("127.0.0.1") is True
    assert _is_local("100.64.1.2") is False
    assert _is_local("::1") is False


# ---- status() 内容 ----

def test_status_never_includes_cash_or_client_holdings(conn):
    live_store.set_mode(conn, "dryrun")
    data = status(conn, NOW, lambda codes: {})
    assert "cash" not in data
    assert "balance" not in data
    assert set(data) == {"mode", "paused", "last_read_ok", "positions", "to_sell", "orders", "skipped"}


def test_status_position_shows_price_and_pnl_pct_from_quotes(conn):
    live_store.put_position(conn, LivePosition("600001", 100, 1005.0, 10.0, date(2026, 9, 29), 10.0))
    q = quote(code="600001", pct=5.0)   # price 10.5
    data = status(conn, NOW, lambda codes: {"600001": q})
    pos = data["positions"][0]
    assert pos["price"] == 10.5
    assert pos["pnl_pct"] == 5.0


def test_status_position_without_quote_shown_without_price(conn):
    live_store.put_position(conn, LivePosition("600001", 100, 1005.0, 10.0, date(2026, 9, 29), 10.0))

    def boom(codes):
        raise RuntimeError("行情取不到")

    data = status(conn, NOW, boom)
    pos = data["positions"][0]
    assert pos["price"] is None
    assert pos["pnl_pct"] is None


def test_status_to_sell_is_program_position_no_longer_held_by_sim_account(conn):
    live_store.put_position(conn, LivePosition("600001", 100, 1005.0, 10.0, date(2026, 9, 29), 10.0))
    live_store.put_position(conn, LivePosition("600002", 200, 2005.0, 10.0, date(2026, 9, 29), 10.0))
    conn.execute("INSERT INTO positions(account, code, shares, cost, buy_price, buy_date) "
                "VALUES (?, '600001', 100, 1005.0, 10.0, '2026-09-29')", (config.LIVE_ACCOUNT,))
    conn.commit()
    data = status(conn, NOW, lambda codes: {})
    assert data["to_sell"] == ["600002"]


# ---- HTTP 接口 ----

def test_pause_30_minutes_writes_until_and_manual_kind(server, conn):
    r = _post(f"{server}/api/pause", json={"minutes": 30})
    assert r.status_code == 200
    p = live_store.paused(conn)
    assert p["until"] == NOW.replace(minute=30)
    assert p["reason"] == "手机暂停"
    assert p["kind"] == "manual"


def test_pause_until_close_writes_today_15_00(server, conn):
    r = _post(f"{server}/api/pause", json={"until": "close"})
    assert r.status_code == 200
    p = live_store.paused(conn)
    assert p["until"] == datetime.combine(NOW.date(), config.LIVE_PAUSE_CLOSE)


def test_pause_minutes_capped_at_15_00_close(conn):
    near_close = datetime(2026, 9, 29, 14, 45)
    with running_server(conn, clock=lambda: near_close) as base_url:
        r = _post(f"{base_url}/api/pause", json={"minutes": 30})
    assert r.status_code == 200
    p = live_store.paused(conn)
    assert p["until"] == datetime.combine(near_close.date(), config.LIVE_PAUSE_CLOSE)


def test_pause_rejects_bad_payload(server):
    r = _post(f"{server}/api/pause", json={"minutes": 45})
    assert r.status_code == 400


def test_resume_sets_resume_requested_state(server, conn):
    r = _post(f"{server}/api/resume", json={})
    assert r.status_code == 200
    assert live_store.get_state(conn, "resume_requested") == "1"


def test_sell_all_writes_remote_sell_intent(server, conn):
    r = _post(f"{server}/api/sell_all", json={})
    assert r.status_code == 200
    row = conn.execute("SELECT source, side FROM live_intents").fetchone()
    assert row == ("remote", "sell")


def test_mode_changes_when_request_is_local(server, conn):
    r = _post(f"{server}/api/mode", json={"mode": "dryrun"})
    assert r.status_code == 200
    assert live_store.mode(conn) == "dryrun"


def test_mode_rejects_unknown_mode(server):
    r = _post(f"{server}/api/mode", json={"mode": "nope"})
    assert r.status_code == 400


def test_post_without_json_content_type_returns_415(server):
    r = _post(f"{server}/api/resume", data="{}", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_post_with_cross_site_origin_returns_403(server):
    r = _post(f"{server}/api/resume", json={},
                      headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403


def test_post_with_same_origin_is_accepted(server):
    from urllib.parse import urlparse
    host = urlparse(server).netloc
    r = _post(f"{server}/api/resume", json={}, headers={"Origin": f"http://{host}"})
    assert r.status_code == 200


def test_post_with_invalid_json_returns_400(server):
    r = _post(f"{server}/api/resume", data="not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_get_status_reports_local_true_for_loopback_request(server):
    r = requests.get(f"{server}/api/status")
    assert r.status_code == 200
    assert r.json()["local"] is True


def test_get_root_serves_html_page(server):
    r = requests.get(f"{server}/")
    assert r.status_code == 200
    assert "text/html" in r.headers["Content-Type"]
    assert "<html" in r.text


# ---- serve() 绑定失败只记日志 ----

def test_serve_logs_and_skips_when_bind_fails(conn):
    # 端口复用在 Windows 上不可靠触发绑定失败（SO_REUSEADDR 默认允许重复绑定监听中的端口），
    # 改用一个无法解析的地址稳定触发 OSError，覆盖「绑定失败只记日志，不影响其它地址」。
    handler_cls = make_handler(lambda: conn)
    logged = []
    result = serve(["999.999.999.999"], 0, handler_cls, log=logged.append)
    assert result == []
    assert logged and "绑定失败" in logged[0]


def test_serve_binds_remaining_hosts_when_one_fails(conn):
    handler_cls = make_handler(lambda: conn)
    logged = []
    servers = serve(["999.999.999.999", "127.0.0.1"], 0, handler_cls, log=logged.append)
    try:
        assert len(servers) == 1
        assert logged
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
