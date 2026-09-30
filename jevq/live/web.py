"""手机页：live.py 内置的只读状态 + 控制页面。只通过 jevq.live.store 读写数据库，绝不碰 broker
（页面线程永远不该摸真实账户——broker 只在 Executor 那一侧调用）。

- GET / ：内嵌 HTML，手机宽度可用（≤400px，16px 边距），纯内联 JS，每 10 秒轮询一次状态。
- GET /api/status ：同一份状态数据的 JSON。
- POST /api/pause /api/resume /api/sell_all /api/mode ：控制接口，见 status()/make_handler() 注释。
"""
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from jevq import config
from jevq.data import tencent
from jevq.ledger import Ledger
from jevq.live import store as live_store


def _is_local(addr: str) -> bool:
    return addr == "127.0.0.1"


def _origin_matches_host(origin: str, host: str) -> bool:
    try:
        return urlparse(origin).netloc == host
    except ValueError:
        return False


def _hostname(host_header: str) -> str:
    """Host 请求头去掉端口后的主机名，小写；[::1]:8766 这种带方括号的 IPv6 也能取对。"""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end != -1 else h
    return h.rsplit(":", 1)[0] if ":" in h else h


def status(conn, now: datetime, quotes) -> dict:
    """页面数据：不含账户资金、不含用户自己的持仓——只有归程序管的 live_positions 和只读汇总。"""
    positions = live_store.positions(conn)
    try:
        quote_map = quotes(list(positions)) if positions else {}
    except Exception:
        quote_map = {}   # 行情取不到就不显示现价，不让整页失败（决策者决定）
    pos_list = []
    for code, p in positions.items():
        q = quote_map.get(code)
        price = q.price if q is not None else None
        pnl_pct = round((price / p.buy_price - 1) * 100, 2) if price is not None and p.buy_price else None
        pos_list.append({"code": code, "shares": p.shares, "buy_price": p.buy_price,
                          "price": price, "pnl_pct": pnl_pct})

    ledger = Ledger(conn, init=False)
    to_sell = [code for code in positions if not ledger.holds(config.LIVE_ACCOUNT, code)]

    pause = live_store.paused(conn)
    today = now.date()
    return {
        "mode": live_store.mode(conn),
        "paused": None if pause is None else {
            "until": pause["until"].isoformat() if pause["until"] else None,
            "reason": pause["reason"], "kind": pause["kind"]},
        "last_read_ok": live_store.get_state(conn, "last_read_ok"),
        "positions": pos_list,
        "to_sell": to_sell,
        "orders": live_store.orders_on(conn, today),
        "skipped": [i for i in live_store.intents_on(conn, today) if i["status"] == "skipped"],
    }


def _pause_until(payload: dict, now: datetime):
    close = datetime.combine(now.date(), config.LIVE_PAUSE_CLOSE)
    if payload.get("until") == "close":
        return close
    minutes = payload.get("minutes")
    if minutes in (30, 60):
        return min(now + timedelta(minutes=minutes), close)
    return None


