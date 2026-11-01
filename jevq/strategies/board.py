import json
from datetime import date, datetime
from time import monotonic

from jevq.models import Fill, Quote
from jevq.rules.board import (EXIT_PCT, SCAN_WINDOWS, VOLUME_MIN, WATCH_MAX, Candidate, in_scan_window,
                              same_time_ratio, scan_trigger, sentiment_gate, watch_candidates)
from jevq.rules.ai_board import decide_noloss_sell
from jevq.rules.board_sell import decide_board_sell

SEALED_SKIP = "快照已封死，视为排队未买到"
SHRINK_SKIP = "缩量板不扫"
GATE_PREFIX = "情绪闸门（只记录不拦截）："
AI_GATE_SKIP = "AI 闸门关"


class BoardStrategy:
    """扫首板：观察池维护 + Jev 留痕 + 3 秒扫板买点 + 卖出。卖出永远不依赖 Jev。"""

    id = "board"

    def __init__(self, conn, ledger, notice_judge, news_judge, fetch_quotes, universe: dict[str, str],
                 ret5: dict[str, float], concept_map: dict[str, list[str]],
                 prev_prev_closes: dict[str, float | None], prev_day: date,
                 accounts: tuple[str, str] = ("board_main", "board_shadow"),
                 windows: list = SCAN_WINDOWS, enabled: bool = True,
                 recent_limit_ups: set[str] = frozenset(), sectors_provider=None, fetch_day_minutes=None,
                 legacy_buy: bool = True, ai_accounts: tuple[str, str] | None = None, ai_gate=None,
                 ai_budget_pct: float = 1.0):
        self.conn, self.ledger = conn, ledger
        self.notice_judge, self.news_judge = notice_judge, news_judge
        self.fetch_quotes = fetch_quotes
        self.universe, self.ret5, self.concept_map = universe, ret5, concept_map
        self.prev_prev_closes, self.prev_day = prev_prev_closes, prev_day   # 前日收，首板判断用
        self.recent_limit_ups = recent_limit_ups   # 近 10 日涨停过的代码，扫首板龙头命中就跳过该题材
        self.accounts, self.windows, self.enabled = accounts, windows, enabled
        self.sectors_provider = sectors_provider   # () -> hi189 文章里的板块列表；None = 不用文章
        self.stale_before: date | None = prev_day
        self.watch: dict[str, Candidate] = {}
        self.verdicts: dict[str, tuple[str, str, dict]] = {}
        self.rejected: set[str] = set()
        self.sealed_logged: set[str] = set()     # 当日已记过「快照已封死」的票，每只每天只记一次
        self._rejected_day: date | None = None
        self._gate_day: date | None = None
        # 5 日分时（同时段放量倍数用）：None = 不看量能（测试默认）；code → {日期: {HHMM: 累计量}}，取不到记 {}
        self.fetch_day_minutes = fetch_day_minutes
        self._curves: dict[str, dict] = {}
        self.shrink_logged: set[str] = set()
        # 持有期内（次日）要发财报 / 大额解禁的不买（用户 2026-10-19），jevq.data.events.EventFilter；None = 不看。
        # 影子账户不看判断结果，照旧扫
        self.event_filter = None
        # AI 打首板（dj 2026-11-01）：同一个观察池和扫板买点，另开一对账户。主账户要过 Jev 公告/新闻 + DeepSeek 闸门
        # （ai_gate(code, now) -> (开不开, 说明)），影子只看扫板；两个都按单票不亏损卖。legacy_buy=False 时老账户只卖不买
        self.legacy_buy, self.ai_accounts, self.ai_gate = legacy_buy, ai_accounts, ai_gate
        self.ai_budget_pct = ai_budget_pct
        self.gate_logged: set[str] = set()

    def has_watch(self) -> bool:
        return bool(self.watch)

    def on_minute(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        """一分钟一轮 = sell_step + 不限时的 buy_step。"""
        return self.sell_step(quotes, now) + self.buy_step(quotes, now)

    def sell_step(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        """卖出阶段：只按规则卖，不碰 Jev。"""
        if self._rejected_day != now.date():
            self._restore_rejected(now.date())
        if not quotes:
            return []       # 这一分钟没拿到新行情：不卖（不能用旧价），也不动观察池
        return self._sells(quotes, now)

    def buy_step(self, quotes: dict[str, Quote], now: datetime, deadline: float | None = None) -> list[Fill]:
        """买入准备阶段：观察池维护 + 对未判断的票问 Jev（真正的买点在 on_fast）。
        deadline（time.monotonic 时刻）过了就不再判断新票；没判到的留在池里、没有结论，下一分钟再判。"""
        if self._rejected_day != now.date():
            self._restore_rejected(now.date())
        if not quotes or not self.enabled:
            return []
        if self._gate_day != now.date():
            self._note_gate(now)
        self._maintain_watch(quotes, now, deadline)
        return []

    def _note_gate(self, now: datetime):
        """每天第一次买入阶段记一条情绪闸门结论（试验：只留痕，买卖照旧），之后拿它和首板成交的盈亏对比。
        用上一交易日收盘前最后一次情绪统计；当天已经记过（重启）就不再记。出错只打印，不影响交易。"""
        self._gate_day = now.date()
        try:
            if self.conn.execute("SELECT 1 FROM decisions WHERE strategy='board' AND verdict='gate' "
                                 "AND substr(ts, 1, 10) = ?", (now.date().isoformat(),)).fetchone():
                return
            row = self.conn.execute(
                "SELECT detail FROM decisions WHERE verdict='sentiment' AND substr(ts, 1, 10) = ? "
                "ORDER BY ts DESC, rowid DESC LIMIT 1", (self.prev_day.isoformat(),)).fetchone()
            blocked, why = sentiment_gate(json.loads(row[0]).get("stats") if row else None)
            head = "无法判断" if blocked is None else ("会拦" if blocked else "放行")
            self._log(now, "", "gate", f"{GATE_PREFIX}{head}，{why}", None)
        except Exception as e:
            self.conn.rollback()
            print(f"[board] {now:%H:%M} 情绪闸门记录失败: {e!r}")

    def _restore_rejected(self, day: date):
        """重启后恢复当日已否决/出错的票，避免重复问 Jev（同 Engine._restore_rejected）；
        顺带恢复当日已记过「快照已封死」的票，重启后不重复记。"""
        rows = self.conn.execute(
            "SELECT DISTINCT code FROM decisions WHERE strategy='board' AND verdict IN ('reject', 'error') "
            "AND code != '' AND substr(ts, 1, 10) = ?", (day.isoformat(),)).fetchall()
        self.rejected = {r[0] for r in rows}
        rows = self.conn.execute(
            "SELECT DISTINCT code FROM decisions WHERE strategy='board' AND verdict='skip' AND detail=? "
            "AND substr(ts, 1, 10) = ?", (SEALED_SKIP, day.isoformat())).fetchall()
        self.sealed_logged = {r[0] for r in rows}
        self._rejected_day = day

    def on_fast(self, now: datetime) -> list[Fill]:
        if self._rejected_day != now.date():
            self._restore_rejected(now.date())
        if not self.watch:
            return []
        try:
            quotes = self.fetch_quotes(sorted(self.watch))
        except Exception as e:
            print(f"[board] {now:%H:%M} 快扫行情失败: {e!r}")
            return []
        fills = []
        main, shadow = self.accounts
        for code in list(self.watch):
            try:
                q = quotes.get(code)
                if q is None:
                    continue
                sealed = q.price >= q.limit_up
                triggered = scan_trigger(q, now, self.windows)
                entry = self.verdicts.get(code)
                ratio = self._volume_ratio(code, q, now) if triggered else None
                if triggered and ratio is not None and ratio < VOLUME_MIN:
                    self._note_shrink(code, q, now, entry, ratio)
                    triggered = False
                tag = f"扫板{q.pct}%" + (f"｜量{ratio:g}倍" if ratio is not None else "")

                try:
                    if self.legacy_buy and entry and entry[0] == "pass" and triggered \
                            and self.ledger.free_slots(main) > 0 \
                            and not self.ledger.holds(main, code):
                        if f := self.ledger.buy(main, q, now, tag):
                            fills.append(f)
                            trace = entry[2]
                            trace["outcome"] = {"bought": True, "shares": f.shares, "price": f.price}
                            trace["volume_ratio"] = ratio
                            detail = f"扫板 {q.pct}%" + (f"，同时段放量 {ratio:g} 倍" if ratio is not None else "")
                            self.verdicts[code] = ("pass", detail, trace)
                            self._log(now, code, "pass", detail, trace)
                except Exception as e:
                    self.conn.rollback()
                    print(f"[board] {now:%H:%M} 主账户扫板买入 {code} 出错: {e!r}")

                try:
                    if self.legacy_buy and triggered and self.ledger.free_slots(shadow) > 0 \
                            and not self.ledger.holds(shadow, code):
                        if f := self.ledger.buy(shadow, q, now, f"影子｜{tag}"):
                            fills.append(f)
                except Exception as e:
                    self.conn.rollback()
                    print(f"[board] {now:%H:%M} 影子账户扫板买入 {code} 出错: {e!r}")

                fills += self._ai_buys(code, q, now, entry, triggered, tag)

                if sealed and q.ts.date() >= now.date():
                    self._note_sealed(code, q, now, entry)
            except Exception as e:
                self.conn.rollback()
                print(f"[board] {now:%H:%M} 快扫 {code} 出错: {e!r}")
        return fills

    def _buyer_has_slot(self) -> bool:
        mains = ([self.accounts[0]] if self.legacy_buy else []) + ([self.ai_accounts[0]] if self.ai_accounts else [])
        return any(self.ledger.free_slots(a) > 0 for a in mains)

    def _ai_buys(self, code: str, q: Quote, now: datetime, entry, triggered: bool, tag: str) -> list[Fill]:
        if not self.ai_accounts or not triggered:
            return []
        fills = []
        main, shadow = self.ai_accounts
        try:
            if entry and entry[0] == "pass" and self.ledger.free_slots(main) > 0 and not self.ledger.holds(main, code):
                ok, why = self.ai_gate(code, now) if self.ai_gate else (False, "没有接 DeepSeek 闸门")
                if not ok:
                    if code not in self.gate_logged:
                        self.gate_logged.add(code)
                        self._log(now, code, "skip", f"{AI_GATE_SKIP}：{why}", entry[2])
                elif f := self.ledger.buy(main, q, now, f"AI｜{tag}｜{why}"[:120],
                                          budget=self.ledger.cash(main) * self.ai_budget_pct):
                    fills.append(f)
                    self._log(now, code, "pass", f"AI 打首板买入 {q.pct}%：{why}", entry[2])
        except Exception as e:
            self.conn.rollback()
            print(f"[board] {now:%H:%M} AI 主账户扫板买入 {code} 出错: {e!r}")
        try:
            if self.ledger.free_slots(shadow) > 0 and not self.ledger.holds(shadow, code):
                if f := self.ledger.buy(shadow, q, now, f"AI影子｜{tag}",
                                        budget=self.ledger.cash(shadow) * self.ai_budget_pct):
                    fills.append(f)
        except Exception as e:
            self.conn.rollback()
            print(f"[board] {now:%H:%M} AI 影子账户扫板买入 {code} 出错: {e!r}")
        return fills

    def _sells(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        fills = []
        ai = tuple(self.ai_accounts or ())
        for account in (*self.accounts, *ai):
            for pos in self.ledger.positions(account):
                try:
                    q = quotes.get(pos.code)
                    if q is None or q.ts.date() < now.date():
                        continue
                    d = decide_noloss_sell(pos, q, now, self.prev_day) if account in ai \
                        else decide_board_sell(pos, q, now, self.stale_before)
                    if d.sell and (f := self.ledger.sell(account, pos, q, now, d.reason)):
                        fills.append(f)
                except Exception as e:
                    self.conn.rollback()
                    print(f"[board] {now:%H:%M} 卖出 {account}/{pos.code} 出错: {e!r}")
        return fills

    def _maintain_watch(self, quotes: dict[str, Quote], now: datetime, deadline: float | None = None):
        today = now.date()
        for code in list(self.watch):
            q = quotes.get(code)
            if q is None or q.ts.date() < today or q.pct < EXIT_PCT:
                self.watch.pop(code, None)
                self.verdicts.pop(code, None)
        if len(self.watch) < WATCH_MAX:
            uni_quotes = {c: q for c, q in quotes.items() if c in self.universe}
            for cand in watch_candidates(uni_quotes, self.concept_map, self.ret5, self.prev_prev_closes, today,
                                         self.recent_limit_ups, article_hot=self._article_hot()):
                if len(self.watch) >= WATCH_MAX:
                    break
                if cand.code in self.watch or cand.code in self.rejected:
                    continue
                self.watch[cand.code] = cand
                self._prefetch_curve(cand.code)
        # 未判断过的观察池票：扫板窗口内、主账户有空位才补判断（无论是新进的、之前满仓时跳过的，
        # 还是窗口外进池的）。窗口外买不了，问 Jev 只是白花调用；进窗口的第一分钟会补上。
        if in_scan_window(now.time(), self.windows) and self._buyer_has_slot():
            for code, cand in list(self.watch.items()):
                if code in self.verdicts:
                    continue
                q = quotes.get(code)
                if q is None:
                    continue
                if deadline is not None and monotonic() >= deadline:
                    break       # 本分钟 Jev 预算用完：剩下的没有结论（不进否决集），下一分钟再判
                try:
                    self._judge(cand, q, now)
                except Exception as e:
                    self.conn.rollback()
                    print(f"[board] {now:%H:%M} 判断 {code} 出错: {e!r}")

    def _prefetch_curve(self, code: str):
        """进观察池时先把前几天的分时取好，扫板那一刻不再联网。取不到记 {}（到时候不判断量能，照常扫）。"""
        if self.fetch_day_minutes is None or code in self._curves:
            return
        try:
            self._curves[code] = self.fetch_day_minutes(code) or {}
        except Exception as e:
            print(f"[board] 取 {code} 5 日分时失败: {e!r}")
            self._curves[code] = {}

    def _volume_ratio(self, code: str, q: Quote, now: datetime) -> float | None:
        if self.fetch_day_minutes is None:
            return None
        self._prefetch_curve(code)
        return same_time_ratio(q.volume, self._curves.get(code) or {}, now.date().isoformat(), now.strftime("%H%M"))

    def _note_shrink(self, code: str, q: Quote, now: datetime, entry, ratio: float):
        """涨到扫板线但同时段缩量：不扫，记一条 skip（每只每天一次）。"""
        if code in self.shrink_logged:
            return
        cand = self.watch.get(code) or Candidate(code, "", 0, 0, {})
        trace = entry[2] if entry is not None else self._trace(cand, q)
        trace["volume_ratio"] = ratio
        self._log(now, code, "skip", f"{SHRINK_SKIP}：同时段 {ratio:g} 倍（< {VOLUME_MIN:g}）", trace)
        self.shrink_logged.add(code)

    def candidates_view(self, quotes: dict[str, Quote]) -> list[dict]:
        """面板「候选」页：当前观察池按优先顺序（文章点名 → 热度 → 龙头分，同 watch_candidates）。"""
        out = []
        for cand in sorted(self.watch.values(), key=lambda c: (not c.article_hot, -c.heat, c.score)):
            q = quotes.get(cand.code)
            verdict = self.verdicts.get(cand.code)
            out.append({"rank": len(out) + 1, "code": cand.code, "name": self.universe.get(cand.code, ""),
                        "concept": cand.concept, "heat": cand.heat,
                        "pct": None if q is None else q.pct, "price": None if q is None else q.price,
                        "article_hot": cand.article_hot,
                        "verdict": verdict[0] if verdict else "", "why": verdict[1] if verdict else ""})
        return out

    def _article_hot(self):
        """文章点名判定函数；没有文章或取板块出错 → None（顺序不变）。"""
        try:
            sectors = self.sectors_provider() if self.sectors_provider else None
        except Exception as e:
            print(f"[board] 读取文章板块出错: {e!r}")
            return None
        if not sectors:
            return None
        from jevq.data.hi189 import article_hot
        return lambda concept, members: article_hot(concept, members, self.universe, sectors)

    def _judge(self, cand: Candidate, q: Quote, now: datetime):
        event = None if self.event_filter is None else self.event_filter.blocked(cand.code, 1)
        if event is not None:
            verdict, why, notice_trace = "reject", event, None
        else:
            verdict, why = self._check(self.notice_judge, cand.code, now.date())
            notice_trace = getattr(self.notice_judge, "last_trace", None)
        news_trace = None
        if verdict == "pass":
            verdict, why = self._check(self.news_judge, cand.code, q.name, now)
            news_trace = getattr(self.news_judge, "last_trace", None)
        trace = self._trace(cand, q)
        trace["notices"], trace["news"] = notice_trace, news_trace
        self.verdicts[cand.code] = (verdict, why, trace)
        self._log(now, cand.code, verdict, why, trace)
        if verdict in ("reject", "error"):
            self.watch.pop(cand.code, None)
            self.verdicts.pop(cand.code, None)
            self.rejected.add(cand.code)

    def _trace(self, cand: Candidate, q: Quote) -> dict:
        return {"strategy": "board",
                "candidate": {"name": q.name, "price": q.price, "pct": q.pct,
                              "ret5": self.ret5.get(cand.code), "concept": cand.concept,
                              "heat": cand.heat, "score": cand.score, "ranks": cand.ranks,
                              "article_hot": bool(cand.article_hot), "article_hot_why": cand.article_hot},
                "notices": None, "news": None, "sealed_at_snapshot": False,
                "outcome": {"bought": False, "shares": None, "price": None}}

    def _note_sealed(self, code: str, q: Quote, now: datetime, entry):
        """快扫时已封死涨停：视为排队没买到。标到留痕里，并记一条 skip 决策（每只每天一次）方便复盘。"""
        if entry is not None:
            entry[2]["sealed_at_snapshot"] = True
        if code in self.sealed_logged:
            return
        if entry is not None:
            trace = entry[2]
        else:
            cand = self.watch.get(code) or Candidate(code, "", 0, 0, {})
            trace = self._trace(cand, q)
            trace["sealed_at_snapshot"] = True
        self._log(now, code, "skip", SEALED_SKIP, trace)
        self.sealed_logged.add(code)

    @staticmethod
    def _check(judge, *args) -> tuple[str, str]:
        try:
            return judge.check(*args)
        except Exception as e:
            return "error", f"判断异常：{e!r}"

    def _log(self, now: datetime, code: str, verdict: str, detail: str, trace: dict | None):
        self.conn.execute(
            "INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, ?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), code, verdict, detail,
             None if trace is None else json.dumps(trace, ensure_ascii=False), "board"))
        self.conn.commit()
