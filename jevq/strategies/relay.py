"""高标竞价接力（dj 2026-11-02）：昨天 3 板以上、今天竞价高开 3~6% 的主板票，开盘价买，下一交易日 14:55 后卖。

回测见 research/dragon_relay.py（2024-11 ~ 2026-10 日线：训练 +1.12%/笔、验证 +1.86%/笔，单笔最差 −23%）。
打首板输在「快板买不到」，这里改在集合竞价买——所有人同一个价，不拼速度。
主账户过 Jev 公告排雷 + 消息面，影子只按规则，对照排雷有没有用。只在开盘头几分钟买（成交价按开盘价记），
当天只买一轮；卖出只看时间，不碰 Jev。实盘不跟（regime 不跟这个账户）。
"""
import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, time
from time import monotonic

from jevq import config
from jevq.ledger import Ledger
from jevq.models import Fill, Quote
from jevq.rules.relay import decide_relay_sell, relay_candidates

SID = "relay"


class RelayStrategy:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, notice_judge, news_judge,
                 streaks: dict[str, int], trade_days: list[date] | None = None, can_buy: bool = True):
        self.conn, self.ledger = conn, ledger
        self.notice_judge, self.news_judge = notice_judge, news_judge
        self.streaks = streaks            # 截至上一交易日的连板数（closes.limit_streaks）
        self.calendar = sorted(trade_days) if trade_days else None
        self.can_buy = can_buy
        self.accounts = config.STRATEGIES[SID]["accounts"]
        self._done_day: date | None = None

    def sell_step(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        fills = []
        for account in self.accounts:
            for pos in self.ledger.positions(account):
                q = quotes.get(pos.code)
                if q is None or q.ts.date() < now.date():
                    continue
                d = decide_relay_sell(pos, q, now, self.calendar)
                if d.sell and (f := self.ledger.sell(account, pos, q, now, d.reason)):
                    fills.append(f)
        return fills

    def buy_step(self, quotes: dict[str, Quote], now: datetime, deadline: float | None = None) -> list[Fill]:
        if not quotes or not self.can_buy or self._done(now.date()):
            return []
        if now.time() >= time(*config.RELAY_BUY_UNTIL):
            self._done_day = now.date()          # 开盘头几分钟没跑上：今天不买，价格已经不是竞价价
            return []
        self._done_day = now.date()
        cands = relay_candidates(quotes, self.streaks, now.date())
        main, shadow = self.accounts
        fills = self._buy(shadow, [q for q, _ in cands][:config.RELAY_MAX_PER_DAY], now, "影子｜")
        passed = []
        for q, s in cands:
            if len(passed) >= config.RELAY_MAX_PER_DAY:
                break
            if deadline is not None and monotonic() >= deadline:
                break
            verdict, why = self._check(q, now)
            self._log(now, q.code, verdict, why, {"strategy": SID, "streak": s, "open_pct": q.open_pct})
            if verdict == "pass":
                passed.append(q)
        return fills + self._buy(main, passed, now, "")

    def _buy(self, account: str, picks: list[Quote], now: datetime, prefix: str) -> list[Fill]:
        fills = []
        for q in picks:
            at_open = replace(q, price=q.open)
            reason = f"{prefix}接力｜{self.streaks.get(q.code)}板｜竞价高开{q.open_pct:+.2f}%"
            if f := self.ledger.buy(account, at_open, now, reason, budget=config.RELAY_PER_TRADE):
                fills.append(f)
        return fills

    def _check(self, q: Quote, now: datetime) -> tuple[str, str]:
        for judge, args in ((self.notice_judge, (q.code, now.date())), (self.news_judge, (q.code, q.name, now))):
            try:
                verdict, why = judge.check(*args)
            except Exception as e:                       # noqa: BLE001
                return "error", f"判断异常：{e!r}"
            if verdict != "pass":
                return verdict, why
        return "pass", why

    def _done(self, day: date) -> bool:
        """当天已经买过一轮（含重启前）：看 fills 里有没有本策略今天的买入。"""
        if self._done_day == day:
            return True
        row = self.conn.execute(
            "SELECT 1 FROM fills WHERE side='buy' AND account IN (?, ?) AND substr(ts, 1, 10)=? LIMIT 1",
            (*self.accounts, day.isoformat())).fetchone()
        if row:
            self._done_day = day
        return bool(row)

    def _log(self, now: datetime, code: str, verdict: str, detail: str, trace: dict):
        self.conn.execute(
            "INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, ?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), code, verdict, detail, json.dumps(trace, ensure_ascii=False), SID))
        self.conn.commit()
