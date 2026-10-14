"""超跌反弹：近 5 日跌了 0~20%、近两个月没连板过的票，当日回调到 0~-3% 区间时买入赌反弹。

买入时机只看情绪（正常/火热盘中买，偏弱/冰点不买）；卖出见 rules.rebound_sell（相对买入价止损、
跟踪止盈、最多拿 2 个交易日）。主账户（实盘跟的）止损要连续 2 分钟确认，止损/低开反抽前问 Jev 今天走向
（trend_judge），影子账户照旧规则当对照组。
行情与情绪都由共用的 Engine 每分钟取好、刷新好传进来，本策略不自己拉行情、不自己问情绪。
"""
import json
import sqlite3
from collections import deque
from collections.abc import Callable
from datetime import date, datetime
from time import monotonic

from jevq import config
from jevq.config import (MAX_CHECKS_PER_TICK, MAX_ERRORS_PER_TICK, MAX_PER_TRADE, REBOUND_FLOW_MINUTES,
                         STRATEGIES)
from jevq.ledger import Ledger, affordable_shares
from jevq.models import Fill, Quote
from jevq.rules.buy import buy_window_open, rank_rebound, rebound_mode
from jevq.rules.metrics import shrink_days
from jevq.rules.rebound_sell import decide_rebound_sell

SID = "rebound"


