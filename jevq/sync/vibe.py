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
from datetime import date, timedelta
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
    例如 10-08 并入程序账的 2600 股雪迪龙）在那笔卖出前面补一笔期初持仓，日期 = 卖出那天，
    成本按此前最近一笔买入均价估、没有买入就按卖出价——不补的话 Vibe 重放会报「卖出超过持有」。
    去重标记：这只票第一次补的是 jev:opening:<代码>（10-10 已经按这个导进 Vibe，不能变），之后每次
    jev:opening:<代码>:<卖出委托 id>。"""
    rows = conn.execute(
        "SELECT o.id, o.ts, o.side, o.code, o.filled, o.avg_price, i.name, i.reason FROM live_orders o "
        "LEFT JOIN live_intents i ON i.id = o.intent_id "
        "WHERE o.filled > 0 AND o.status IN ('filled', 'partial') ORDER BY o.ts, o.id").fetchall()
    trades, held, names, last_buy, n_open = [], {}, {}, {}, {}
    for oid, ts, side, code, filled, avg, name, reason in rows:
        day = ts[:10]
        names[code] = names.get(code) or name
        name = name or names[code]                  # 卖出委托多半没有 intent，名字沿用买入那笔
        have = held.get(code, 0)
        if side == "sell" and filled > have:
            price = last_buy.get(code) or avg
            k = n_open.get(code, 0)
            n_open[code] = k + 1
            trades.append({"symbol": code, "name": name or "", "account": ACCOUNT, "type": "opening", "date": day,
                           "shares": filled - have, "price": price,
                           "note": f"jev:opening:{code}{'' if k == 0 else f':{oid}'}{KEY_SEP}"
                                   f"实盘接管前已有的持仓，成本按 {price:g} 估"})
            have = filled
        if side == "buy":
            last_buy[code] = avg
        held[code] = have + filled if side == "buy" else have - filled
        note = f"jev:live:{oid}" + (f"{KEY_SEP}{reason}" if reason else "")
        trades.append({"symbol": code, "name": name or "", "account": ACCOUNT, "type": side,
                       "date": day, "shares": filled, "price": avg, "note": note[:1000]})
    return trades


# ---- 实盘持仓 → 论点 + 判据（dj 10-10 要：Vibe 事件日历按到期日提醒，手机上看得到为什么买、什么时候必须走）----

def hold_days_of(source: str) -> int:
    """买入后第几个交易日必须清仓（和各主账户卖出规则一致）：超跌、低吸按 *_MAIN_MAX_HOLD_DAYS，扫首板次日。"""
    if source == "rebound":                  # 实盘跟的是主账户
        return config.REBOUND_MAIN_MAX_HOLD_DAYS
    return config.LOWBUY_MAIN_MAX_HOLD_DAYS if source == "lowbuy" else 1


def stop_text(source: str) -> str:
    from jevq.rules import board_sell
    if source == "board":
        return (f"次日开盘 {board_sell.LOW_OPEN:g}%~+{board_sell.WEAK_OPEN_HIGH:g}%：竞价不及预期，开盘卖；"
                f"开盘 ≤ {board_sell.LOW_OPEN:g}%：反抽 {board_sell.LOW_OPEN_BOUNCE:g} 点或 "
                f"{board_sell.LOW_OPEN_DEADLINE:%H:%M} 卖，跌到 {board_sell.HARD_STOP:g}% 硬止损；"
                f"高开 > +{board_sell.WEAK_OPEN_HIGH:g}%：跌到 {board_sell.STOP:g}% 止损、炸板卖（相对昨收）")
    if source == "lowbuy":
        return f"现价或开盘跌到 {config.LOWBUY_STOP:g}%（相对昨收）止损"
    return (f"现价连续 {config.REBOUND_STOP_CONFIRM} 分钟在买入价 {config.REBOUND_MAIN_STOP:g}% 以下止损"
            f"（Jev 判探底回升/低开高走可先留着）；跌到 {config.REBOUND_HARD_STOP:g}% 硬止损")


def nth_trading_day(d: date, n: int, calendar: list[date] | None) -> date:
    days = sorted(x for x in (calendar or ()) if x > d)
    if len(days) >= n:
        return days[n - 1]
    cur, left = d, n                     # 日历不够或没有：按工作日（不认节假日，只会偏早，不会偏晚）
    while left:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            left -= 1
    return cur


def live_holdings(conn) -> list[dict]:
    out = []
    for code, shares, buy_price, buy_date in conn.execute(
            "SELECT code, shares, buy_price, buy_date FROM live_positions WHERE shares > 0 ORDER BY code"):
        row = conn.execute(
            "SELECT i.name, i.reason FROM live_orders o LEFT JOIN live_intents i ON i.id = o.intent_id "
            "WHERE o.code=? AND o.side='buy' AND o.filled > 0 ORDER BY o.ts DESC, o.id DESC LIMIT 1", (code,)).fetchone()
        src = conn.execute("SELECT source FROM regime_sources WHERE code=?", (code,)).fetchone()
        out.append({"key": f"{code}:{buy_date}", "code": code, "name": (row and row[0]) or "", "buy_date": buy_date,
                    "buy_price": buy_price, "shares": shares, "reason": (row and row[1]) or "",
                    "source": src[0] if src else "rebound"})
    return out


def plan_records(h: dict, calendar: list[date] | None) -> tuple[dict, dict, dict]:
    sname = config.STRATEGIES.get(h["source"], {}).get("name", h["source"])
    due = nth_trading_day(date.fromisoformat(h["buy_date"]), hold_days_of(h["source"]), calendar).isoformat()
    tag = f"{KEY_SEP}Jev 自动写入（实盘跟情绪轮动）"
    thesis = {"symbol": h["code"], "title": f"{h['name'] or h['code']} 实盘｜{sname}"[:120],
              "statement": f"{h['buy_date']} 买入 {h['shares']} 股 @{h['buy_price']:g}，来源策略：{sname}。"
                           f"买入理由：{h['reason'] or '（没记）'}",
              "review_by": due, "status": "active", "note": f"jev:thesis:{h['key']}{tag}"}
    stop = {"symbol": h["code"], "type": "falsifier", "statement": stop_text(h["source"]), "status": "pending",
            "note": f"jev:stop:{h['key']}{tag}"}
    last = {"symbol": h["code"], "type": "decision_point", "due": due, "status": "pending",
            "statement": f"最晚 {due} {config.EOD_SELL:%H:%M} 清仓（{sname}持有期限）",
            "note": f"jev:due:{h['key']}{tag}"}
    return thesis, stop, last


def _sell_after(conn, code: str, buy_date: str) -> tuple[str, float, str] | None:
    row = conn.execute(
        "SELECT o.ts, o.avg_price, i.reason FROM live_orders o LEFT JOIN live_intents i ON i.id = o.intent_id "
        "WHERE o.code=? AND o.side='sell' AND o.filled > 0 AND substr(o.ts, 1, 10) >= ? "
        "ORDER BY o.ts DESC, o.id DESC LIMIT 1", (code, buy_date)).fetchone()
    return (row[0][:10], row[1], row[2] or "") if row else None


def _envelope_free(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k not in ("kind", "created_at", "updated_at")}


def sync_plans(client, conn, calendar: list[date] | None) -> dict:
    """持仓新出现 → 写论点 + 止损判据 + 到期裁决点；持仓没了 → 论点关掉、判据按卖出结果判定。
    只动 note 带「jev:」前缀的记录，dj 自己写的不碰。"""
    theses = {trade_key(t): t for t in client.list_ledger("thesis") if trade_key(t)}
    crits = {trade_key(c): c for c in client.list_ledger("criterion") if trade_key(c)}
    held = live_holdings(conn)
    opened = closed = 0
    for h in held:
        if f"jev:thesis:{h['key']}" in theses:
            continue
        thesis, stop, last = plan_records(h, calendar)
        saved = client.save("thesis", thesis)
        for c in (stop, last):
            client.save("criterion", {**c, "thesis_id": saved["id"]})
        opened += 1
    open_keys = {f"jev:thesis:{h['key']}" for h in held}
    for k, t in theses.items():
        if t.get("status") != "active" or k in open_keys:
            continue
        hkey = k.removeprefix("jev:thesis:")
        code, buy_date = hkey.split(":", 1)
        sell = _sell_after(conn, code, buy_date)
        day = sell[0] if sell else date.today().isoformat()
        why = f"{sell[0]} 卖出 @{sell[1]:g}｜{sell[2] or '没记理由'}" if sell else "实盘持仓已没有（没找到卖出委托）"
        client.save("thesis", {**_envelope_free(t), "status": "closed"})
        for prefix, status in (("jev:stop:", "met" if sell and "止损" in sell[2] else "dropped"), ("jev:due:", "met")):
            c = crits.get(prefix + hkey)
            if c and c.get("status") == "pending":
                client.save("criterion", {**_envelope_free(c), "status": status, "judged_at": day,
                                          "verdict_note": why[:1000]})
        closed += 1
    return {"opened": opened, "closed": closed}


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
        if session is None:
            session = requests.Session()
            # 只连本机：Windows 系统代理（VPN）的例外里没有 127.0.0.1，不关掉的话请求会被送进代理
            session.trust_env = False
        self.session = session
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

    def list_ledger(self, kind: str) -> list[dict]:
        return self._call("GET", f"/ledger/{kind}").get(kind, [])

    def save(self, kind: str, rec: dict) -> dict:
        """新增（不带 id）或整条覆盖（带 id）。"""
        return self._call("POST", f"/ledger/{kind}", {"record": rec})

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
    proc = log = None
    try:
        if not _port_open(port):
            kwargs: dict = {"stdin": subprocess.DEVNULL, "cwd": vibe_dir}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            Path(config.VIBE_OUT_DIR).mkdir(parents=True, exist_ok=True)
            log = open(Path(config.VIBE_OUT_DIR) / "api.log", "a", encoding="utf-8")
            proc = subprocess.Popen(["node", "orchestrator/src/api.ts", "--port", str(port), "--host", "127.0.0.1"],
                                    stdout=log, stderr=log, **kwargs)
        deadline = time.monotonic() + wait
        client = None
        while time.monotonic() < deadline:
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(f"Vibe API 启动失败，退出码 {proc.returncode}（见 {config.VIBE_OUT_DIR}/api.log）")
            if token_file.exists() and _port_open(port):
                try:
                    c = VibeClient(f"http://127.0.0.1:{port}", token_file.read_text(encoding="utf-8").strip())
                    if c.health():
                        client = c
                        break
                except Exception:
                    pass
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
        if log is not None:
            log.close()


def installed(vibe_dir) -> bool:
    d = Path(vibe_dir)
    return (d / "orchestrator" / "src" / "api.ts").exists() and (d / "orchestrator" / "node_modules").exists()


def _calendar() -> list[date] | None:
    try:
        from jevq.data import akdata
        return akdata.trade_days()
    except Exception as e:
        print(f"[vibe] 交易日历取不到，到期日按工作日算: {e!r}")
        return None


def sync_day(conn, day: date, out_dir=config.VIBE_OUT_DIR, report_dir=config.REPORT_DIR,
             vibe_dir=config.VIBE_DIR, kb: dict | None = None, client_factory=None, calendar=_calendar) -> dict:
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
            stats.update(sync_plans(client, conn, calendar() if callable(calendar) else calendar))
        finally:                         # 推到一半失败也把已推的记下来，下次不重复
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    stats["pushed"] = True
    return stats
