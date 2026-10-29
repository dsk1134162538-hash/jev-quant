"""全市场与行业广度：独立只读快照，失败时保留最后成功数据。"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

import requests

from jevq.data.tencent import parse_quotes

QT_URL = "https://qt.gtimg.cn/q="
SINA_INDUSTRIES_URL = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
_DIRECT_ENV_LOCK = threading.Lock()


def market_symbol(code: str) -> str:
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("4", "8", "9")):
        return "bj" + code
    return "sz" + code


def fetch_universe() -> dict[str, str]:
    """沪深京 A 股清单。仅此处临时绕过系统代理，完成后恢复环境。"""
    import akshare as ak

    with _DIRECT_ENV_LOCK:
        old = {key: os.environ.get(key) for key in ("NO_PROXY", "no_proxy")}
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
        try:
            frame = ak.stock_info_a_code_name()
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return {str(row["code"]).zfill(6): str(row["name"]).replace(" ", "") for _, row in frame.iterrows()}


def fetch_all_quotes(codes: list[str], chunk: int = 400) -> dict:
    session = requests.Session()
    session.trust_env = False
    result = {}
    for start in range(0, len(codes), chunk):
        symbols = [market_symbol(code) for code in codes[start:start + chunk]]
        response = session.get(QT_URL + ",".join(symbols), timeout=12)
        response.raise_for_status()
        result.update(parse_quotes(response.content.decode("gbk", errors="ignore")))
    return result


def parse_industries(text: str) -> list[dict]:
    raw = json.loads(text[text.find("{"):])
    rows = []
    for value in raw.values():
        parts = value.split(",")
        try:
            rows.append({
                "name": parts[1], "companies": int(float(parts[2])), "change": round(float(parts[5]), 2),
                "amount": float(parts[7]), "leaderCode": parts[8], "leaderName": parts[12],
                "leaderChange": round(float(parts[9]), 2),
            })
        except (ValueError, IndexError):
            continue
    return sorted(rows, key=lambda row: row["change"], reverse=True)


def fetch_industries() -> list[dict]:
    session = requests.Session()
    session.trust_env = False
    response = session.get(SINA_INDUSTRIES_URL, timeout=10)
    response.raise_for_status()
    return parse_industries(response.content.decode("gbk", errors="ignore"))


def summarize_market(quotes: dict, universe: dict[str, str], now: datetime) -> dict:
    valid = [quote for code, quote in quotes.items()
             if code in universe and quote.ts.date() == now.date() and not quote.suspended]
    total = len(valid)
    up = sum(quote.pct > 0 for quote in valid)
    down = sum(quote.pct < 0 for quote in valid)
    amount = sum(quote.amount for quote in valid)
    as_of = max((quote.ts for quote in valid), default=None)
    coverage = total / len(universe) if universe else 0.0
    return {
        "available": total > 0, "complete": total >= 1000 and coverage >= 0.90,
        "asOf": as_of.isoformat() if as_of else None, "up": up, "down": down, "flat": total - up - down,
        "total": total, "universe": len(universe), "coverage": round(coverage * 100, 1),
        "upRatio": round(up / total * 100, 1) if total else None,
        "limitUp": sum(bool(q.limit_up) and q.price >= q.limit_up for q in valid),
        "limitDown": sum(bool(q.limit_down) and q.price <= q.limit_down for q in valid),
        "amount": amount, "source": "腾讯全市场只读行情",
    }


def summarize_industries(rows: list[dict], fetched_at: datetime) -> dict:
    up = sum(row["change"] > 0 for row in rows)
    down = sum(row["change"] < 0 for row in rows)
    return {
        "available": bool(rows), "complete": len(rows) >= 40, "asOf": fetched_at.isoformat(),
        "up": up, "down": down, "flat": len(rows) - up - down, "total": len(rows),
        "leaders": rows[:5], "laggards": list(reversed(rows[-5:])), "source": "新浪行业只读行情",
    }


class MarketSnapshotService:
    def __init__(self, universe_fetch=None, quote_fetch=None, industry_fetch=None, cache_seconds: float = 30,
                 clock=datetime.now):
        self.universe_fetch = universe_fetch or fetch_universe
        self.quote_fetch = quote_fetch or fetch_all_quotes
        self.industry_fetch = industry_fetch or fetch_industries
        self.cache_seconds = cache_seconds
        self.clock = datetime.now if clock is datetime else clock
        self._lock = threading.Lock()
        self._refreshing = False
        self._last_attempt = 0.0
        self._universe: dict[str, str] = {}
        self._universe_date = None
        self._market: dict = {}
        self._sectors: dict = {}
        self._quotes: dict = {}
        self._market_error = ""
        self._sectors_error = ""

    def ensure_fresh(self) -> None:
        with self._lock:
            if self._refreshing or time.monotonic() - self._last_attempt < self.cache_seconds:
                return
            self._refreshing = True
            self._last_attempt = time.monotonic()
        threading.Thread(target=self.refresh_now, daemon=True, name="dashboard-market-breadth").start()

    def refresh_now(self) -> None:
        now = self.clock()
        try:
            universe = self._universe
            if not universe or self._universe_date != now.date():
                universe = self.universe_fetch()
                if not universe:
                    raise RuntimeError("全市场股票清单为空")
            quotes = self.quote_fetch(sorted(universe))
            market = summarize_market(quotes, universe, now)
            if not market["available"]:
                raise RuntimeError("全市场没有当日有效行情")
            with self._lock:
                self._universe, self._universe_date = universe, now.date()
                self._market, self._quotes, self._market_error = market, quotes, ""
        except Exception as exc:
            with self._lock:
                self._market_error = str(exc)
        try:
            rows = self.industry_fetch()
            if not rows:
                raise RuntimeError("行业行情为空")
            sectors = summarize_industries(rows, now)
            with self._lock:
                self._sectors, self._sectors_error = sectors, ""
        except Exception as exc:
            with self._lock:
                self._sectors_error = str(exc)
        finally:
            with self._lock:
                self._refreshing = False

    def quotes_for(self, codes: list[str]) -> dict:
        """返回最近一次全市场快照中的候选股；不向调用方暴露可变的整表。"""
        with self._lock:
            return {code: self._quotes[code] for code in codes if code in self._quotes}

    def payload(self, blocking: bool = False) -> dict:
        if blocking:
            self.refresh_now()
        else:
            self.ensure_fresh()
        with self._lock:
            market = {**self._market} if self._market else {
                "available": False, "complete": False, "up": 0, "down": 0, "flat": 0, "total": 0,
                "universe": 0, "coverage": 0.0, "upRatio": None, "amount": 0, "source": "等待全市场行情",
            }
            sectors = {**self._sectors} if self._sectors else {
                "available": False, "complete": False, "up": 0, "down": 0, "flat": 0, "total": 0,
                "leaders": [], "laggards": [], "source": "等待行业行情",
            }
            for target, source, error in ((market, self._market, self._market_error),
                                          (sectors, self._sectors, self._sectors_error)):
                target["stale"] = bool(error) and bool(source)
                target["loading"] = self._refreshing and not source
                target["error"] = error
        return {"marketBreadth": market, "sectorBreadth": sectors}