def make_handler(conn_factory, clock=datetime.now, quotes=tencent.fetch_quotes,
                 close_conn: bool = False, allowed_hosts=None):
    """conn_factory()：每个请求调用一次，拿到这个请求自己用的数据库连接。
    close_conn=True（live.py 用）：conn_factory 每次返回新连接，请求处理完关掉它——页面线程绝不和执行器
    共用同一个 sqlite 连接（并发写会直接报错，还会把写了一半的状态一起提交）；WAL 下各连接互不阻塞。
    close_conn=False（默认，测试注入共享内存连接用）：不关，连接归调用方管。
    allowed_hosts：不为 None 时，请求 Host 头去掉端口后的主机名必须在这个集合里，否则任何方法一律 403，
    防 DNS rebinding（攻击网页把自己的域名解析到本机或 Tailscale 地址，Origin 和 Host 就一致了）。"""
    allowed = None if allowed_hosts is None else {h.strip().lower() for h in allowed_hosts}

    @contextmanager
    def request_conn():
        conn = conn_factory()
        try:
            yield conn
        finally:
            if close_conn:
                try:
                    conn.close()
                except Exception:
                    pass

    class Handler(BaseHTTPRequestHandler):
        server_version = "JevLiveWeb/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass   # 不往 stderr 打访问日志：pythonw 下没处可写，也没必要

        def parse_request(self):
            """请求行和头都解析完、分发到 do_XXX 之前先验 Host：这样任何方法（含没实现的 PUT/DELETE）都被拒。"""
            if not super().parse_request():
                return False
            if allowed is not None and _hostname(self.headers.get("Host", "")) not in allowed:
                self._json_body(403, {"error": "Host 不在白名单"}, close=True)
                return False
            return True

        def _json_body(self, code: int, payload: dict, close: bool = False) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _html_body(self, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---- GET ----

        def do_GET(self):
            if self.path == "/":
                self._html_body(_PAGE_HTML)
                return
            if self.path == "/api/status":
                with request_conn() as conn:
                    data = status(conn, clock(), quotes)
                data["local"] = _is_local(self.client_address[0])
                self._json_body(200, data)
                return
            self._json_body(404, {"error": "not found"})

        # ---- POST ----

        def _read_json(self):
            """校验 Content-Type / 跨站 Origin / JSON 语法；任何一步失败自己写好响应并返回 (None, False)。"""
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip()
            if ctype != "application/json":
                self._json_body(415, {"error": "需要 application/json"})
                return None, False
            origin = self.headers.get("Origin")
            if origin and not _origin_matches_host(origin, self.headers.get("Host", "")):
                self._json_body(403, {"error": "跨站请求被拒绝"})
                return None, False
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                self._json_body(400, {"error": "JSON 解析失败"})
                return None, False
            if not isinstance(payload, dict):
                self._json_body(400, {"error": "参数错误"})
                return None, False
            return payload, True

        def do_POST(self):
            payload, ok = self._read_json()
            if not ok:
                return
            now = clock()
            with request_conn() as conn:
                self._dispatch_post(conn, now, payload)

        def _dispatch_post(self, conn, now: datetime, payload: dict) -> None:
            if self.path == "/api/pause":
                until = _pause_until(payload, now)
                if until is None:
                    self._json_body(400, {"error": "参数错误"})
                    return
                current = live_store.pause_unless_auto(conn, until, "手机暂停")
                if current is not None:
                    # 自动暂停只能手动恢复：不许被改成到点自动恢复的手动暂停（检查和写入在同一个事务里）
                    self._json_body(409, {"error": f"已自动暂停（{current['reason']}），先在 App 里核对，再点恢复"})
                    return
                self._json_body(200, {"ok": True, "until": until.isoformat()})
            elif self.path == "/api/resume":
                current = live_store.paused(conn)
                if current is not None and (payload.get("reason") or "") != (current["reason"] or ""):
                    # 页面上看到的暂停和现在的不是同一个：这是用户没看到的新暂停，不能顺手清掉
                    self._json_body(409, {"error": f"暂停状态刚变了：{current['reason']}，请看清后再点恢复"})
                    return
                live_store.set_state(conn, "resume_requested", "1")
                self._json_body(200, {"ok": True})
            elif self.path == "/api/sell_all":
                live_store.add_remote_sell(conn, now)
                self._json_body(200, {"ok": True})
            elif self.path == "/api/mode":
                if not _is_local(self.client_address[0]):
                    self._json_body(403, {"error": "只能在本机切换模式"})
                    return
                try:
                    live_store.set_mode(conn, payload.get("mode"))
                except ValueError as e:
                    self._json_body(400, {"error": str(e)})
                    return
                self._json_body(200, {"ok": True})
            else:
                self._json_body(404, {"error": "not found"})

    return Handler


def serve(hosts: list[str], port: int, handler_cls, log=print) -> list[ThreadingHTTPServer]:
    """每个地址开一个 ThreadingHTTPServer + 一个 daemon 线程；绑定失败只记日志，不影响其它地址。"""
    servers = []
    for host in hosts:
        try:
            server = ThreadingHTTPServer((host, port), handler_cls)
        except OSError as e:
            log(f"手机页绑定失败 {host}:{port}：{e!r}")
            continue
        threading.Thread(target=server.serve_forever, daemon=True, name=f"live-web-{host}").start()
        servers.append(server)
    return servers


_PAGE_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jev 实盘</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0 auto; padding: 16px; max-width: 400px;
         font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         font-size: 15px; background: #f5f5f7; color: #1c1c1e; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  h2 { font-size: 14px; margin: 16px 0 6px; color: #666; }
  .card { background: #fff; border-radius: 10px; padding: 12px; margin-bottom: 10px; }
  .row { display: flex; justify-content: space-between; padding: 4px 0; font-size: 14px; }
  .muted { color: #888; }
  button { width: 100%; padding: 10px; margin: 4px 0; border: none; border-radius: 8px;
           font-size: 15px; background: #007aff; color: #fff; }
  button.danger { background: #ff3b30; }
  button.ghost { background: #e5e5ea; color: #1c1c1e; }
  .btnrow { display: flex; gap: 8px; }
  .btnrow button { flex: 1; }
  .up { color: #d9363e; }
  .down { color: #389e0d; }
  .empty { color: #999; font-size: 13px; padding: 4px 0; }
</style>
</head>
<body>
<h1>Jev 实盘</h1>
<div class="card" id="status"></div>
<div class="btnrow">
  <button onclick="doPause(30)">暂停30分</button>
  <button onclick="doPause(60)">暂停1小时</button>
  <button onclick="doPauseClose()">暂停到收盘</button>
</div>
<button class="ghost" onclick="doResume()">恢复</button>
<button class="danger" onclick="doSellAll()">立即卖出</button>
<div id="modeBox" style="display:none">
  <h2>模式（本机）</h2>
  <div class="btnrow">
    <button class="ghost" onclick="doMode('off')">关闭</button>
    <button class="ghost" onclick="doMode('dryrun')">演练</button>
    <button class="ghost" onclick="doMode('live')">实盘</button>
  </div>
</div>
<h2>持仓</h2>
<div class="card" id="positions"></div>
<h2>待卖</h2>
<div class="card" id="toSell"></div>
<h2>今天委托</h2>
<div class="card" id="orders"></div>
<h2>今天被跳过</h2>
<div class="card" id="skipped"></div>
<script>
function esc(s){
  return String(s).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
function fmtTime(s){ return s ? s.replace('T', ' ').slice(0, 19) : (Number.isInteger(s) ? s : '—'); }

var lastPaused = null;   // 页面上当前显示的暂停（点恢复时把它的原因带给服务端，对不上就拒绝）

async function refresh(){
  var d;
  try {
    var r = await fetch('/api/status');
    d = await r.json();
  } catch (e) { return; }
  render(d);
}

function render(d){
  lastPaused = d.paused || null;
  var modeNames = {off: '关闭', dryrun: '演练', live: '实盘'};
  var s = '<div class="row"><span>模式</span><span>' + esc(modeNames[d.mode] || d.mode) + '</span></div>';
  if (d.paused) {
    var until = d.paused.until ? fmtTime(d.paused.until) : '手动恢复';
    s += '<div class="row"><span>暂停中</span><span>到 ' + esc(until) + '</span></div>';
    s += '<div class="row muted"><span>原因</span><span>' + esc(d.paused.reason || '') + '</span></div>';
  } else {
    s += '<div class="row"><span>暂停</span><span>否</span></div>';
  }
  s += '<div class="row muted"><span>客户端最后读成功</span><span>' + fmtTime(d.last_read_ok) + '</span></div>';
  document.getElementById('status').innerHTML = s;

  document.getElementById('modeBox').style.display = d.local ? 'block' : 'none';

  var positions = document.getElementById('positions');
  positions.innerHTML = !d.positions.length ? '<div class="empty">无</div>' : d.positions.map(function(p){
    var pnl = p.pnl_pct == null ? '—' : (p.pnl_pct >= 0 ? '+' : '') + p.pnl_pct + '%';
    var cls = p.pnl_pct == null ? '' : (p.pnl_pct >= 0 ? 'up' : 'down');
    return '<div class="row"><span>' + esc(p.code) + ' ' + p.shares + '股</span>' +
           '<span class="' + cls + '">成本 ' + p.buy_price + ' 现价 ' +
           (p.price == null ? '—' : p.price) + ' ' + pnl + '</span></div>';
  }).join('');

  var toSell = document.getElementById('toSell');
  toSell.innerHTML = d.to_sell.length ? esc(d.to_sell.join('、')) : '<span class="empty">无</span>';

  var orders = document.getElementById('orders');
  orders.innerHTML = !d.orders.length ? '<div class="empty">无</div>' : d.orders.map(function(o){
    return '<div class="row"><span>' + esc(o.code) + ' ' + (o.side === 'buy' ? '买' : '卖') + '</span>' +
           '<span>' + o.shares + '股@' + o.price + ' ' + esc(o.status) + '</span></div>';
  }).join('');

  var skipped = document.getElementById('skipped');
  skipped.innerHTML = !d.skipped.length ? '<div class="empty">无</div>' : d.skipped.map(function(i){
    return '<div class="row"><span>' + esc(i.code) + '</span><span class="muted">' +
           esc(i.note || i.reason || '') + '</span></div>';
  }).join('');
}

async function post(path, body){
  try {
    var r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(body || {})});
    if (!r.ok) { var d = await r.json().catch(function(){ return {}; }); alert('操作失败：' + (d.error || r.status)); }
  } catch (e) { alert('网络错误'); }
  refresh();
}
function doPause(minutes){ post('/api/pause', {minutes: minutes}); }
function doPauseClose(){ post('/api/pause', {until: 'close'}); }
function doResume(){ post('/api/resume', {reason: lastPaused ? lastPaused.reason : null}); }
function doSellAll(){ if (confirm('确定立即卖出程序全部持仓？')) post('/api/sell_all', {}); }
function doMode(m){ if (confirm('确定切换模式到 ' + m + '？')) post('/api/mode', {mode: m}); }

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""
