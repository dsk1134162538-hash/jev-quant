"""行情终端页的数据（dj 2026-11-01：要实时数据、少写字）。只读：jevq.sqlite 用 mode=ro 打开，网关只问 /health。

一次返回一屏要看的全部数字：指数、市场宽度、各账户净值/今日/持仓（持仓按秒级行情估值）、今日成交、净值走势、
实盘与网关状态、AI 最近一次判断。休市时宽度退回最近一个收盘日的统计，并标出日期。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from jevq import config
from jevq.data import candidates
from jevq.models import Position
from jevq.rules.ai_board import breakeven_price

ACCOUNTS = ("regime_main", "rebound_main", "lowbuy_main", "board_main",
            "rebound_shadow", "lowbuy_shadow", "board_shadow")
MAIN_ACCOUNTS = ACCOUNTS[:4]
AI_ACCOUNTS = tuple(config.STRATEGIES["aiboard"]["accounts"])   # AI 打首板，单独放在右栏
INDEX_CODES = {"000001", "399001", "399006"}     # 和个股代码会撞（平安银行 000001），持仓里遇到就不进秒级通道
GATEWAY_URL = "http://127.0.0.1:5000/health"
CURVE_DAYS = 30


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


class TerminalData:
    def __init__(self, db_path: Path | None, runtime_dir: Path, gateway_url: str = GATEWAY_URL, session=None):
        self.db_path = db_path
        self.runtime_dir = Path(runtime_dir)
        self.gateway_url = gateway_url
        self.session = session or requests.Session()
        self.session.trust_env = False
        self._lock = threading.Lock()
        self._day_cache: dict = {}             # 当天不变的东西（5 日中位数、收盘宽度、净值曲线）按日期缓存
        self._gateway: tuple[float, dict] | None = None
        self._held: tuple[float, list[str]] | None = None

    # ---- 秒级通道要跟的持仓代码 ----
    def held_codes(self) -> list[str]:
        now = time.monotonic()
        if self._held and now - self._held[0] < 10:
            return self._held[1]
        codes: list[str] = []
        if self.db_path and Path(self.db_path).exists():
            try:
                conn = _ro(self.db_path)
                try:
                    accts = ACCOUNTS + AI_ACCOUNTS
                    marks = ",".join("?" * len(accts))
                    codes = [r[0] for r in conn.execute(
                        f"SELECT DISTINCT code FROM positions WHERE account IN ({marks}) AND shares > 0", accts)]
                    codes += [r[0] for r in conn.execute("SELECT code FROM live_positions")]
                finally:
                    conn.close()
            except sqlite3.Error:
                codes = []
        codes = [c for c in dict.fromkeys(codes) if c not in INDEX_CODES]
        self._held = (now, codes)
        return codes

    # ---- 网关：只问 /health，15 秒缓存 ----
    def gateway(self) -> dict:
        now = time.monotonic()
        if self._gateway and now - self._gateway[0] < 15:
            return self._gateway[1]
        try:
            d = self.session.get(self.gateway_url, timeout=1).json().get("data") or {}
            state = {"ok": bool(d.get("logged_in")) and bool(d.get("xiadan_running")),
                     "text": "在线" if d.get("logged_in") else "同花顺没登录" if d.get("xiadan_running") else "下单程序没开"}
        except Exception:
            state = {"ok": False, "text": "连不上"}
        self._gateway = (now, state)
        return state

    # ---- 当天不变的统计 ----
    def _daily(self, conn: sqlite3.Connection, today: date) -> dict:
        key = today.isoformat()
        if key in self._day_cache:
            return self._day_cache[key]
        dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT 6",
                                            (key,))]
        out: dict = {"ret5": None, "close": None}
        if len(dates) >= 2:
            last, prev = dates[0], dates[1]
            px = {}
            for code, d, close, pre in conn.execute(
                    "SELECT code, date, close, pre_close FROM closes WHERE date IN (?, ?) AND close > 0", (last, prev)):
                px.setdefault(code, {})[d] = (close, pre)
            up = down = lu = ld = 0
            for s in px.values():
                if last not in s:
                    continue
                base = s[last][1] or (s[prev][0] if prev in s else None)
                if not base:
                    continue
                pct = s[last][0] / base - 1
                up += pct > 0
                down += pct < 0
                lu += pct >= 0.095
                ld += pct <= -0.095
            out["close"] = {"date": last, "up": up, "down": down, "limitUp": lu, "limitDown": ld}
        if len(dates) >= 6:
            from jevq.data.closes import ret5_from_db
            codes = [r[0] for r in conn.execute("SELECT code FROM closes WHERE date=?", (dates[0],))]
            r5 = ret5_from_db(conn, codes, today)
            if r5:
                out["ret5"] = round(statistics.median(r5.values()), 2)
        curves = {}
        for acct in ACCOUNTS + AI_ACCOUNTS:
            rows = conn.execute("SELECT date, value FROM equity WHERE account=? AND date < ? ORDER BY date DESC LIMIT ?",
                                (acct, key, CURVE_DAYS)).fetchall()
            curves[acct] = [{"d": r[0], "v": round(r[1], 2)} for r in reversed(rows)]
        out["curves"] = curves
        self._day_cache = {key: out}
        return out

    def _sentiment(self, conn: sqlite3.Connection) -> dict | None:
        row = conn.execute("SELECT ts, detail FROM decisions WHERE verdict='sentiment' ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        d = json.loads(row[1] or "{}")
        s = d.get("stats") or {}
        return {"ts": row[0], "label": d.get("label"), "limitUp": s.get("涨停家数"), "limitDown": s.get("跌停家数"),
                "broken": s.get("炸板率%")}

    def _ai_last(self) -> dict | None:
        log = self.runtime_dir / "ai_loop.log"
        if not log.exists():
            return None
        try:
            with open(log, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4096))
                lines = [ln for ln in f.read().decode("utf-8", errors="ignore").splitlines() if ln.strip()]
            return json.loads(lines[-1]) if lines else None
        except (OSError, ValueError):
            return None

    def payload(self, quotes: dict, market_breadth: dict, paper_account: dict | None, decisions: list,
                paper_status: dict | None = None) -> dict:
        now = datetime.now()
        today = now.date()
        out: dict = {"now": now.isoformat(timespec="seconds"), "accounts": [], "fills": [], "curves": {},
                     "live": None, "market": {}, "ai": None, "gateway": self.gateway()}
        if not self.db_path or not Path(self.db_path).exists():
            out["error"] = "找不到 jevq.sqlite"
            return out
        conn = _ro(self.db_path)
        try:
            daily = self._daily(conn, today)
            out["curves"] = daily["curves"]
            sent = self._sentiment(conn)
            mb = market_breadth or {}
            live_breadth = bool(mb.get("available") and mb.get("complete") and not mb.get("stale"))
            close = daily["close"] or {}
            out["market"] = {
                "live": live_breadth,
                "asOf": None if live_breadth else close.get("date"),
                "up": mb.get("up") if live_breadth else close.get("up"),
                "down": mb.get("down") if live_breadth else close.get("down"),
                "amount": mb.get("amount") if live_breadth else None,
                "limitUp": (sent or {}).get("limitUp") if live_breadth and sent else close.get("limitUp"),
                "limitDown": (sent or {}).get("limitDown") if live_breadth and sent else close.get("limitDown"),
                "ret5": daily["ret5"],
                "sentiment": (sent or {}).get("label"), "sentimentAt": (sent or {}).get("ts"),
            }
            names = {}
            board: dict = {"accounts": [], "fills": [], "watch": [], "gate": None}
            for acct in ACCOUNTS + AI_ACCOUNTS:
                cash_row = conn.execute("SELECT amount FROM cash WHERE account=?", (acct,)).fetchone()
                if cash_row is None:
                    continue
                prev = conn.execute("SELECT value FROM equity WHERE account=? AND date < ? ORDER BY date DESC LIMIT 1",
                                    (acct, today.isoformat())).fetchone()
                positions, mv = [], 0.0
                for p in conn.execute("SELECT code, shares, cost, buy_price, buy_date FROM positions WHERE account=?",
                                      (acct,)):
                    q = quotes.get(p["code"])
                    if q is None:
                        last = conn.execute("SELECT close FROM closes WHERE code=? ORDER BY date DESC LIMIT 1",
                                            (p["code"],)).fetchone()
                    price = q.price if q is not None else (last[0] if last else p["buy_price"])
                    names[p["code"]] = getattr(q, "name", None) or names.get(p["code"]) or p["code"]
                    mv += price * p["shares"]
                    positions.append({
                        "code": p["code"], "name": names[p["code"]], "shares": p["shares"], "price": round(price, 2),
                        "pct": round((q.price / q.pre_close - 1) * 100, 2) if q is not None and q.pre_close else None,
                        "pnl": round((price / p["buy_price"] - 1) * 100, 2) if p["buy_price"] else None,
                        "buyDate": p["buy_date"], "live": q is not None,
                        "breakeven": breakeven_price(Position(acct, p["code"], p["shares"], p["cost"], p["buy_price"],
                                                              date.fromisoformat(p["buy_date"])))
                        if acct in AI_ACCOUNTS and p["shares"] else None})
                equity = cash_row[0] + mv
                (board["accounts"] if acct in AI_ACCOUNTS else out["accounts"]).append({
                    "id": acct, "name": config.ACCOUNT_NAMES.get(acct, acct), "main": acct in MAIN_ACCOUNTS,
                    "live": acct == config.LIVE_ACCOUNT, "equity": round(equity, 2), "cash": round(cash_row[0], 2),
                    "today": round((equity / prev[0] - 1) * 100, 2) if prev and prev[0] else None,
                    "total": round((equity / config.INITIAL_CASH - 1) * 100, 2), "positions": positions})
            for f in conn.execute("SELECT account, code, side, price, shares, ts, reason FROM fills "
                                  "WHERE substr(ts, 1, 10) = ? AND account IN ({}) ORDER BY ts DESC LIMIT 40".format(
                                      ",".join("?" * len(ACCOUNTS + AI_ACCOUNTS))),
                                  (today.isoformat(), *ACCOUNTS, *AI_ACCOUNTS)):
                q = quotes.get(f["code"])
                (board["fills"] if f["account"] in AI_ACCOUNTS else out["fills"]).append({"t": f["ts"][11:16], "account": config.ACCOUNT_NAMES.get(f["account"], f["account"]),
                                     "code": f["code"], "name": getattr(q, "name", None) or names.get(f["code"], f["code"]),
                                     "side": f["side"], "price": f["price"], "shares": f["shares"],
                                     "reason": (f["reason"] or "").split("｜")[0][:24],
                                     "live": f["account"] == config.LIVE_ACCOUNT})
            board["watch"], board["gate"] = self._board_watch(conn, today)
            state = dict(conn.execute("SELECT key, value FROM live_state WHERE key IN "
                                      "('mode','paused','pause_reason','heartbeat','last_read_ok')").fetchall())
            hb = state.get("heartbeat")
            age = (now - datetime.fromisoformat(hb)).total_seconds() if hb else None
            out["live"] = {"mode": state.get("mode") or "off", "paused": state.get("paused") == "1",
                           "reason": state.get("pause_reason"), "heartbeatAge": None if age is None else round(age),
                           "positions": [r[0] for r in conn.execute("SELECT code FROM live_positions")]}
        finally:
            conn.close()
        # DeepSeek 自己写的分析，原样给页面（不拼模板）；没有分析的旧记录不展示
        thoughts = [{"id": r.get("id"), "ts": r.get("ts"), "time": r.get("time"), "regime": r.get("regime"),
                     "action": r.get("action"), "confidence": r.get("confidence"), "model": r.get("model"),
                     "analysis": r["analysis"]} for r in decisions if r.get("analysis")][:20]
        strategy = (paper_status or {}).get("latestStrategy") or None
        portfolio = (strategy or {}).get("portfolio") or {}
        out["ai"] = {"loop": self._ai_last(), "thoughts": thoughts,
                     "plan": None if not strategy else {
                         "ts": strategy.get("ts"), "posture": portfolio.get("posture"),
                         "positionPct": portfolio.get("positionPct"),
                         "orders": [{"code": c, "name": getattr(quotes.get(c), "name", None) or c, **o}
                                    for c, o in (portfolio.get("orders") or {}).items()],
                         "execution": strategy.get("execution")},
                     "account": paper_account, "board": board}
        return out

    @staticmethod
    def _board_watch(conn: sqlite3.Connection, today: date) -> tuple[list, str | None]:
        """今天扫首板观察池（后台每分钟写一次）+ 最近一条 AI 闸门没开的原因。"""
        snap = candidates.latest(conn)
        watch = snap["board"] if snap.get("date") == today.isoformat() else []
        row = conn.execute("SELECT ts, detail FROM decisions WHERE strategy='board' AND verdict='skip' "
                           "AND detail LIKE 'AI 闸门关%' AND substr(ts, 1, 10) = ? ORDER BY id DESC LIMIT 1",
                           (today.isoformat(),)).fetchone()
        return ([{k: w.get(k) for k in ("code", "name", "concept", "heat", "pct", "verdict", "why")} for w in watch],
                f"{row[0][11:16]} {row[1]}" if row else None)
