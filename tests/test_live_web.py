"""手机页测试：真实 ThreadingHTTPServer 绑 127.0.0.1:0 + requests 打本机回环（不算联网）。
覆盖：暂停/恢复/立即卖出写对状态、非 JSON 415、跨站 Origin 403、JSON 解析失败 400、
/api/mode 本机可改（_is_local 单测非本机返回 False）、status 不含资金字段、
每个请求自己的连接（用完关掉）、暂停的检查+写入原子、恢复要对得上页面看到的暂停、Host 白名单。
"""
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime

import pytest
import requests

from factories import quote
from jevq import config, db
from jevq.live import store as live_store
from jevq.live.store import LivePosition
from jevq.live.web import _PAGE_HTML, _hostname, _is_local, make_handler, serve, status

NOW = datetime(2026, 9, 29, 10, 0)


def _post(url, **kw):
    # conftest.py 把 jevq.notify.requests.post（同一个 requests 模块对象）打成一调用就报错，
    # 防的是真的联网推送；这里打本机回环走独立 Session().post，不经过那个被打桩的模块级函数。
    return requests.Session().post(url, **kw)


@pytest.fixture
def conn():
    return db.connect(":memory:")


@contextmanager
def running_server(conn, clock=lambda: NOW, quotes=lambda codes: {}, factory=None, **handler_kw):
    handler_cls = make_handler(factory or (lambda: conn), clock=clock, quotes=quotes, **handler_kw)
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


def test_pause_never_downgrades_an_auto_pause(server, conn):
    """自动暂停只能手动恢复；手机点「暂停 30 分钟」不能把它改成到点自动恢复的手动暂停。"""
    live_store.pause(conn, None, "撤单前核对不上：最后一笔不是程序的委托", "auto")
    r = _post(f"{server}/api/pause", json={"minutes": 30})
    assert r.status_code == 409
    assert "自动暂停" in r.json()["error"]
    p = live_store.paused(conn)
    assert p["kind"] == "auto" and p["until"] is None and "撤单前核对不上" in p["reason"]


def test_pause_rejects_bad_payload(server):
    r = _post(f"{server}/api/pause", json={"minutes": 45})
    assert r.status_code == 400


def test_resume_when_not_paused_writes_nothing(server, conn):
    """没在暂停就没有可恢复的：不写 resume_requested——不然紧接着出现的自动暂停会被这个旧请求清掉。"""
    r = _post(f"{server}/api/resume", json={})
    assert r.status_code == 200
    assert live_store.get_state(conn, "resume_requested") is None


# ---- 恢复要对得上页面看到的暂停 ----

def test_resume_with_matching_reason_sets_resume_requested(server, conn):
    live_store.pause(conn, None, "撤单前核对不上：读不到当日委托", "auto")
    r = _post(f"{server}/api/resume", json={"reason": "撤单前核对不上：读不到当日委托"})
    assert r.status_code == 200
    # 请求里记下要恢复的是哪一个暂停：执行器只在当前暂停还是它时才清
    assert live_store.get_state(conn, "resume_requested") == "撤单前核对不上：读不到当日委托"


def test_resume_with_stale_reason_is_rejected_and_keeps_the_pause(server, conn):
    """页面上看到的是「手机暂停」，点恢复时已经换成了它没看到的自动暂停：不能顺手把新的自动暂停清掉。"""
    live_store.pause(conn, None, "卖单核对不上：600001 当日委托里找不到这笔卖单", "auto")
    r = _post(f"{server}/api/resume", json={"reason": "手机暂停"})
    assert r.status_code == 409
    err = r.json()["error"]
    assert "暂停状态刚变了" in err and "卖单核对不上：600001" in err and "请看清后再点恢复" in err
    assert live_store.get_state(conn, "resume_requested") is None
    assert live_store.paused(conn)["kind"] == "auto"


def test_resume_without_reason_while_paused_is_rejected(server, conn):
    """页面显示「未暂停」（reason 是 null）时点恢复，而此刻已经暂停：同样是没看到的暂停。"""
    live_store.pause(conn, None, "连续读取失败，客户端可能掉线", "auto")
    r = _post(f"{server}/api/resume", json={"reason": None})
    assert r.status_code == 409
    assert live_store.get_state(conn, "resume_requested") is None
    r = _post(f"{server}/api/resume", json={})
    assert r.status_code == 409
    assert live_store.get_state(conn, "resume_requested") is None


def test_resume_after_the_pause_already_ended_writes_nothing(server, conn):
    r = _post(f"{server}/api/resume", json={"reason": "手机暂停"})    # 页面上看到的暂停刚好已经到点恢复了
    assert r.status_code == 200
    assert live_store.get_state(conn, "resume_requested") is None


def test_page_resume_button_sends_the_pause_reason_it_displays():
    body = _PAGE_HTML
    assert "lastPaused = d.paused" in body
    start = body.index("function doResume()")
    assert "reason" in body[start:body.index("\n", start)]


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


