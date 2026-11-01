"""衡策面板本地只读服务：SQLite + 腾讯行情，不提供任何交易接口。"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from dashboard.adaptive_paper import AdaptivePaperEngine
from dashboard.decision_journal import DecisionJournal
from dashboard.kimi_judge import KimiJudge
from dashboard.market_snapshot import MarketSnapshotService
from dashboard.terminal import TerminalData
from jevq.data.tencent import parse_quotes
from research.kronos_status import kronos_status
from research.status import cross_sectional_status

DIST = Path(__file__).resolve().parent / "dist"
INDICES = (("sh000001", "000001", "上证指数"), ("sz399001", "399001", "深证成指"),
           ("sz399006", "399006", "创业板指"))
WATCHLIST = (("sz000559", "000559", "SZ"), ("sh603081", "603081", "SH"),
             ("sh603933", "603933", "SH"))


def choose_db(root: Path = ROOT) -> Path | None:
    for candidate in (root / "data" / "jevq.sqlite", root / "data" / "home_view.sqlite"):
        if candidate.exists():
            return candidate
    return None


def readonly_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def phase(now: datetime) -> str:
    if now.weekday() >= 5:
        return "休市"
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 30:
        return "盘前"
    if minutes < 11 * 60 + 30 or 13 * 60 <= minutes < 15 * 60:
        return "盘中"
    if minutes < 13 * 60:
        return "午间休市"
    return "盘后"


class DashboardService:
    def __init__(self, db_path: Path | None = None, quote_fetch=None, cache_seconds: float = 1,
                 market_service=None, decision_journal=None, decision_judge=None, paper_engine=None,
                 research_dir: Path | None = None, kronos_dir: Path | None = None,
                 kronos_smoke_dir: Path | None = None):
        self.db_path = db_path if db_path is not None else choose_db()
        self.quote_fetch = quote_fetch or self._fetch_quotes
        self.cache_seconds = cache_seconds
        self.market = market_service or MarketSnapshotService()
        self.paper = paper_engine
        self.research_dir = research_dir or ROOT / "research" / "artifacts" / "pit-full-market-baseline"
        self.kronos_dir = kronos_dir or ROOT / "research" / "artifacts" / "pit-kronos-small"
        self.kronos_smoke_dir = kronos_smoke_dir or ROOT / "research" / "artifacts" / "pit-kronos-small-smoke"
        if decision_journal is not None:
            self.journal = decision_journal
        else:
            subscription = os.environ.get("DASHBOARD_DECISION_SOURCE", "subscription") != "api"
            judge = decision_judge if decision_judge is not None else self._default_decision_judge()
            if self.paper is None:
                self.paper = AdaptivePaperEngine(ROOT / "dashboard" / "runtime" / "ai_paper.sqlite",
                                                 judge=judge, quote_provider=self._paper_quotes,
                                                 external=subscription)
            self.journal = DecisionJournal(ROOT / "dashboard" / "runtime" / "ai_decisions.jsonl", judge=judge,
                                           on_record=self.paper.process_record if self.paper is not None else None,
                                           external=subscription)
        self._lock = threading.Lock()
        self._quotes: dict = {}
        self._quote_error = ""
        self._last_attempt = 0.0
        self._last_success = 0.0
        self._previous_prices: dict[str, tuple[float, float]] = {}
        self._background_lock = threading.Lock()
        self._background_stop = threading.Event()
        self._background_thread = None
        self._background_last_run = None
        self._background_last_success = None
        self._background_error = ""
        self.terminal = TerminalData(self.db_path, ROOT / "dashboard" / "runtime")

    def terminal_payload(self) -> dict:
        """行情终端页：秒级行情 + jevq.sqlite 各账户 + 实盘/网关状态（只读）。"""
        self._refresh_quotes()
        paper = self.paper.status()["account"] if self.paper is not None else None
        return self.terminal.payload(self._quotes, self.market.payload().get("marketBreadth", {}), paper,
                                     self.journal.records())

    @staticmethod
    def _default_decision_judge():
        if os.environ.get("DASHBOARD_DECISION_SOURCE", "subscription") != "api" or not os.environ.get("KIMI_API_KEY"):
            return None
        session = requests.Session()
        session.trust_env = False
        return KimiJudge(session=session)

    @staticmethod
    def _fetch_quotes(symbols: list[str]) -> dict:
        session = requests.Session()
        session.trust_env = False
        response = session.get("https://qt.gtimg.cn/q=" + ",".join(symbols), timeout=8)
        response.raise_for_status()
        return parse_quotes(response.content.decode("gbk", errors="ignore"))

    def start_background(self, poll_seconds: float = 1) -> None:
        """重点行情按秒刷新；全市场扫描与 Kimi 调用分别限流。"""
        with self._background_lock:
            if self._background_thread is not None and self._background_thread.is_alive():
                return
            self._background_stop.clear()
            self._background_thread = threading.Thread(
                target=self._background_loop, args=(poll_seconds,), daemon=True, name="dashboard-decision-loop")
            self._background_thread.start()

    def stop_background(self) -> None:
        self._background_stop.set()
        thread = self._background_thread
        if thread is not None:
            thread.join(timeout=5)

    def _background_loop(self, poll_seconds: float) -> None:
        while not self._background_stop.is_set():
            with self._background_lock:
                self._background_last_run = datetime.now().isoformat(timespec="seconds")
            try:
                self.payload()
            except Exception as exc:
                with self._background_lock:
                    self._background_error = str(exc)
            else:
                with self._background_lock:
                    self._background_last_success = datetime.now().isoformat(timespec="seconds")
                    self._background_error = ""
            self._background_stop.wait(poll_seconds)

    def background_status(self) -> dict:
        with self._background_lock:
            return {"running": self._background_thread is not None and self._background_thread.is_alive(),
                    "lastRun": self._background_last_run, "lastSuccess": self._background_last_success,
                    "error": self._background_error}

    def research_status(self) -> dict:
        """只读取已生成 artifact；请求面板不会触发训练或任何交易动作。"""
        return cross_sectional_status(self.research_dir)

    def kronos_research_status(self) -> dict:
        return kronos_status(self.kronos_dir, self.kronos_smoke_dir)

    @staticmethod
    def _symbol(code: str) -> str:
        return ("sh" if code.startswith("6") else "bj" if code.startswith(("4", "8", "9")) else "sz") + code

    def _fast_symbols(self) -> list[str]:
        """指数、观察样本、模拟持仓和前三个行业领涨股进入秒级通道。"""
        symbols = [row[0] for row in INDICES + WATCHLIST]
        leaders = self.market.payload().get("sectorBreadth", {}).get("leaders", [])[:3]
        for row in leaders:
            raw = str(row.get("leaderCode", ""))
            code = raw[-6:]
            if len(code) == 6:
                symbols.append(raw if raw.startswith(("sh", "sz", "bj")) else self._symbol(code))
        if self.paper is not None:
            for row in self.paper.status()["account"]["positionDetails"]:
                symbols.append(self._symbol(row["code"]))
        terminal = getattr(self, "terminal", None)
        if terminal is not None:                 # 各策略账户、实盘的持仓也进秒级通道（行情终端页按它估值）
            symbols += [self._symbol(code) for code in terminal.held_codes()]
        return list(dict.fromkeys(symbols))

    def _paper_quotes(self, codes: list[str]) -> dict:
        """候选优先使用秒级快照，其余使用最近一次全市场快照。"""
        quotes = self.market.quotes_for(codes)
        quotes.update({code: self._quotes[code] for code in codes if code in self._quotes})
        return quotes

    def _refresh_quotes(self) -> None:
        now = time.monotonic()
        if now - self._last_attempt < self.cache_seconds:
            return
        with self._lock:
            if now - self._last_attempt < self.cache_seconds:
                return
            self._last_attempt = now
            symbols = self._fast_symbols()
            try:
                quotes = self.quote_fetch(symbols)
                if not quotes:
                    raise RuntimeError("行情接口未返回有效记录")
                wall = time.time()
                for code, quote in quotes.items():
                    if code in self._quotes:
                        self._previous_prices[code] = (wall, self._quotes[code].price)
                self._quotes = quotes
                self._last_success = wall
                self._quote_error = ""
                if self.paper is not None and hasattr(self.paper, "check_fast_risk"):
                    self.paper.check_fast_risk(self._quotes)
            except Exception as exc:
                self._quote_error = str(exc)

    @staticmethod
    def _number(value: float) -> str:
        return f"{value:,.2f}"

    def _markets(self) -> list[dict]:
        result = []
        for _, code, name in INDICES:
            quote = self._quotes.get(code)
            if quote is None:
                result.append({"name": name, "value": "—", "change": "未返回", "breadth": "等待只读行情"})
                continue
            change = (quote.price / quote.pre_close - 1) * 100
            result.append({"name": name, "value": self._number(quote.price),
                           "change": f"{change:+.2f}%", "breadth": f"昨收 {self._number(quote.pre_close)}",
                           "asOf": quote.ts.isoformat()})
        return result

    def _observations(self) -> list[dict]:
        result = []
        for _, code, suffix in WATCHLIST:
            quote = self._quotes.get(code)
            if quote is None:
                result.append({"name": code, "code": f"{code}.{suffix}", "price": "—", "change": None,
                               "minute": "—", "volume": "—", "amount": "—", "verdict": "行情未返回", "tone": "muted"})
                continue
            change = round((quote.price / quote.pre_close - 1) * 100, 2)
            minute = "—"
            previous = self._previous_prices.get(code)
            if previous and quote.price != previous[1]:
                minute = f"{(quote.price / previous[1] - 1) * 100:+.2f}%"
            result.append({"name": quote.name, "code": f"{code}.{suffix}", "price": f"{quote.price:.2f}",
                           "change": change, "minute": minute, "volume": f"{quote.volume:,}",
                           "amount": f"{quote.amount:.0f}", "verdict": "行情已更新，等待 AI 判断", "tone": "muted",
                           "asOf": quote.ts.isoformat()})
        return result

    def _account(self) -> dict:
        empty = {"available": False, "total": 10000.0, "cash": 10000.0, "positionPct": 0.0,
                 "todayPnl": None, "positions": 0, "fillsToday": 0, "source": "静态快照"}
        if not self.db_path or not self.db_path.exists():
            return empty
        try:
            conn = readonly_connect(self.db_path)
            try:
                cash_row = conn.execute("SELECT amount FROM cash WHERE account='ai_experiment'").fetchone()
                if cash_row is None:
                    return {**empty, "source": f"SQLite 无 AI 账户 · {self.db_path.name}"}
                cash = float(cash_row[0])
                rows = conn.execute("SELECT code,shares,cost FROM positions WHERE account='ai_experiment'").fetchall()
                market_value = 0.0
                for row in rows:
                    quote = self._quotes.get(row["code"])
                    market_value += (quote.price if quote else float(row["cost"]) / row["shares"]) * row["shares"]
                today = datetime.now().date().isoformat()
                fills_today = conn.execute("SELECT COUNT(*) FROM fills WHERE account='ai_experiment' AND substr(ts,1,10)=?",
                                           (today,)).fetchone()[0]
                total = cash + market_value
                return {"available": True, "total": total, "cash": cash,
                        "positionPct": market_value / total * 100 if total > 0 else 0.0,
                        "todayPnl": None, "positions": len(rows), "fillsToday": int(fills_today),
                        "source": f"SQLite 只读 · {self.db_path.name}"}
            finally:
                conn.close()
        except Exception as exc:
            return {**empty, "source": f"SQLite 读取失败 · {exc}"}

    def payload(self) -> dict:
        self._refresh_quotes()
        markets = self._markets()
        observations = self._observations()
        paper_status = self.paper.status() if self.paper is not None else None
        account = paper_status["account"] if paper_status is not None else self._account()
        breadth = self.market.payload()
        market_breadth, sector_breadth = breadth["marketBreadth"], breadth["sectorBreadth"]
        valid_quotes = list(self._quotes.values())
        quote_as_of = max((q.ts for q in valid_quotes), default=None)
        indices_received = sum(1 for item in markets if item["value"] != "—")
        sample_down = sum(1 for item in observations if item["change"] is not None and item["change"] < 0)
        live = bool(valid_quotes)
        display_time = quote_as_of.strftime("%Y-%m-%d %H:%M:%S") if quote_as_of else "尚未取得行情"
        payload = {
            "snapshot": {"asOf": quote_as_of.isoformat() if quote_as_of else None,
                         "displayTime": display_time, "phase": phase(datetime.now()),
                         "regime": "防守观察 · 09:53 历史决策", "mode": "防守观察",
                         "source": "腾讯/新浪只读行情 + 隔离模拟 SQLite"},
            "markets": markets, "observations": observations, "account": account, "paper": paper_status, **breadth,
            "evidence": {"indicesReceived": indices_received, "indicesTotal": len(INDICES),
                         "sampleDown": sample_down, "sampleTotal": len(observations),
                         "complete": indices_received == len(INDICES),
                         "researchComplete": bool(market_breadth.get("complete") and sector_breadth.get("complete"))},
            "live": {"connected": live, "stale": bool(self._quote_error) and live,
                     "error": self._quote_error, "quoteAsOf": display_time,
                     "lastSuccess": datetime.fromtimestamp(self._last_success).isoformat() if self._last_success else None,
                     "refreshSeconds": 1},
            "systemItems": [
                {"name": "执行模式", "value": "paper / dryrun", "note": "只读面板，无交易接口", "state": "good"},
                {"name": "交易网关", "value": "未连接", "note": "本服务不读取、不调用交易网关", "state": "good"},
                {"name": "行情连接", "value": "只读实时" if live else "未连接",
                 "note": self._quote_error or f"腾讯行情 · {display_time}", "state": "good" if live else "warn"},
                {"name": "全市场宽度", "value": "已接入" if market_breadth.get("complete") else "等待完整数据",
                 "note": market_breadth.get("error") or f"沪深京 A 股 · {market_breadth.get('total', 0)} 只有效行情",
                 "state": "good" if market_breadth.get("complete") else "warn"},
                {"name": "行业广度", "value": "已接入" if sector_breadth.get("complete") else "等待完整数据",
                 "note": sector_breadth.get("error") or f"新浪行业 · {sector_breadth.get('total', 0)} 个行业",
                 "state": "good" if sector_breadth.get("complete") else "warn"},
                {"name": "账户数据库", "value": "SQLite 只读" if account["available"] else "静态快照",
                 "note": account["source"], "state": "good" if account["available"] else "warn"},
                {"name": "真实委托", "value": "未启用", "note": "没有下单、撤单或切换实盘入口", "state": "good"},
                {"name": "策略验证",
                 "value": ("未接入" if paper_status is None else "未配置 Kimi" if not paper_status["enabled"]
                           else "对账暂停" if paper_status["paused"] or not paper_status["reconciled"]
                           else "模拟闭环运行"),
                 "note": (paper_status["paused"] or paper_status["reconciliationError"] or paper_status["error"]
                          if paper_status is not None else "未接入模拟策略") or
                         "Kimi 动态策略 → 模拟成交 → 硬风控 → 独立对账；样本仍待积累",
                 "state": "good" if paper_status is not None and paper_status["enabled"] and
                          paper_status["reconciled"] and not paper_status["paused"] and not paper_status["error"] else "warn"},
            ],
        }
        self.journal.maybe_update(payload)
        persisted = self.journal.records()
        journal_status = self.journal.status()
        journal_status["background"] = self.background_status()
        payload["decisions"] = persisted
        payload["decisionStore"] = journal_status
        if persisted:
            latest = persisted[0]
            payload["snapshot"]["regime"] = f"{latest['regime']} · {latest['time']} 盘中 AI"
            payload["snapshot"]["mode"] = latest["regime"]
        payload["systemItems"].append({
            "name": "AI 决策日志",
            "value": ("调用失败" if journal_status["error"] else "未配置 Kimi" if not journal_status["enabled"]
                      else "后台盯盘" if journal_status["background"]["running"] else "页面请求时运行"),
            "note": (journal_status["error"] or journal_status["background"]["error"]
                     or f"Kimi · 追加式 JSONL · 已保存 {journal_status['count']} 条 · 不触发真实交易"),
            "state": "good" if journal_status["enabled"] and not journal_status["error"] else "warn",
        })
        return payload


class Handler(SimpleHTTPRequestHandler):
    service: DashboardService

    def _json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/api/health":
            self._json(200, {"ok": True, "readonly": True, "trading": False})
            return
        if self.path == "/api/dashboard":
            try:
                self._json(200, self.service.payload())
            except Exception as exc:
                self._json(503, {"ok": False, "error": str(exc)})
            return
        if self.path == "/api/terminal":
            try:
                self._json(200, self.service.terminal_payload())
            except Exception as exc:
                self._json(503, {"ok": False, "error": str(exc)})
            return
        if self.path == "/api/research/cross-sectional":
            try:
                self._json(200, self.service.research_status())
            except Exception as exc:
                self._json(503, {"ok": False, "error": str(exc)})
            return
        if self.path == "/api/research/kronos":
            try:
                self._json(200, self.service.kronos_research_status())
            except Exception as exc:
                self._json(503, {"ok": False, "error": str(exc)})
            return
        if self.path.startswith("/api/"):
            self._json(404, {"ok": False, "error": "未知只读接口"})
            return
        super().do_GET()

    def log_message(self, fmt: str, *args) -> None:
        print(f"[dashboard] {self.address_string()} {fmt % args}")


def main() -> None:
    if sys.stdout is None:
        sys.stdout = open(Path.home() / "NUL" if sys.platform == "win32" else "/dev/null", "w")
    if sys.stderr is None:
        sys.stderr = sys.stdout
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7100)
    parser.add_argument("--api-only", action="store_true")
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    service = DashboardService(db_path=args.db)
    directory = str(DIST) if not args.api_only else None
    handler = partial(type("BoundHandler", (Handler,), {"service": service}), directory=directory)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    service.start_background()
    print(f"衡策只读服务：http://{args.host}:{args.port} · SQLite={service.db_path} · Kimi 后台盯盘")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop_background()
        server.server_close()


if __name__ == "__main__":
    main()
