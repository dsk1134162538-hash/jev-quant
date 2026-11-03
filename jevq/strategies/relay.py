"""竞价买入类策略（开盘价买、下一交易日 14:55 后卖）：高标竞价接力、弱转强，共用买卖流程，各自提供选股和备注。

高标竞价接力（dj 2026-11-02）：昨天 3 板以上、今天竞价高开 3~6% 的主板票，开盘价买，下一交易日 14:55 后卖。

回测见 research/dragon_relay.py（2024-11 ~ 2026-10 日线：训练 +1.12%/笔、验证 +1.86%/笔，单笔最差 −23%）。
打首板输在「快板买不到」，这里改在集合竞价买——所有人同一个价，不拼速度。
弱转强（dj 2026-11-03，知识库）：昨天炸板、炸板前 ≥2 连板，今天竞价高开 0~6%，同样开盘价买、次日收盘卖（research/kb_events.py）。
主账户过 Jev 公告排雷 + 消息面，影子只按规则，对照排雷有没有用。只在开盘头几分钟买（成交价按开盘价记），
每个账户当天只买一轮（主账户 Jev 预算不够时下一分钟接着判）；卖出只看时间，不碰 Jev。实盘不跟（regime 不跟这个账户）。
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
from jevq.rules.themes import bz_candidates
from jevq.rules.wts import wts_candidates

SID = "relay"


class AuctionStrategy:
    """子类给 SID / PREFIX（config 里 {PREFIX}_PER_TRADE 等）、candidates()、tag()。"""
    SID = SID
    PREFIX = "RELAY"

    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, notice_judge, news_judge,
                 trade_days: list[date] | None = None, can_buy: bool = True):
        self.conn, self.ledger = conn, ledger
        self.notice_judge, self.news_judge = notice_judge, news_judge
        self.calendar = sorted(trade_days) if trade_days else None
        self.can_buy = can_buy
        self.accounts = config.STRATEGIES[self.SID]["accounts"]
        self._done_for: dict[str, date] = {}     # 账户 → 今天已买过一轮
        self._verdicts: dict[str, str] = {}      # 今天已判过的主账户候选（Jev 预算不够时跨分钟接着判）
        self._verdict_day: date | None = None

    def _cfg(self, name: str):
        return getattr(config, f"{self.PREFIX}_{name}")

    def candidates(self, quotes: dict[str, Quote], day: date) -> list[tuple[Quote, int]]:
        raise NotImplementedError

    def tag(self, q: Quote, n: int) -> str:
        raise NotImplementedError

    def sell_step(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        fills = []
        for account in self.accounts:
            for pos in self.ledger.positions(account):
                q = quotes.get(pos.code)
                if q is None or q.ts.date() < now.date():
                    continue
                d = decide_relay_sell(pos, q, now, self.calendar, sell_at=self._cfg("SELL_AT"))
                if d.sell and (f := self.ledger.sell(account, pos, q, now, d.reason)):
                    fills.append(f)
        return fills

    def buy_step(self, quotes: dict[str, Quote], now: datetime, deadline: float | None = None) -> list[Fill]:
        """开盘头几分钟：影子按规则买一轮；主账户逐只过 Jev，时间预算用完就下一分钟接着判（还在窗口内）。"""
        day = now.date()
        if not quotes or not self.can_buy or now.time() >= time(*self._cfg("BUY_UNTIL")):
            return []
        main, shadow = self.accounts
        todo = [a for a in (shadow, main) if not self._done(a, day)]
        if not todo:
            return []
        cands = self.candidates(quotes, day)
        fills = []
        if shadow in todo:
            fills += self._buy(shadow, [(q, n) for q, n in cands][:self._cfg("MAX_PER_DAY")], now, "影子｜")
            self._done_for[shadow] = day
        if main in todo:
            passed, finished = [], True
            for q, s in cands:
                if len(passed) >= self._cfg("MAX_PER_DAY"):
                    break
                if q.code not in self._verdicts:
                    if deadline is not None and monotonic() >= deadline:
                        finished = False
                        break
                    verdict, why = self._check(q, now)
                    self._verdicts[q.code] = verdict
                    self._log(now, q.code, verdict, why, {"strategy": self.SID, "streak": s, "open_pct": q.open_pct})
                if self._verdicts[q.code] == "pass":
                    passed.append((q, s))
            if finished:
                fills += self._buy(main, passed, now, "")
                self._done_for[main] = day
        return fills

    def _buy(self, account: str, picks: list[tuple[Quote, int]], now: datetime, prefix: str) -> list[Fill]:
        fills = []
        for q, n in picks:
            at_open = replace(q, price=q.open)
            reason = f"{prefix}{self.tag(q, n)}"
            if f := self.ledger.buy(account, at_open, now, reason, budget=self._cfg("PER_TRADE")):
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

    def _done(self, account: str, day: date) -> bool:
        """这个账户今天已经买过一轮：内存里记着，或重启前已有本账户今天的买入成交。"""
        if self._done_for.get(account) == day:
            return True
        if self._verdict_day != day:
            self._verdicts, self._verdict_day = {}, day
        row = self.conn.execute("SELECT 1 FROM fills WHERE side='buy' AND account=? AND substr(ts, 1, 10)=? LIMIT 1",
                                (account, day.isoformat())).fetchone()
        if row:
            self._done_for[account] = day
        return bool(row)

    def _log(self, now: datetime, code: str, verdict: str, detail: str, trace: dict):
        self.conn.execute(
            "INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, ?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), code, verdict, detail, json.dumps(trace, ensure_ascii=False), self.SID))
        self.conn.commit()


class RelayStrategy(AuctionStrategy):
    SID, PREFIX = "relay", "RELAY"

    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, notice_judge, news_judge,
                 streaks: dict[str, int], trade_days: list[date] | None = None, can_buy: bool = True):
        super().__init__(conn, ledger, notice_judge, news_judge, trade_days, can_buy)
        self.streaks = streaks            # 截至上一交易日的连板数（closes.limit_streaks）

    def candidates(self, quotes, day):
        return relay_candidates(quotes, self.streaks, day)

    def tag(self, q, n):
        return f"接力｜{n}板｜竞价高开{q.open_pct:+.2f}%"


class WtsStrategy(AuctionStrategy):
    SID, PREFIX = "wts", "WTS"

    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, notice_judge, news_judge,
                 pool: dict[str, int], trade_days: list[date] | None = None, can_buy: bool = True):
        super().__init__(conn, ledger, notice_judge, news_judge, trade_days, can_buy)
        self.pool = pool                  # 昨日炸板的高标：代码 → 炸板前连板数（jevq.data.wts_pool.build_pool）

    def candidates(self, quotes, day):
        return wts_candidates(quotes, self.pool, day)

    def tag(self, q, n):
        return f"弱转强｜前{n}连板炸板｜竞价高开{q.open_pct:+.2f}%"


class BzStrategy(AuctionStrategy):
    """补涨（dj 2026-11-03）：题材龙头 ≥5 板，同题材昨日首板，竞价高开 0~3% 买；买卖流程同接力 / 弱转强。"""
    SID, PREFIX = "bz", "BZ"

    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, notice_judge, news_judge,
                 pool: dict[str, dict], trade_days: list[date] | None = None, can_buy: bool = True):
        super().__init__(conn, ledger, notice_judge, news_judge, trade_days, can_buy)
        self.pool = pool                  # 代码 → {"theme", "leader"}（jevq.rules.themes.bz_pool）

    def candidates(self, quotes, day):
        return bz_candidates(quotes, self.pool, day)

    def tag(self, q, n):
        theme = (self.pool.get(q.code) or {}).get("theme", "")
        return f"补涨｜{theme} 龙头{n}板｜竞价高开{q.open_pct:+.2f}%"
