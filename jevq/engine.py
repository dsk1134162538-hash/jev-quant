import json
import sqlite3
from datetime import date, datetime, timedelta

from jevq.config import ACCOUNTS, MAX_CHECKS_PER_TICK, SENTIMENT_REFRESH_MIN
from jevq.ledger import Ledger
from jevq.models import Fill, Quote
from jevq.rules.buy import buy_mode, buy_window_open, rank_candidates
from jevq.rules.sell import decide_sell


class Engine:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, notice_judge, news_judge,
                 market_judge, fetch_quotes, fetch_pools, universe: dict[str, str],
                 ret5: dict[str, float], prev_day: date, overseas: str | None):
        self.conn, self.ledger = conn, ledger
        self.notice_judge, self.news_judge, self.market_judge = notice_judge, news_judge, market_judge
        self.fetch_quotes, self.fetch_pools = fetch_quotes, fetch_pools
        self.universe, self.ret5, self.prev_day, self.overseas = universe, ret5, prev_day, overseas
        self.rejected: set[str] = set()
        self.fail_streak = 0
        self.halted = False
        self.last_quotes: dict[str, Quote] = {}
        self.sentiment_label: str | None = None
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
        fills += self._buy_main(ranked, now)
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
            self.sentiment_label = self.market_judge.sentiment(stats) if stats else None
            self._sentiment_at = now
            self._log(now, "", "sentiment", json.dumps(
                {"stats": stats, "label": self.sentiment_label}, ensure_ascii=False))
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

    def _buy_main(self, ranked: list[Quote], now: datetime) -> list[Fill]:
        fills, checked = [], 0
        for q in ranked:
            if self.ledger.free_slots("main") <= 0 or checked >= MAX_CHECKS_PER_TICK:
                break
            if q.code in self.rejected or self.ledger.holds("main", q.code):
                continue
            checked += 1
            verdict, why = self.notice_judge.check(q.code, now.date())
            if verdict == "pass":
                verdict, why = self.news_judge.check(q.code, q.name, now)
            self._log(now, q.code, verdict, why)
            if verdict == "reject":
                self.rejected.add(q.code)
                continue
            if verdict == "error":
                continue
            if f := self.ledger.buy("main", q, now, f"低吸{q.pct}%｜5日{self.ret5[q.code]}%｜{why}"):
                fills.append(f)
        return fills

    def _log(self, now: datetime, code: str, verdict: str, detail: str):
        self.conn.execute("INSERT INTO decisions(ts, code, verdict, detail) VALUES (?, ?, ?, ?)",
                          (now.isoformat(timespec="seconds"), code, verdict, detail))
        self.conn.commit()
