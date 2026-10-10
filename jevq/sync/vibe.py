"""每日导出到 Vibe-Research（dj 的投研面板，E:\\vibe-research-agent）。

两步，都在收盘报告写完之后（daemon.py 的 after_close）：
1. 写本地文件 data/vibe/：trades.csv（实盘成交，也能在 Vibe「我的持仓」手动导入）+ docs/*.md
   （复盘报告、hi189 文章、知识库专题）。data/ 不进 git——这些内容不能进公开仓库。
2. 推进 Vibe：Vibe 的 HTTP API 已经开着就直接用；没开就临时起一个（node orchestrator/src/api.ts），
   推完关掉。实盘成交写进台账「交易流水」，文档进「研报库」。

只读我们的数据库。Vibe 那边只写交易流水和研报库，不碰它的其他台账；去重靠 note 里的
「jev:…」前缀（交易）和 state.json 里记的内容哈希（文档），所以重复跑不会重复导入。
任何异常交给调用方（daemon 只记日志），不影响交易。
"""
import base64
import csv
import hashlib
import json
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from jevq import config
from jevq.data import hi189

ACCOUNT = "实盘"
KEY_SEP = "｜"
CSV_HEADER = ["date", "symbol", "name", "type", "shares", "price", "account", "note"]
_BAD_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


# ---- 实盘成交 → 交易流水 ----

def trade_key(rec: dict) -> str | None:
    note = str(rec.get("note") or "")
    return note.split(KEY_SEP, 1)[0] if note.startswith("jev:") else None


def live_trades(conn) -> list[dict]:
    """实盘真实成交（含部分成交），按时间排序。卖出超过程序买入的部分（实盘接管前就有的持仓，
    例如 10-08 并入程序账的 2600 股雪迪龙）补一笔期初持仓，日期取这只票第一笔成交那天，
    成本按第一笔买入均价估、没有买入就按卖出价——不补的话 Vibe 重放会报「卖出超过持有」。"""
    rows = conn.execute(
        "SELECT o.id, o.ts, o.side, o.code, o.filled, o.avg_price, i.name, i.reason FROM live_orders o "
        "LEFT JOIN live_intents i ON i.id = o.intent_id "
        "WHERE o.filled > 0 AND o.status IN ('filled', 'partial') ORDER BY o.ts, o.id").fetchall()
    trades, held, first = [], {}, {}
    openings: dict[str, dict] = {}
    for oid, ts, side, code, filled, avg, name, reason in rows:
        day = ts[:10]
        first.setdefault(code, {"date": day, "buy": None, "name": name})
        if side == "buy" and first[code]["buy"] is None:
            first[code]["buy"] = avg
        first[code]["name"] = first[code]["name"] or name
        name = name or first[code]["name"]          # 卖出委托多半没有 intent，名字沿用买入那笔
        have = held.get(code, 0)
        if side == "sell" and filled > have:
            o = openings.setdefault(code, {"shares": 0, "fallback": avg})
            o["shares"] += filled - have
            have = filled
        held[code] = have + filled if side == "buy" else have - filled
        note = f"jev:live:{oid}" + (f"{KEY_SEP}{reason}" if reason else "")
        trades.append({"symbol": code, "name": name or "", "account": ACCOUNT, "type": side,
                       "date": day, "shares": filled, "price": avg, "note": note[:1000]})
    out = []
    for code, o in openings.items():
        price = first[code]["buy"] or o["fallback"]
        out.append({"symbol": code, "name": first[code]["name"] or "", "account": ACCOUNT, "type": "opening",
                    "date": first[code]["date"], "shares": o["shares"], "price": price,
                    "note": f"jev:opening:{code}{KEY_SEP}实盘接管前已有的持仓，成本按 {price:g} 估"})
    return out + trades


# ---- 文档 → 研报库 ----

def _safe(s: str) -> str:
    return _BAD_NAME.sub("_", s).strip(" ._")[:120]


def knowledge_docs(kb: dict) -> dict[str, bytes]:
    titles = {a["id"]: f"{a.get('date', '')} {a.get('title', '')}".strip() for a in kb.get("articles", [])}
    src = kb.get("source", {}).get("name", "")
    docs = {}
    for t in kb.get("topics", []):
        lines = [f"# {t['name']}", "", f"来源：{src}（Jev 知识库专题）", "", t.get("intro", ""), ""]
        for sec in t.get("sections", []):
            lines += [f"## {sec['title']}", ""]
            for p in sec.get("points", []):
                refs = "；".join(titles[r] for r in p.get("refs", []) if r in titles)
                lines.append(f"- {p['text']}" + (f"（出处：{refs}）" if refs else ""))
            lines.append("")
        docs[f"Jev知识库_{_safe(t['name'])}.md"] = "\n".join(lines).encode("utf-8")
    return docs


def collect_docs(conn, report_dir, kb: dict | None) -> dict[str, bytes]:
    docs = {}
    for p in sorted(Path(report_dir).glob("????-??-??.md")):
        docs[f"Jev复盘_{p.stem}.md"] = p.read_bytes()
    for (d,) in conn.execute("SELECT DISTINCT date FROM day_cache WHERE kind=? ORDER BY date", (hi189.KIND,)):
        for a in hi189.cached(conn, date.fromisoformat(d)):
            body = f"# {a['title']}\n\n发布：{a.get('published', '')}\n原文：{a.get('link', '')}\n\n{a.get('text', '')}\n"
            docs[f"hi189_{d}_{_safe(a['title'])}.md"] = body.encode("utf-8")
    if kb:
        docs.update(knowledge_docs(kb))
    return docs