# ---- 每个请求自己的连接 ----

class _TrackedConns:
    """conn_factory 替身：每次开一个新的文件库连接并记下来，测试结束后检查是不是都关了。"""

    def __init__(self, path):
        self.path = path
        self.opened = []

    def __call__(self):
        c = sqlite3.connect(self.path, timeout=5, check_same_thread=False)    # 测试线程要能检查它关没关
        self.opened.append(c)
        return c

    def all_closed(self):
        for c in self.opened:
            try:
                c.execute("SELECT 1")
            except sqlite3.ProgrammingError as e:
                assert "closed" in str(e)
                continue
            return False
        return True


@pytest.fixture
def file_db(tmp_path):
    path = str(tmp_path / "jevq.sqlite")
    db.connect(path).close()        # 建表 + WAL，和 live.py 里执行器那条连接一样
    return path


def test_close_conn_gives_each_request_its_own_connection_and_closes_it(file_db):
    factory = _TrackedConns(file_db)
    with running_server(None, factory=factory, close_conn=True) as base_url:
        assert requests.get(f"{base_url}/api/status").status_code == 200
        assert _post(f"{base_url}/api/pause", json={"minutes": 30}).status_code == 200
        assert _post(f"{base_url}/api/pause", json={"minutes": 45}).status_code == 400     # 参数错误也不漏连接
        assert _post(f"{base_url}/api/resume", json={"reason": "手机暂停"}).status_code == 200
        assert _post(f"{base_url}/api/sell_all", json={}).status_code == 200
        assert _post(f"{base_url}/api/mode", json={"mode": "dryrun"}).status_code == 200
        assert _post(f"{base_url}/api/mode", json={"mode": "nope"}).status_code == 400
        assert _post(f"{base_url}/api/nope", json={}).status_code == 404
    assert len(factory.opened) >= 6
    assert len({id(c) for c in factory.opened}) == len(factory.opened)      # 每次都是新连接，没有复用
    assert factory.all_closed()


def test_close_conn_closes_connection_even_when_the_handler_raises(file_db, monkeypatch):
    from jevq.live import web

    def boom(conn, now, quotes):
        raise RuntimeError("读库炸了")

    monkeypatch.setattr(web, "status", boom)
    factory = _TrackedConns(file_db)
    with running_server(None, factory=factory, close_conn=True) as base_url:
        with pytest.raises(requests.exceptions.ConnectionError):
            requests.get(f"{base_url}/api/status")      # 处理线程异常，服务端直接断开连接
    assert len(factory.opened) == 1 and factory.all_closed()


def test_shared_connection_is_not_closed_by_default(conn):
    with running_server(conn) as base_url:
        assert requests.get(f"{base_url}/api/status").status_code == 200
        assert _post(f"{base_url}/api/resume", json={}).status_code == 200
    conn.execute("SELECT 1")     # 没被关：注入共享连接的测试还能继续用它


def test_pause_writes_are_visible_to_another_connection_on_a_file_db(file_db):
    factory = _TrackedConns(file_db)
    with running_server(None, factory=factory, close_conn=True) as base_url:
        assert _post(f"{base_url}/api/pause", json={"minutes": 30}).status_code == 200
    other = sqlite3.connect(file_db)
    p = live_store.paused(other)
    assert p["kind"] == "manual" and p["reason"] == "手机暂停" and p["until"] == NOW.replace(minute=30)
    other.close()


def test_pause_check_and_write_are_atomic_against_executor_auto_pause(file_db, monkeypatch):
    """页面检查完「不是自动暂停」之后、写入之前，执行器自动暂停：不能被页面的手动暂停覆盖，最终仍是 auto。"""
    executor_conn = sqlite3.connect(file_db, timeout=5, check_same_thread=False)
    check_conn = sqlite3.connect(file_db, timeout=5, check_same_thread=False)
    real_paused = live_store.paused
    seen = {}

    def paused_hook(c):
        result = real_paused(c)
        if "thread" not in seen:
            seen["thread"] = threading.Thread(
                target=lambda: live_store.pause(executor_conn, None, "执行器自动暂停", "auto"))
            seen["thread"].start()
            time.sleep(0.3)
            seen["during"] = real_paused(check_conn)
        return result

    monkeypatch.setattr(live_store, "paused", paused_hook)
    factory = _TrackedConns(file_db)
    with running_server(None, factory=factory, close_conn=True) as base_url:
        r = _post(f"{base_url}/api/pause", json={"minutes": 30})
    seen["thread"].join(timeout=10)
    monkeypatch.undo()

    assert r.status_code == 200
    assert seen["during"] is None       # 页面持有写锁期间，执行器的自动暂停还没落库
    final = live_store.paused(check_conn)
    assert final["kind"] == "auto" and final["reason"] == "执行器自动暂停" and final["until"] is None


# ---- Host 白名单（防 DNS rebinding）----