class ReboundStrategy:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, notice_judge, news_judge,
                 universe: dict[str, str], ret5: dict[str, float], excluded: set[str], prev_day: date,
                 sentiment_provider: Callable[[], str | None], enabled: bool = True,
                 sentiment_trace_provider: Callable[[], dict | None] | None = None,
                 trade_days: list[date] | None = None, features: dict[str, dict] | None = None,
                 trend_judge=None, turn_provider: Callable[[], bool] | None = None):
        self.conn, self.ledger = conn, ledger
        # 今天是否已确认情绪拐点（Engine.turn_on）：是 → 偏弱/冰点也盘中买（拐点抄底）
        self.turn_provider = turn_provider or (lambda: False)
        # 问 Jev 持仓票今天走向（jevq.judge.trend.TrendJudge）；None = 主账户止损只做 2 分钟确认、不问走向
        self.trend_judge = trend_judge
        # (账户, 代码) → (最近一次计数的分钟, 现价连续在止损线下的分钟数)
        self._below_stop: dict[tuple[str, str], tuple[str, int]] = {}
        # 历史成交量特征（history_features 的 vols/vols_until），成交量条件用；None = 不看成交量（只有测试不传）
        self.features = features
        self.accounts = STRATEGIES[SID]["accounts"]
        self.notice_judge, self.news_judge = notice_judge, news_judge
        self.universe, self.ret5, self.excluded = universe, ret5, excluded
        self.sentiment_provider = sentiment_provider
        self.sentiment_trace_provider = sentiment_trace_provider or (lambda: None)
        self.enabled = enabled
        self.calendar = sorted(trade_days) if trade_days else None     # 交易日历；None = 不可用
        self.rejected: set[str] = set()
        self._rejected_day: date | None = None
        # 持仓票每分钟的 (分钟, 外盘, 内盘) 快照，用来算最近几分钟的主买/主卖增量
        self._flow: dict[str, deque] = {}

    def on_minute(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        """先卖后买（= sell_step + 不限时的 buy_step）。"""
        return self.sell_step(quotes, now) + self.buy_step(quotes, now)

    def sell_step(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        """卖出阶段：纯规则，不碰 Jev。没有行情这一分钟不卖。"""
        if self._rejected_day != now.date():
            self._restore_rejected(now.date())
        if not quotes:
            return []
        return self._sells(quotes, now)

    def buy_step(self, quotes: dict[str, Quote], now: datetime, deadline: float | None = None) -> list[Fill]:
        """买入阶段。deadline（time.monotonic 时刻）过了就不再对新候选问 Jev；没问到的不算判过，下一分钟照常。"""
        if self._rejected_day != now.date():
            self._restore_rejected(now.date())
        if not quotes or not self.enabled:
            return []
        sentiment = self.sentiment_provider()
        mode = rebound_mode(sentiment, bool(self.turn_provider()))
        if not buy_window_open(mode, now.time()):
            return []
        ranked = rank_rebound(
            (q for c, q in quotes.items() if c in self.universe and q.ts.date() >= now.date()),
            self.ret5, self.excluded,
            shrink_days=None if self.features is None else (lambda q: self._shrink_days(q, now)))
        # 主账户只买 5 日跌幅够深的（用户 2026-10-14）；影子账户照旧，当对照组
        deep = [q for q in ranked if self.ret5[q.code] <= config.REBOUND_MAIN_RET5_MAX]
        return self._buy_shadow(ranked, now) + self._buy_main(deep, now, mode, sentiment, deadline)

    def candidates_view(self, quotes: dict[str, Quote], now: datetime) -> dict:
        """面板「候选」页：此刻满足选股条件的票，按买入优先顺序（同 buy_step 的 rank_rebound）。
        不看买入时段：没到买点时也列出来，window_open 标明现在能不能买。"""
        sentiment = self.sentiment_provider()
        mode = rebound_mode(sentiment, bool(self.turn_provider()))
        info = {"sentiment": sentiment, "mode": mode, "window_open": buy_window_open(mode, now.time()),
                "enabled": self.enabled, "items": []}
        if not self.enabled or not quotes:
            return info
        ranked = rank_rebound(
            (q for c, q in quotes.items() if c in self.universe and q.ts.date() >= now.date()),
            self.ret5, self.excluded,
            shrink_days=None if self.features is None else (lambda q: self._shrink_days(q, now)))
        verdicts = {code: (v, d) for code, v, d in self.conn.execute(
            "SELECT code, verdict, detail FROM decisions WHERE strategy = ? AND code != '' "
            "AND substr(ts, 1, 10) = ? ORDER BY id", (SID, now.date().isoformat()))}
        for rank, q in enumerate(ranked[:30], 1):
            v = verdicts.get(q.code)
            info["items"].append({"rank": rank, "code": q.code, "name": q.name or self.universe.get(q.code, ""),
                                  "pct": q.pct, "price": q.price, "ret5": self.ret5.get(q.code),
                                  "shrink_days": self._shrink_days(q, now),
                                  "verdict": v[0] if v else "", "why": v[1] if v else ""})
        info["total"] = len(ranked)
        return info

    def _shrink_days(self, q: Quote, now: datetime) -> int | None:
        """这只票连续缩量几天（含今天）；没有量能历史/今天的量还不能折算 → None。"""
        if self.features is None:
            return None
        return shrink_days(self.features.get(q.code) or {}, q, now.time())

    def _rebuy_blocked(self, account: str, q: Quote, now: datetime) -> bool:
        """今天在这个账户里卖出过这只票（取最近一次卖出价），现价没比它低 REBOUND_REBUY_DROP% 以上 → 不买回。
        从成交记录里查，重启不丢；只管当天，隔天卖出的票不受限。"""
        row = self.conn.execute(
            "SELECT price FROM fills WHERE account = ? AND code = ? AND side = 'sell' AND substr(ts, 1, 10) = ? "
            "ORDER BY ts DESC, id DESC LIMIT 1", (account, q.code, now.date().isoformat())).fetchone()
        return row is not None and q.price > round(row[0] * (1 - config.REBOUND_REBUY_DROP / 100), 2)

    def _restore_rejected(self, day: date):
        """重启后恢复当日已否决/出错的票，避免重复问 Jev。"""
        rows = self.conn.execute(
            "SELECT DISTINCT code FROM decisions WHERE verdict IN ('reject', 'error') AND code != '' "
            "AND strategy = ? AND substr(ts, 1, 10) = ?", (SID, day.isoformat())).fetchall()
        self.rejected = {r[0] for r in rows}
        self._rejected_day = day

    def _sells(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        fills = []
        main = self.accounts[0]
        for account in self.accounts:
            for pos in self.ledger.positions(account):
                q = quotes.get(pos.code)
                if q is None or q.ts.date() < now.date():
                    continue
                extra = {}
                if account == main:      # 影子账户不传：旧规则，当对照组
                    asked: list[bool] = []
                    extra = {"stop_minutes": self._stop_minutes(account, pos.buy_price, q, now),
                             "trend": None if self.trend_judge is None
                             else (lambda pos=pos, q=q, asked=asked: asked.append(True) or self._trend(pos, q, now)),
                             "pullback": config.REBOUND_MAIN_TRAIL_PULLBACK}
                d = decide_rebound_sell(pos, q, now, self._hold_days(pos.buy_date, now.date()),
                                        buy_pressure=self._buy_pressure(q, now), **extra)
                if account == main and extra.get("trend") is not None and asked:
                    self._log(now, pos.code, "trend", d.reason, {"strategy": SID, "account": account,
                                                                 "trend": self.trend_judge.last_trace})
                if d.sell and (f := self.ledger.sell(account, pos, q, now, d.reason)):
                    fills.append(f)
        return fills

    def _stop_minutes(self, account: str, buy_price: float, q: Quote, now: datetime) -> int:
        """现价连续几分钟（含这一分钟）在止损线下；同一分钟重复调用不重复计数，回到线上清零。
        只在内存里：重启后从 0 重新数（多等一两分钟，不会提前卖）。"""
        minute = now.strftime("%Y-%m-%d %H:%M")
        key = (account, q.code)
        last, n = self._below_stop.get(key, ("", 0))
        if last == minute:
            return n
        below = round((q.price / buy_price - 1) * 100, 2) <= config.REBOUND_STOP
        n = n + 1 if below else 0
        self._below_stop[key] = (minute, n)
        return n

    def _trend(self, pos, q: Quote, now: datetime) -> str | None:
        try:
            return self.trend_judge.judge(pos, q, now)
        except Exception as e:
            self.trend_judge.last_trace = {"label": None, "note": f"走向判断异常：{e!r}"}
            return None

    def _buy_pressure(self, q: Quote, now: datetime) -> bool:
        """最近 REBOUND_FLOW_MINUTES 分钟外盘（主动买）增量是否大于内盘（主动卖）增量。
        外盘/内盘是全天累计值，这里记每分钟快照取差值。快照不够两个（刚开盘/刚重启）或数据异常 → False（照常卖）。"""
        minute = now.strftime("%Y-%m-%d %H:%M")
        snaps = self._flow.setdefault(q.code, deque(maxlen=REBOUND_FLOW_MINUTES + 1))
        if not snaps or snaps[-1][0] != minute:
            if snaps and snaps[-1][0][:10] != minute[:10]:
                snaps.clear()
            snaps.append((minute, q.outer_vol, q.inner_vol))
        if len(snaps) < 2:
            return False
        d_out, d_in = snaps[-1][1] - snaps[0][1], snaps[-1][2] - snaps[0][2]
        if d_out < 0 or d_in < 0:
            return False
        return d_out > d_in

    def _hold_days(self, buy_date: date, today: date) -> int | None:
        """持有交易日数：日历里晚于买入日、不晚于今天的天数。日历不可用返回 None（卖出规则当天尾盘清仓）。"""
        if self.calendar is None:
            return None
        return sum(1 for d in self.calendar if buy_date < d <= today)

    def _buy_shadow(self, ranked: list[Quote], now: datetime) -> list[Fill]:
        shadow = self.accounts[1]
        fills = []
        for q in ranked:
            if self.ledger.free_slots(shadow) <= 0:
                break
            if self._rebuy_blocked(shadow, q, now):
                continue
            if f := self.ledger.buy(shadow, q, now, f"影子｜超跌{q.pct}%｜5日{self.ret5[q.code]}%"):
                fills.append(f)
        return fills

    def _buy_main(self, ranked: list[Quote], now: datetime, mode: str, sentiment: str | None,
                  deadline: float | None = None) -> list[Fill]:
        main = self.accounts[0]
        fills, checked, errors = [], 0, 0
        for rank, q in enumerate(ranked, 1):
            if self.ledger.free_slots(main) <= 0 or checked >= MAX_CHECKS_PER_TICK:
                break
            if q.code in self.rejected or self.ledger.holds(main, q.code) or self._rebuy_blocked(main, q, now):
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
            trace = {"strategy": SID,
                     "candidate": {"name": q.name, "price": q.price, "pct": q.pct,
                                   "ret5": self.ret5[q.code], "shrink_days": self._shrink_days(q, now),
                                   "rank": rank, "of": len(ranked)},
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
            tag = "拐点抄底｜" if mode == "turn" else ""
            f = self.ledger.buy(main, q, now, f"{tag}超跌{q.pct}%｜5日{self.ret5[q.code]}%｜{why}")
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