def load_kb() -> dict | None:
    p = Path(__file__).resolve().parents[1] / "app" / "knowledge.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ---- 本地文件 ----

def write_local(out_dir, trades: list[dict], docs: dict[str, bytes]) -> list[Path]:
    out = Path(out_dir)
    (out / "docs").mkdir(parents=True, exist_ok=True)
    p = out / "trades.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as f:      # 带 BOM：Excel 打开不乱码
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for t in trades:
            w.writerow([t.get(k, "") for k in CSV_HEADER])
    for name, content in docs.items():
        (out / "docs" / name).write_bytes(content)
    return [p, out / "docs"]


# ---- Vibe API ----

class VibeClient:
    def __init__(self, base: str, token: str, session=None, timeout: float = 60):
        import requests
        self.base, self.timeout = base.rstrip("/"), timeout
        self.session = session or requests.Session()
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body=None):
        r = self.session.request(method, self.base + path, headers=self.headers, json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def health(self) -> bool:
        return bool(self._call("GET", "/health").get("ok"))

    def list_trades(self) -> list[dict]:
        return self._call("GET", "/ledger/trade").get("trade", [])

    def add_trade(self, rec: dict):
        return self._call("POST", "/ledger/trade", {"record": rec})

    def list_reports(self) -> list[dict]:
        return self._call("GET", "/reports")

    def add_report(self, name: str, content: bytes) -> dict:
        return self._call("POST", "/reports", {"name": name, "content": base64.b64encode(content).decode()})

    def delete_report(self, rid: str):
        return self._call("POST", f"/reports/{rid}/delete", {})


def push(client, trades: list[dict], docs: dict[str, bytes], state: dict) -> dict:
    """把还没进 Vibe 的成交和变了的文档推过去，state（docs: 名字 → {id, sha}）就地更新。"""
    have = {k for t in client.list_trades() if (k := trade_key(t))}
    n_trades = 0
    for t in trades:                     # 顺序要保留：同一天 Vibe 按写入先后重放（期初持仓在最前）
        if trade_key(t) not in have:
            client.add_trade(t)
            n_trades += 1
    known = state.setdefault("docs", {})
    reports = client.list_reports()
    ids = {r["id"] for r in reports}
    n_docs = 0
    for name, content in docs.items():
        sha = hashlib.sha256(content).hexdigest()
        prev = known.get(name)
        if prev and prev["sha"] == sha and prev["id"] in ids:
            continue
        rec = client.add_report(name, content)
        for r in reports:                # 同名旧版本删掉，研报库里一个名字只留最新一份
            if r["name"] == name and r["id"] != rec["id"]:
                client.delete_report(r["id"])
        reports = [r for r in reports if r["name"] != name] + [rec]
        ids = {r["id"] for r in reports}
        known[name] = {"id": rec["id"], "sha": sha}
        n_docs += 1
    return {"trades": n_trades, "docs": n_docs}


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


@contextmanager
def api_client(vibe_dir, port: int = config.VIBE_PORT, wait: float = config.VIBE_START_TIMEOUT):
    """Vibe API 开着就用；没开就临时起一个，用完关掉。token 是 Vibe 自己生成的 .local/api.token。"""
    vibe_dir = Path(vibe_dir)
    token_file = vibe_dir / ".local" / "api.token"
    proc = None
    if not _port_open(port):
        kwargs: dict = {"stdin": subprocess.DEVNULL, "cwd": vibe_dir}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        log = open(Path(config.VIBE_OUT_DIR) / "api.log", "a", encoding="utf-8")
        proc = subprocess.Popen(["node", "orchestrator/src/api.ts", "--port", str(port), "--host", "127.0.0.1"],
                                stdout=log, stderr=log, **kwargs)
    try:
        deadline = time.monotonic() + wait
        client = None
        while time.monotonic() < deadline:
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(f"Vibe API 启动失败，退出码 {proc.returncode}（见 {config.VIBE_OUT_DIR}/api.log）")
            if token_file.exists() and _port_open(port):
                try:
                    client = VibeClient(f"http://127.0.0.1:{port}", token_file.read_text(encoding="utf-8").strip())
                    if client.health():
                        break
                except Exception:
                    client = None
            time.sleep(0.5)
        if client is None:
            raise RuntimeError(f"等 Vibe API 超过 {wait:g} 秒")
        yield client
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def installed(vibe_dir) -> bool:
    d = Path(vibe_dir)
    return (d / "orchestrator" / "src" / "api.ts").exists() and (d / "orchestrator" / "node_modules").exists()


def sync_day(conn, day: date, out_dir=config.VIBE_OUT_DIR, report_dir=config.REPORT_DIR,
             vibe_dir=config.VIBE_DIR, kb: dict | None = None, client_factory=None) -> dict:
    trades = live_trades(conn)
    docs = collect_docs(conn, report_dir, kb)
    write_local(out_dir, trades, docs)
    stats = {"day": day.isoformat(), "pushed": False, "trades": 0, "docs": 0}
    if client_factory is None:
        if not installed(vibe_dir):
            return stats
        client_factory = lambda: api_client(vibe_dir)          # noqa: E731
    state_file = Path(out_dir) / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    with client_factory() as client:
        try:
            stats.update(push(client, trades, docs, state))
        finally:                         # 推到一半失败也把已推的记下来，下次不重复
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    stats["pushed"] = True
    return stats
