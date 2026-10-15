"""低吸（9-23 版规则，2026-10-09 重新上线）：当日回调到 0~-3% 的强势股（近 5 日涨幅高的优先），次日冲高就走。
规则见 jevq.rules.lowbuy。主账户经 Jev 公告排雷 + 消息面，影子账户不拦截。情绪轮动（regime）在偏弱、
正常（10:30 后）时跟它主账户的成交。行情与情绪由共用的 Engine 每分钟传进来。
"""
import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from time import monotonic

from jevq import config
from jevq.config import MAX_CHECKS_PER_TICK, MAX_ERRORS_PER_TICK, MAX_PER_TRADE, STRATEGIES
from jevq.ledger import Ledger, affordable_shares
from jevq.models import Fill, Quote
from jevq.rules.buy import buy_window_open
from jevq.rules.board import mainline_concepts
from jevq.rules.lowbuy import decide_lowbuy_sell, lowbuy_mode, rank_lowbuy
from jevq.rules.metrics import shrink_days

SID = "lowbuy"


class LowbuyStrategy:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, notice_judge, news_judge,
                 universe: dict[str, str], ret5: dict[str, float], prev_day: date,
                 sentiment_provider: Callable[[], str | None], overseas_provider: Callable[[], str | None],
                 enabled: bool = True, features: dict[str, dict] | None = None,
                 concept_map: dict[str, list[str]] | None = None, sectors_provider=None,
                 trade_days: list[date] | None = None):
        self.conn, self.ledger = conn, ledger
        self.accounts = STRATEGIES[SID]["accounts"]
        self.notice_judge, self.news_judge = notice_judge, news_judge
        self.universe, self.ret5, self.prev_day = universe, ret5, prev_day
        self.sentiment_provider, self.overseas_provider = sentiment_provider, overseas_provider
        self.enabled = enabled
        self.rejected: set[str] = set()
        self._rejected_day: date | None = None
        # 缩量回调、主线两条选股条件的数据：None = 不看这一条（只有测试不传）
        self.features, self.concept_map = features, concept_map
        self.sectors_provider = sectors_provider     # () -> hi189 文章板块，主线判断「文章点名」用
        self.calendar = sorted(trade_days) if trade_days else None   # 交易日历；None = 主账户也只拿 1 天

    def sell_step(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        if self._rejected_day != now.date():
            self._restore_rejected(now.date())
        fills = []
        for account in self.accounts:
            for pos in self.ledger.positions(account):
                q = quotes.get(pos.code)
                if q is None or q.ts.date() < now.date():
                    continue
                if account == self.accounts[0]:      # 主账户：回落线放宽（10-14）、拿 2 天且低开反抽不卖（10-15）
                    d = decide_lowbuy_sell(pos, q, now, self.prev_day, pullback=config.LOWBUY_MAIN_TRAIL_PULLBACK,
                                           hold_days=self._hold_days(pos.buy_date, now.date()),
                                           max_hold=config.LOWBUY_MAIN_MAX_HOLD_DAYS,
                                           low_open_sell=config.LOWBUY_MAIN_LOW_OPEN_SELL)
                else:                                # 影子：原规则当对照
                    d = decide_lowbuy_sell(pos, q, now, self.prev_day)
                if d.sell and (f := self.ledger.sell(account, pos, q, now, d.reason)):
                    fills.append(f)
        return fills

    def buy_step(self, quotes: dict[str, Quote], now: datetime, deadline: float | None = None) -> list[Fill]:
        if self._rejected_day != now.date():
            self._restore_rejected(now.date())
        if not quotes or not self.enabled:
            return []
        sentiment = self.sentiment_provider()
        mode = lowbuy_mode(sentiment, self.overseas_provider())
        if not buy_window_open(mode, now.time()):
            return []
        today_quotes = {c: q for c, q in quotes.items() if c in self.universe and q.ts.date() >= now.date()}
        ranked = rank_lowbuy(today_quotes.values(), self.ret5,
                             shrink=None if self.features is None else (
                                 lambda q: shrink_days(self.features.get(q.code) or {}, q, now.time())),
                             mainline=self._mainline(today_quotes, now))
        return self._buy_shadow(ranked, now) + self._buy_main(ranked, now, mode, sentiment, deadline)

    def _hold_days(self, buy_date: date, today: date) -> int | None:
        if self.calendar is None:
            return None
        return sum(1 for d in self.calendar if buy_date < d <= today)

    def on_minute(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        return self.sell_step(quotes, now) + self.buy_step(quotes, now)

    def _mainline(self, quotes: dict[str, Quote], now: datetime):
        """今天主线题材的成员判定函数；没有题材表 → None（不看这一条）。"""
        if self.concept_map is None:
            return None
        article_hot = None
        try:
            sectors = self.sectors_provider() if self.sectors_provider else None
            if sectors:
                from jevq.data.hi189 import article_hot as hot
                article_hot = lambda concept, members: hot(concept, members, self.universe, sectors)   # noqa: E731
        except Exception as e:
            print(f"[lowbuy] 读取文章板块出错: {e!r}")
        lines = mainline_concepts(quotes, self.concept_map, now.date(), article_hot)
        return lambda code: any(c in lines for c in self.concept_map.get(code, []))

    def _restore_rejected(self, day: date):
        rows = self.conn.execute(
            "SELECT DISTINCT code FROM decisions WHERE verdict IN ('reject', 'error') AND code != '' "
            "AND strategy = ? AND substr(ts, 1, 10) = ?", (SID, day.isoformat())).fetchall()
        self.rejected = {r[0] for r in rows}
        self._rejected_day = day

    def _buy_shadow(self, ranked: list[Quote], now: datetime) -> list[Fill]:
        shadow = self.accounts[1]
        fills = []
        for q in ranked:
            if self.ledger.free_slots(shadow) <= 0:
                break
            if f := self.ledger.buy(shadow, q, now, f"影子｜低吸{q.pct}%｜5日{self.ret5[q.code]}%"):
                fills.append(f)
        return fills

    def _buy_main(self, ranked: list[Quote], now: datetime, mode: str, sentiment: str | None,
                  deadline: float | None) -> list[Fill]:
        main = self.accounts[0]
        fills, checked, errors = [], 0, 0
        for rank, q in enumerate(ranked, 1):
            if self.ledger.free_slots(main) <= 0 or checked >= MAX_CHECKS_PER_TICK or errors >= MAX_ERRORS_PER_TICK:
                break
            if q.code in self.rejected or self.ledger.holds(main, q.code):
                continue
            if affordable_shares(min(self.ledger.cash(main), MAX_PER_TRADE), q.price) == 0:
                continue
            if deadline is not None and monotonic() >= deadline:
                break
            checked += 1
            verdict, why = self._check(self.notice_judge, q.code, now.date())
            notice_trace = getattr(self.notice_judge, "last_trace", None)
            news_trace = None
            if verdict == "pass":
                verdict, why = self._check(self.news_judge, q.code, q.name, now)
                news_trace = getattr(self.news_judge, "last_trace", None)
            trace = {"strategy": SID,
                     "candidate": {"name": q.name, "price": q.price, "pct": q.pct, "ret5": self.ret5[q.code],
                                   "rank": rank, "of": len(ranked)},
                     "notices": notice_trace, "news": news_trace,
                     "market": {"mode": mode, "sentiment": sentiment},
                     "outcome": {"bought": False, "shares": None, "price": None}}
            if verdict in ("reject", "error"):
                self._log(now, q.code, verdict, why, trace)
                self.rejected.add(q.code)
                errors += verdict == "error"
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

    def _log(self, now: datetime, code: str, verdict: str, detail: str, trace: dict):
        self.conn.execute(
            "INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, ?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), code, verdict, detail, json.dumps(trace, ensure_ascii=False), SID))
        self.conn.commit()
