"""超跌反弹：近 5 日跌了 0~20%、近两个月没连板过的票，当日回调到低吸区间时买入赌反弹，反弹就走。

买入时机只看情绪（正常/火热盘中买，偏弱/冰点尾盘买），卖出与低吸完全相同（rules.sell.decide_sell）。
行情与情绪都由低吸引擎每分钟取好传进来，本策略不自己拉行情、不自己问情绪。
"""
import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime

from jevq.config import MAX_CHECKS_PER_TICK, MAX_ERRORS_PER_TICK, MAX_PER_TRADE, STRATEGIES
from jevq.ledger import Ledger, affordable_shares
from jevq.models import Fill, Quote
from jevq.rules.buy import buy_window_open, rank_rebound, rebound_mode
from jevq.rules.sell import decide_sell

SID = "rebound"


class ReboundStrategy:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, notice_judge, news_judge,
                 universe: dict[str, str], ret5: dict[str, float], excluded: set[str], prev_day: date,
                 sentiment_provider: Callable[[], str | None], enabled: bool = True,
                 sentiment_trace_provider: Callable[[], dict | None] | None = None):
        self.conn, self.ledger = conn, ledger
        self.accounts = STRATEGIES[SID]["accounts"]
        self.notice_judge, self.news_judge = notice_judge, news_judge
        self.universe, self.ret5, self.excluded = universe, ret5, excluded
        self.sentiment_provider = sentiment_provider
        self.sentiment_trace_provider = sentiment_trace_provider or (lambda: None)
        self.enabled = enabled
        self.stale_before: date | None = prev_day      # 同低吸：错过上一交易日的持仓开盘即卖
        self.rejected: set[str] = set()
        self._rejected_day: date | None = None

    def on_minute(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        if self._rejected_day != now.date():
            self._restore_rejected(now.date())
        if not quotes:
            return []
        fills = self._sells(quotes, now)
        if not self.enabled:
            return fills
        sentiment = self.sentiment_provider()
        mode = rebound_mode(sentiment)
        if not buy_window_open(mode, now.time()):
            return fills
        ranked = rank_rebound(
            (q for c, q in quotes.items() if c in self.universe and q.ts.date() >= now.date()),
            self.ret5, self.excluded)
        fills += self._buy_shadow(ranked, now)
        fills += self._buy_main(ranked, now, mode, sentiment)
        return fills

    def _restore_rejected(self, day: date):
        """重启后恢复当日已否决/出错的票，避免重复问 Jev。"""
        rows = self.conn.execute(
            "SELECT DISTINCT code FROM decisions WHERE verdict IN ('reject', 'error') AND code != '' "
            "AND strategy = ? AND substr(ts, 1, 10) = ?", (SID, day.isoformat())).fetchall()
        self.rejected = {r[0] for r in rows}
        self._rejected_day = day

    def _sells(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        fills = []
        for account in self.accounts:
            for pos in self.ledger.positions(account):
                q = quotes.get(pos.code)
                if q is None or q.ts.date() < now.date():
                    continue
                d = decide_sell(pos, q, now, self.stale_before)
                if d.sell and (f := self.ledger.sell(account, pos, q, now, d.reason)):
                    fills.append(f)
        return fills

    def _buy_shadow(self, ranked: list[Quote], now: datetime) -> list[Fill]:
        shadow = self.accounts[1]
        fills = []
        for q in ranked:
            if self.ledger.free_slots(shadow) <= 0:
                break
            if f := self.ledger.buy(shadow, q, now, f"影子｜超跌{q.pct}%｜5日{self.ret5[q.code]}%"):
                fills.append(f)
        return fills

    def _buy_main(self, ranked: list[Quote], now: datetime, mode: str, sentiment: str | None) -> list[Fill]:
        main = self.accounts[0]
        fills, checked, errors = [], 0, 0
        for rank, q in enumerate(ranked, 1):
            if self.ledger.free_slots(main) <= 0 or checked >= MAX_CHECKS_PER_TICK:
                break
            if q.code in self.rejected or self.ledger.holds(main, q.code):
                continue
            if errors >= MAX_ERRORS_PER_TICK:
                break
            if affordable_shares(min(self.ledger.cash(main), MAX_PER_TRADE), q.price) == 0:
                continue
            checked += 1
            verdict, why = self._check(self.notice_judge, q.code, now.date())
            notice_trace = getattr(self.notice_judge, "last_trace", None)
            news_trace = None
            if verdict == "pass":
                verdict, why = self._check(self.news_judge, q.code, q.name, now)
                news_trace = getattr(self.news_judge, "last_trace", None)
            trace = {"strategy": SID,
                     "candidate": {"name": q.name, "price": q.price, "pct": q.pct,
                                   "ret5": self.ret5[q.code], "rank": rank, "of": len(ranked)},
                     "notices": notice_trace, "news": news_trace,
                     "market": {"mode": mode, "sentiment": sentiment,
                                "sentiment_trace": self.sentiment_trace_provider()},
                     "outcome": {"bought": False, "shares": None, "price": None}}
            if verdict in ("reject", "error"):
                self._log(now, q.code, verdict, why, trace)
                self.rejected.add(q.code)
                if verdict == "error":
                    errors += 1
                continue
            f = self.ledger.buy(main, q, now, f"超跌{q.pct}%｜5日{self.ret5[q.code]}%｜{why}")
            if f:
                trace["outcome"] = {"bought": True, "shares": f.shares, "price": f.price}
                fills.append(f)
            self._log(now, q.code, verdict, why, trace)
        return fills

    @staticmethod
    def _check(judge, *args) -> tuple[str, str]:
        try:
            return judge.check(*args)
        except Exception as e:
            return "error", f"判断异常：{e!r}"

    def _log(self, now: datetime, code: str, verdict: str, detail: str, trace: dict):
        self.conn.execute(
            "INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, ?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), code, verdict, detail,
             json.dumps(trace, ensure_ascii=False), SID))
        self.conn.commit()
