"""面板验收预览（开发用，python scripts/panel_preview.py 后打开 http://127.0.0.1:8765）：用 HTTP 把 ui.html 接到真实 Api（真实数据库，只读），写操作一律拦截。"""
import json
import os
import sys
from datetime import date, datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(r"D:\claude\jev-quant")
os.chdir(REPO)
sys.path.insert(0, str(REPO))
from jevq.app.api import Api  # noqa: E402

BLOCKED = {"start", "stop", "rule_create", "rule_update", "rule_disable"}
api = Api(runner=lambda **k: None)

STUB = """<script>
window.pywebview = {api: new Proxy({}, {get: (_, name) => (...args) =>
  fetch('/api/' + name, {method: 'POST', body: JSON.stringify(args)}).then(r => r.json())})};
window.addEventListener('load', () => window.dispatchEvent(new Event('pywebviewready')));
</script>"""


def default(o):
    if isinstance(o, (date, datetime, time)):
        return o.isoformat()
    return str(o)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        html = (REPO / "jevq/app/ui.html").read_text(encoding="utf-8")
        html = html.replace("<head>", "<head>" + STUB, 1)
        self._send(html.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        name = self.path.rsplit("/", 1)[-1]
        args = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or "[]")
        if name in BLOCKED:
            out = {"errors": ["预览模式：不保存、不启停"], "ok": False}
        else:
            try:
                out = getattr(api, name)(*args)
            except Exception as e:  # 预览里暴露出来，便于发现跨桥抛异常
                out = {"__exception__": repr(e)}
        self._send(json.dumps(out, ensure_ascii=False, default=default).encode("utf-8"), "application/json")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()
