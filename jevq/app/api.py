import json
import re
import threading
from collections import deque
from datetime import date, datetime
from pathlib import Path

from jevq import db
from jevq.config import ACCOUNTS, DB_PATH, INITIAL_CASH, REPORT_DIR
from jevq.data import tencent
from jevq.ledger import Ledger

REPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
NAMES = {"main": "主账户(Jev)", "shadow": "影子账户"}


class Api:
    """暴露给 pywebview 前端的方法；下划线开头的成员不会暴露给 JS。"""

    def __init__(self, db_path: str = DB_PATH, report_dir: str = REPORT_DIR,
                 runner=None, fetch_quotes=tencent.fetch_quotes):
        if runner is None:
            from run import run_day as runner
        self._db_path, self._report_dir = db_path, Path(report_dir)
        self._runner, self._fetch_quotes = runner, fetch_quotes
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._logs: deque[str] = deque(maxlen=500)
        self._state: dict = {}
        self._names: dict[str, str] = {}

    # ---- 运行控制 ----
    def start(self) -> dict:
        if self.is_running():
            return {"ok": False, "msg": "已经在运行"}
        self._stop = threading.Event()
        self._state.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return {"ok": True, "msg": "已启动"}

    def stop(self) -> dict:
        self._stop.set()
        return {"ok": True, "msg": "正在停止"}

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        self._log("开始运行")
        try:
            self._runner(stop=self._stop, log=self._log, state=self._state)
        except Exception as e:
            self._log(f"出错：{e}")
        finally:
            self._state["phase"] = "未运行"
            self._log("运行结束")

    def _log(self, msg: str):
        self._logs.append(f"{datetime.now():%H:%M:%S} {msg}")

    # ---- 查询 ----
    def _lookup_names(self, codes, quotes: dict | None = None) -> dict[str, str]:
        known = {c: q.name for c, q in (quotes or {}).items()}
        known.update(self._names)
        known.update(self._state.get("names") or {})      # 股票池名称优先级最高
        missing = sorted({c for c in codes if c and c not in known})
        if missing:
            try:
                known.update({c: q.name for c, q in self._fetch_quotes(missing).items()})
            except Exception as e:
                self._log(f"股票名称获取失败：{e}")
        self._names.update({c: known[c] for c in codes if c in known})
        return {c: known.get(c, "") for c in codes}

    def status(self) -> dict:
        conn = db.connect(self._db_path)
        try:
            ledger = Ledger(conn)
            prices = dict(self._state.get("prices") or {})
            held = {p.code for a in ACCOUNTS for p in ledger.positions(a)}
            missing = sorted(held - prices.keys())
            quotes = {}
            if missing:
                try:
                    quotes = self._fetch_quotes(missing)
                    prices.update({c: q.price for c, q in quotes.items()})
                except Exception as e:
                    self._log(f"持仓行情获取失败：{e}")
            names = self._lookup_names(held, quotes)
            accounts = [self._account(ledger, a, prices, names) for a in ACCOUNTS]
        finally:
            conn.close()
        return {"running": self.is_running(), "phase": self._state.get("phase", "未运行"),
                "overseas": self._state.get("overseas"), "sentiment": self._state.get("sentiment"),
                "today": date.today().isoformat(), "accounts": accounts}

    def _account(self, ledger: Ledger, account: str, prices: dict[str, float],
                names: dict[str, str]) -> dict:
        value = ledger.equity(account, prices)
        positions = []
        for p in ledger.positions(account):
            price = prices.get(p.code)
            positions.append({"code": p.code, "name": names.get(p.code, ""), "shares": p.shares,
                              "buy_price": p.buy_price,
                              "buy_date": p.buy_date.isoformat(), "price": price,
                              "pnl_pct": None if price is None else round((price / p.buy_price - 1) * 100, 2)})
        return {"id": account, "name": NAMES[account], "equity": value,
                "cash": round(ledger.cash(account), 2),
                "return_pct": round((value / INITIAL_CASH - 1) * 100, 2), "positions": positions}

    def fills(self, day: str) -> list[dict]:
        conn = db.connect(self._db_path)
        try:
            fills = Ledger(conn).fills_on(date.fromisoformat(day))
        finally:
            conn.close()
        names = self._lookup_names({f.code for f in fills})
        return [{"time": f.ts.strftime("%H:%M"), "account": NAMES[f.account], "code": f.code,
                 "name": names[f.code],
                 "side": "买" if f.side == "buy" else "卖", "price": f.price, "shares": f.shares,
                 "fee": f.fee, "reason": f.reason}
                for f in fills]

    def decisions(self, day: str) -> list[dict]:
        conn = db.connect(self._db_path)
        try:
            rows = conn.execute("SELECT id, ts, code, verdict, detail, trace IS NOT NULL FROM decisions "
                                "WHERE substr(ts, 1, 10)=? ORDER BY id DESC LIMIT 300", (day,)).fetchall()
        finally:
            conn.close()
        names = self._lookup_names({code for _, _, code, _, _, _ in rows if code})
        return [{"id": did, "time": ts[11:16], "code": code or "市场",
                 "name": names.get(code, "") if code else "",
                 "verdict": verdict, "detail": detail, "has_trace": bool(has_trace)}
                for did, ts, code, verdict, detail, has_trace in rows]

    def decision(self, decision_id: int) -> dict | None:
        try:
            decision_id = int(decision_id)
        except (TypeError, ValueError):
            return None
        conn = db.connect(self._db_path)
        try:
            row = conn.execute("SELECT id, ts, code, verdict, detail, trace FROM decisions WHERE id=?",
                               (decision_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        did, ts, code, verdict, detail, raw = row
        try:
            trace = json.loads(raw) if raw else None
        except ValueError:
            trace = None
        name = self._lookup_names({code})[code] if code else ""
        return {"id": did, "time": ts[11:16], "code": code or "市场", "name": name,
                "verdict": verdict, "detail": detail, "trace": trace}

    def equity_curve(self) -> dict[str, list]:
        conn = db.connect(self._db_path)
        try:
            rows = conn.execute("SELECT date, account, value FROM equity ORDER BY date").fetchall()
        finally:
            conn.close()
        out: dict[str, list] = {a: [] for a in ACCOUNTS}
        for d, a, v in rows:
            out[a].append([d, v])
        return out

    def reports(self) -> list[str]:
        if not self._report_dir.exists():
            return []
        return sorted((p.name for p in self._report_dir.glob("*.md") if REPORT_NAME.match(p.name)),
                      reverse=True)

    def report(self, name: str) -> str:
        if not REPORT_NAME.match(name):
            raise ValueError(f"非法报告名：{name}")
        return (self._report_dir / name).read_text(encoding="utf-8")

    def logs(self) -> list[str]:
        return list(self._logs)