def test_hostname_strips_port_and_lowercases():
    assert _hostname("127.0.0.1:8766") == "127.0.0.1"
    assert _hostname("localhost") == "localhost"
    assert _hostname("LocalHost:8766") == "localhost"
    assert _hostname("100.64.1.2:8766") == "100.64.1.2"
    assert _hostname("[::1]:8766") == "::1"
    assert _hostname("") == ""


@pytest.fixture
def guarded(conn):
    with running_server(conn, allowed_hosts={"127.0.0.1", "localhost", "100.64.1.2"}) as base_url:
        yield base_url


BAD_HOSTS = ["evil.example.com", "evil.example.com:8766", "127.0.0.1.evil.com:8766",
             "localhost.evil.com", "100.64.1.3:8766"]
GOOD_HOSTS = ["127.0.0.1:8766", "localhost:8766", "LOCALHOST", "100.64.1.2:8766"]


def test_host_not_in_whitelist_is_rejected_for_every_method(guarded, conn):
    for host in BAD_HOSTS:
        h = {"Host": host}
        assert requests.get(f"{guarded}/", headers=h).status_code == 403, host
        assert requests.get(f"{guarded}/api/status", headers=h).status_code == 403, host
        assert requests.get(f"{guarded}/nope", headers=h).status_code == 403, host
        assert _post(f"{guarded}/api/resume", json={}, headers=h).status_code == 403, host
        assert _post(f"{guarded}/api/mode", json={"mode": "dryrun"}, headers=h).status_code == 403, host
        assert requests.put(f"{guarded}/api/resume", json={}, headers=h).status_code == 403, host
        assert requests.delete(f"{guarded}/api/resume", headers=h).status_code == 403, host
        assert requests.head(f"{guarded}/", headers=h).status_code == 403, host
    assert live_store.get_state(conn, "resume_requested") is None      # 被拒的请求什么都没写
    assert live_store.mode(conn) == "off"


def test_host_in_whitelist_is_served(guarded, conn):
    for host in GOOD_HOSTS:
        h = {"Host": host}
        assert requests.get(f"{guarded}/", headers=h).status_code == 200, host
        assert requests.get(f"{guarded}/api/status", headers=h).status_code == 200, host
        conn.execute("DELETE FROM live_state WHERE key='resume_requested'")
        conn.commit()
        live_store.pause(conn, None, "手机暂停", "manual")
        r = _post(f"{guarded}/api/resume", json={"reason": "手机暂停"}, headers={**h, "Origin": f"http://{host}"})
        assert r.status_code == 200, host
        assert live_store.get_state(conn, "resume_requested") == "手机暂停"


def test_rejected_host_gets_json_403_body(guarded):
    r = requests.get(f"{guarded}/api/status", headers={"Host": "evil.example.com"})
    assert r.status_code == 403
    assert "error" in r.json()


def test_no_whitelist_by_default_accepts_any_host(server):
    assert requests.get(f"{server}/api/status", headers={"Host": "evil.example.com"}).status_code == 200


def test_serve_keeps_retrying_a_host_that_is_not_up_yet_and_binds_it_later(monkeypatch):
    """开机时 Tailscale 地址可能还没起来（2026-10-07 实际发生：绑定失败后手机页再也没起来），
    绑定失败的地址要在后台一直重试，起来后补进 servers；失败只记一次，不是每次重试都记。"""
    from jevq.live import web
    attempts = []

    class FlakyServer:
        def __init__(self, addr, handler):
            attempts.append(addr)
            if len(attempts) < 4:
                raise OSError(10049, "address not valid")
            self.server_address = addr

        def serve_forever(self):
            pass

    monkeypatch.setattr(web, "ThreadingHTTPServer", FlakyServer)
    logs = []
    servers = serve(["100.1.1.1"], 8766, object, log=logs.append, retry_seconds=0.01)
    assert servers == [] and logs and "绑定失败" in logs[0] and "100.1.1.1:8766" in logs[0]
    deadline = time.time() + 5
    while not servers and time.time() < deadline:
        time.sleep(0.01)
    assert [x.server_address for x in servers] == [("100.1.1.1", 8766)] and len(attempts) == 4
    assert sum("绑定失败" in m for m in logs) == 1
    assert any("已绑定" in m and "100.1.1.1:8766" in m for m in logs)


def test_serve_binds_good_hosts_immediately_even_when_another_host_is_down(monkeypatch):
    from jevq.live import web

    class Server:
        def __init__(self, addr, handler):
            if addr[0] == "100.9.9.9":
                raise OSError(10049, "address not valid")
            self.server_address = addr

        def serve_forever(self):
            pass

    monkeypatch.setattr(web, "ThreadingHTTPServer", Server)
    servers = serve(["127.0.0.1", "100.9.9.9"], 8766, object, log=lambda m: None, retry_seconds=60)
    assert [x.server_address[0] for x in servers] == ["127.0.0.1"]
