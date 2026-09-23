import json
import sqlite3
from datetime import date, datetime, timedelta

from jevq.config import (ACCOUNTS, MAX_CHECKS_PER_TICK, MAX_ERRORS_PER_TICK, MAX_PER_TRADE,
                         SENTIMENT_REFRESH_MIN)
from jevq.ledger import Ledger, affordable_shares
from jevq.models import Fill, Quote
from jevq.rules.buy import buy_mode, buy_window_open, rank_candidates
from jevq.rules.sell import decide_sell


class Engine:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, notice_judge, news_judge,
                 market_judge, fetch_quotes, fetch_pools, universe: dict[str, str],
                 ret5: dict[str, float], prev_day: date, overseas: str | None,
                 overseas_trace: dict | None = None):
        self.conn, self.ledger = conn, ledger
        self.notice_judge, self.news_judge, self.market_judge = notice_judge, news_judge, market_judge
        self.fetch_quotes, self.fetch_pools = fetch_quotes, fetch_pools
        self.universe, self.ret5, self.prev_day, self.overseas = universe, ret5, prev_day, overseas
        self.overseas_trace = overseas_trace
        self.rejected: set[str] = set()
        self.fail_streak = 0
        self.halted = False
        self.last_quotes: dict[str, Quote] = {}
        self.sentiment_label: str | None = None
        self.sentiment_trace: dict | None = None
        self._sentiment_at: datetime | None = None

    def tick(self, now: datetime) -> list[Fill]:
        quotes = self._fetch(now)
        if quotes is None:
            return []
        fills = self._sells(quotes, now)
        mode = buy_mode(self._sentiment(now, quotes), self.overseas)
        if self.halted or not buy_window_open(mode, now.time()):
            return fills
        ranked = rank_candidates((q for c, q in quotes.items() if c in self.universe), self.ret5)
        fills += self._buy_shadow(ranked, now)
        fills += self._buy_main(ranked, now, mode)
        return fills

    def _fetch(self, now: datetime) -> dict[str, Quote] | None:
        held = {p.code for a in ACCOUNTS for p in self.ledger.positions(a)}
        try:
            quotes = self.fetch_quotes(sorted(set(self.universe) | held))
        except Exception as e:
            self.fail_streak += 1
            print(f"[engine] {now:%H:%M} 行情失败({self.fail_streak}): {e!r}")
            if self.fail_streak >= 3:
                self.halted = True
            return None
        self.fail_streak = 0
        self.last_quotes = quotes
        total = sum(q.amount for c, q in quotes.items() if c in self.universe)
        self.conn.execute("INSERT OR REPLACE INTO turnover VALUES (?, ?)",
                          (now.strftime("%Y-%m-%d %H:%M"), total))
        self.conn.commit()
        return quotes

    def _sells(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        fills = []
        for account in ACCOUNTS:
            for pos in self.ledger.positions(account):
                q = quotes.get(pos.code)
                if q is None:
                    continue
                d = decide_sell(pos, q, now)
                if d.sell and (f := self.ledger.sell(account, pos, q, now, d.reason)):
                    fills.append(f)
        return fills

    def _sentiment(self, now: datetime, quotes: dict[str, Quote]) -> str | None:
        fresh = self._sentiment_at and now - self._sentiment_at < timedelta(minutes=SENTIMENT_REFRESH_MIN)
        if not fresh:
            stats = self._market_stats(now, quotes)
            if stats:
                try:
                    self.sentiment_label = self.market_judge.sentiment(stats)
                    self.sentiment_trace = getattr(self.market_judge, "sentiment_trace", None)
                except Exception as e:
                    print(f"[engine] 情绪判断异常: {e!r}")
                    self.sentiment_label = None
                    self.sentiment_trace = None
            else:
                self.sentiment_label = None
                self.sentiment_trace = None
            self._sentiment_at = now
            self._log(now, "", "sentiment", json.dumps(
                {"stats": stats, "label": self.sentiment_label}, ensure_ascii=False), trace=self.sentiment_trace)
        return self.sentiment_label

    def _market_stats(self, now: datetime, quotes: dict[str, Quote]) -> dict:
        try:
            stats = dict(self.fetch_pools(now.date()))
        except Exception as e:
            print(f"[engine] 涨跌停池失败: {e!r}")
            return {}
        uni = [q for c, q in quotes.items() if c in self.universe]
        stats["上涨家数"] = sum(q.pct > 0 for q in uni)
        stats["下跌家数"] = sum(q.pct < 0 for q in uni)
        prev = self.conn.execute("SELECT amount FROM turnover WHERE ts=?",
                                 (f"{self.prev_day.isoformat()} {now:%H:%M}",)).fetchone()
        if prev and prev[0] > 0:
            cur = sum(q.amount for q in uni)
            stats["成交额较昨日同期%"] = round((cur / prev[0] - 1) * 100, 1)
        return stats

    def _buy_shadow(self, ranked: list[Quote], now: datetime) -> list[Fill]:
        fills = []
        for q in ranked:
            if self.ledger.free_slots("shadow") <= 0:
                break
            if f := self.ledger.buy("shadow", q, now, f"影子｜低吸{q.pct}%｜5日{self.ret5[q.code]}%"):
                fills.append(f)
        return fills

    def _buy_main(self, ranked: list[Quote], now: datetime, mode: str) -> list[Fill]:
        fills, checked, errors = [], 0, 0
        for rank, q in enumerate(ranked, 1):
            if self.ledger.free_slots("main") <= 0 or checked >= MAX_CHECKS_PER_TICK:
                break
            if q.code in self.rejected or self.ledger.holds("main", q.code):
                continue
            if errors >= MAX_ERRORS_PER_TICK:
                break
            if affordable_shares(min(self.ledger.cash("main"), MAX_PER_TRADE), q.price) == 0:
                continue
            checked += 1
            verdict, why = self._check(self.notice_judge, q.code, now.date())
            notice_trace = getattr(self.notice_judge, "last_trace", None)
            news_trace = None
            if verdict == "pass":
                verdict, why = self._check(self.news_judge, q.code, q.name, now)
                news_trace = getattr(self.news_judge, "last_trace", None)
            trace = {"candidate": {"name": q.name, "price": q.price, "pct": q.pct,
                                   "ret5": self.ret5[q.code], "rank": rank, "of": len(ranked)},
                     "notices": notice_trace, "news": news_trace,
                     "market": {"mode": mode, "sentiment": self.sentiment_label,
                                "sentiment_trace": self.sentiment_trace,
                                "overseas": self.overseas, "overseas_trace": self.overseas_trace},
                     "outcome": {"bought": False, "shares": None, "price": None}}
            if verdict in ("reject", "error"):
                self._log(now, q.code, verdict, why, trace)
                self.rejected.add(q.code)
                if verdict == "error":
                    # Spec §7: Jev 超时/报错 → 重试一次（JevClient 内部已重试）；仍失败 → 该股当日不买。
                    errors += 1
                continue
            f = self.ledger.buy("main", q, now, f"低吸{q.pct}%｜5日{self.ret5[q.code]}%｜{why}")
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

    def _log(self, now: datetime, code: str, verdict: str, detail: str, trace: dict | None = None):
        self.conn.execute("INSERT INTO decisions(ts, code, verdict, detail, trace) VALUES (?, ?, ?, ?, ?)",
                          (now.isoformat(timespec="seconds"), code, verdict, detail,
                           None if trace is None else json.dumps(trace, ensure_ascii=False)))
        self.conn.commit()
