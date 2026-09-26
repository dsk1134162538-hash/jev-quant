import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from time import monotonic

from jevq.config import (ACCOUNTS, MAX_CHECKS_PER_TICK, MAX_ERRORS_PER_TICK, MAX_PER_TRADE,
                         SENTIMENT_REFRESH_MIN, STRATEGIES)
from jevq.ledger import Ledger, affordable_shares
from jevq.models import Fill, Quote
from jevq.rules.buy import buy_mode, buy_window_open, rank_candidates
from jevq.rules.sell import decide_sell


class Engine:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, notice_judge, news_judge,
                 market_judge, fetch_quotes, fetch_pools, universe: dict[str, str],
                 ret5: dict[str, float], prev_day: date, overseas: str | None,
                 overseas_trace: dict | None = None,
                 accounts: tuple[str, str] = STRATEGIES["lowbuy"]["accounts"],
                 accounts_provider: Callable[[], tuple[str, ...]] | None = None):
        self.conn, self.ledger = conn, ledger
        self.accounts = accounts
        # 持仓代码（_fetch 用来决定该拉哪些行情）要看全部账户，不只是本策略自己那两个；
        # 默认取内置四个账户，保证低吸这条主线的行为不变。自定义策略跑起来后由调用方
        # 注入一个能看到「今天在跑」账户全集的 provider（见 jevq.accounts.all_accounts）。
        self.accounts_provider = accounts_provider or (lambda: ACCOUNTS)
        self.notice_judge, self.news_judge, self.market_judge = notice_judge, news_judge, market_judge
        self.fetch_quotes, self.fetch_pools = fetch_quotes, fetch_pools
        self.universe, self.ret5, self.prev_day, self.overseas = universe, ret5, prev_day, overseas
        self.overseas_trace = overseas_trace
        self.stale_before: date | None = prev_day
        self.rejected: set[str] = set()
        self._rejected_day: date | None = None
        self.fail_streak = 0
        self.halted = False
        self.last_quotes: dict[str, Quote] = {}
        self.last_fetch_minute: str | None = None   # 最近一次取数成功的分钟（"%Y-%m-%d %H:%M"）
        self.sentiment_label: str | None = None
        self.sentiment_trace: dict | None = None
        self._sentiment_at: datetime | None = None
        self._sold_minute: tuple[datetime, dict[str, Quote]] | None = None   # 本分钟卖出阶段取到的行情

    def tick(self, now: datetime) -> list[Fill]:
        """一分钟一轮：先卖后买（= sell_step + 不限时的 buy_step）。"""
        return self.sell_step(now) + self.buy_step(now)

    def sell_step(self, now: datetime) -> list[Fill]:
        """卖出阶段：取行情 + 按规则卖，不碰 Jev。行情留给同一分钟的 buy_step 用。"""
        self._sold_minute = None
        if self._rejected_day != now.date():
            self._restore_rejected(now.date())
            if self._sentiment_at is None:
                self._restore_sentiment(now.date())
        quotes = self._fetch(now)
        if quotes is None:
            return []
        fills = self._sells(quotes, now)
        self._sold_minute = (now, quotes)      # 卖出阶段完整走完才允许买（卖出出错 = 这一分钟不买，同旧 tick）
        return fills

    def buy_step(self, now: datetime, deadline: float | None = None) -> list[Fill]:
        """买入阶段：情绪 → 候选排序 → 影子 → 主账户（Jev）。必须在同一分钟的 sell_step 之后调用。
        deadline（time.monotonic 时刻）：过了就不再对新候选问 Jev；没问到的这一分钟不算判过，下一分钟照常。"""
        if self._sold_minute is None or self._sold_minute[0] != now:
            return []
        quotes = self._sold_minute[1]
        self._sold_minute = None               # 每分钟只买一轮
        mode = buy_mode(self._sentiment(now, quotes), self.overseas)
        if self.halted or not buy_window_open(mode, now.time()):
            return []
        ranked = rank_candidates(
            (q for c, q in quotes.items() if c in self.universe and q.ts.date() >= now.date()), self.ret5)
        fills = self._buy_shadow(ranked, now)
        fills += self._buy_main(ranked, now, mode, deadline)
        return fills

    def _restore_rejected(self, day: date):
        """重启后恢复当日已否决/出错的股票，避免重复问 Jev。"""
        rows = self.conn.execute(
            "SELECT DISTINCT code FROM decisions WHERE verdict IN ('reject', 'error') "
            "AND code != '' AND (strategy IS NULL OR strategy = 'lowbuy') "
            "AND substr(ts, 1, 10) = ?", (day.isoformat(),)).fetchall()
        self.rejected = {r[0] for r in rows}
        self._rejected_day = day

    def _restore_sentiment(self, day: date):
        """重启后恢复当日最后一次情绪标签，只给卖出阶段（自定义退出条件）一个合理值；
        _sentiment_at 不动，买入阶段照旧立即重新判断。"""
        try:
            row = self.conn.execute(
                "SELECT detail FROM decisions WHERE verdict='sentiment' AND (strategy IS NULL OR strategy='lowbuy') "
                "AND substr(ts, 1, 10) = ? ORDER BY ts DESC, rowid DESC LIMIT 1", (day.isoformat(),)).fetchone()
            label = json.loads(row[0]).get("label") if row else None
        except Exception as e:
            print(f"[engine] 恢复当日情绪失败: {e!r}")
            return
        self.sentiment_label = label if isinstance(label, str) else None

    def _fetch(self, now: datetime) -> dict[str, Quote] | None:
        held = {p.code for a in self.accounts_provider() for p in self.ledger.positions(a)}
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
        self.last_fetch_minute = now.strftime("%Y-%m-%d %H:%M")
        total = sum(q.amount for c, q in quotes.items() if c in self.universe)
        self.conn.execute("INSERT OR REPLACE INTO turnover VALUES (?, ?)",
                          (now.strftime("%Y-%m-%d %H:%M"), total))
        self.conn.commit()
        return quotes

    def _sells(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        fills = []
        stale_codes = set()
        for account in self.accounts:
            for pos in self.ledger.positions(account):
                q = quotes.get(pos.code)
                if q is None:
                    continue
                if q.ts.date() < now.date():
                    stale_codes.add(pos.code)
                    continue
                d = decide_sell(pos, q, now, self.stale_before)
                if d.sell and (f := self.ledger.sell(account, pos, q, now, d.reason)):
                    fills.append(f)
        if stale_codes:
            print(f"[engine] {now:%H:%M} 行情非当日，跳过卖出 {len(stale_codes)} 只")
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
            # 外围一起记下：面板试跑（后台 daemon 在交易、面板自己没跑时）从这一行读最近的情绪与外围
            self._log(now, "", "sentiment", json.dumps(
                {"stats": stats, "label": self.sentiment_label, "overseas": self.overseas}, ensure_ascii=False),
                trace=self.sentiment_trace)
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
        shadow = self.accounts[1]
        fills = []
        for q in ranked:
            if self.ledger.free_slots(shadow) <= 0:
                break
            if f := self.ledger.buy(shadow, q, now, f"影子｜低吸{q.pct}%｜5日{self.ret5[q.code]}%"):
                fills.append(f)
        return fills

    def _buy_main(self, ranked: list[Quote], now: datetime, mode: str,
                  deadline: float | None = None) -> list[Fill]:
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
            if deadline is not None and monotonic() >= deadline:
                break           # 本分钟 Jev 预算用完：剩下的不算判过（不记否决），下一分钟再问
            checked += 1
            verdict, why = self._check(self.notice_judge, q.code, now.date())
            notice_trace = getattr(self.notice_judge, "last_trace", None)
            news_trace = None
            if verdict == "pass":
                verdict, why = self._check(self.news_judge, q.code, q.name, now)
                news_trace = getattr(self.news_judge, "last_trace", None)
            trace = {"strategy": "lowbuy",
                     "candidate": {"name": q.name, "price": q.price, "pct": q.pct,
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
            f = self.ledger.buy(main, q, now, f"低吸{q.pct}%｜5日{self.ret5[q.code]}%｜{why}")
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
        self.conn.execute(
            "INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, ?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), code, verdict, detail,
             None if trace is None else json.dumps(trace, ensure_ascii=False), "lowbuy"))
        self.conn.commit()
