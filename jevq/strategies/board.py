import json
from datetime import date, datetime

from jevq.models import Fill, Quote
from jevq.rules.board import EXIT_PCT, SCAN_WINDOWS, WATCH_MAX, Candidate, scan_trigger, watch_candidates
from jevq.rules.board_sell import decide_board_sell


class BoardStrategy:
    """扫首板：观察池维护 + Jev 留痕 + 3 秒扫板买点 + 卖出。卖出永远不依赖 Jev。"""

    id = "board"

    def __init__(self, conn, ledger, notice_judge, news_judge, fetch_quotes, universe: dict[str, str],
                 ret5: dict[str, float], concept_map: dict[str, list[str]],
                 prev_prev_closes: dict[str, float | None], prev_day: date,
                 accounts: tuple[str, str] = ("board_main", "board_shadow"),
                 windows: list = SCAN_WINDOWS, enabled: bool = True):
        self.conn, self.ledger = conn, ledger
        self.notice_judge, self.news_judge = notice_judge, news_judge
        self.fetch_quotes = fetch_quotes
        self.universe, self.ret5, self.concept_map = universe, ret5, concept_map
        self.prev_prev_closes, self.prev_day = prev_prev_closes, prev_day   # 前日收，首板判断用
        self.accounts, self.windows, self.enabled = accounts, windows, enabled
        self.stale_before: date | None = prev_day
        self.watch: dict[str, Candidate] = {}
        self.verdicts: dict[str, tuple[str, str, dict]] = {}
        self.rejected: set[str] = set()
        self._rejected_day: date | None = None

    def has_watch(self) -> bool:
        return bool(self.watch)

    def on_minute(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        if self._rejected_day != now.date():
            self._restore_rejected(now.date())
        if not quotes:
            return []       # 这一分钟没拿到新行情：不卖（不能用旧价），也不动观察池
        fills = self._sells(quotes, now)
        if not self.enabled:
            return fills
        self._maintain_watch(quotes, now)
        return fills

    def _restore_rejected(self, day: date):
        """重启后恢复当日已否决/出错的票，避免重复问 Jev（同 Engine._restore_rejected）。"""
        rows = self.conn.execute(
            "SELECT DISTINCT code FROM decisions WHERE strategy='board' AND verdict IN ('reject', 'error') "
            "AND code != '' AND substr(ts, 1, 10) = ?", (day.isoformat(),)).fetchall()
        self.rejected = {r[0] for r in rows}
        self._rejected_day = day

    def on_fast(self, now: datetime) -> list[Fill]:
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

                try:
                    if entry and entry[0] == "pass" and triggered and self.ledger.free_slots(main) > 0 \
                            and not self.ledger.holds(main, code):
                        if f := self.ledger.buy(main, q, now, f"扫板{q.pct}%"):
                            fills.append(f)
                            trace = entry[2]
                            trace["outcome"] = {"bought": True, "shares": f.shares, "price": f.price}
                            detail = f"扫板 {q.pct}%"
                            self.verdicts[code] = ("pass", detail, trace)
                            self._log(now, code, "pass", detail, trace)
                except Exception as e:
                    print(f"[board] {now:%H:%M} 主账户扫板买入 {code} 出错: {e!r}")

                try:
                    if triggered and self.ledger.free_slots(shadow) > 0 and not self.ledger.holds(shadow, code):
                        if f := self.ledger.buy(shadow, q, now, f"影子｜扫板{q.pct}%"):
                            fills.append(f)
                except Exception as e:
                    print(f"[board] {now:%H:%M} 影子账户扫板买入 {code} 出错: {e!r}")

                if sealed and entry is not None:
                    entry[2]["sealed_at_snapshot"] = True
            except Exception as e:
                print(f"[board] {now:%H:%M} 快扫 {code} 出错: {e!r}")
        return fills

    def _sells(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        fills = []
        for account in self.accounts:
            for pos in self.ledger.positions(account):
                try:
                    q = quotes.get(pos.code)
                    if q is None or q.ts.date() < now.date():
                        continue
                    d = decide_board_sell(pos, q, now, self.stale_before)
                    if d.sell and (f := self.ledger.sell(account, pos, q, now, d.reason)):
                        fills.append(f)
                except Exception as e:
                    print(f"[board] {now:%H:%M} 卖出 {account}/{pos.code} 出错: {e!r}")
        return fills

    def _maintain_watch(self, quotes: dict[str, Quote], now: datetime):
        today = now.date()
        for code in list(self.watch):
            q = quotes.get(code)
            if q is None or q.ts.date() < today or q.pct < EXIT_PCT:
                self.watch.pop(code, None)
                self.verdicts.pop(code, None)
        if len(self.watch) < WATCH_MAX:
            uni_quotes = {c: q for c, q in quotes.items() if c in self.universe}
            for cand in watch_candidates(uni_quotes, self.concept_map, self.ret5, self.prev_prev_closes, today):
                if len(self.watch) >= WATCH_MAX:
                    break
                if cand.code in self.watch or cand.code in self.rejected:
                    continue
                self.watch[cand.code] = cand
        # 未判断过的观察池票：只要主账户有空位就补判断（无论是新进的还是之前满仓时跳过的）。
        if self.ledger.free_slots(self.accounts[0]) > 0:
            for code, cand in list(self.watch.items()):
                if code in self.verdicts:
                    continue
                q = quotes.get(code)
                if q is None:
                    continue
                try:
                    self._judge(cand, q, now)
                except Exception as e:
                    print(f"[board] {now:%H:%M} 判断 {code} 出错: {e!r}")

    def _judge(self, cand: Candidate, q: Quote, now: datetime):
        verdict, why = self._check(self.notice_judge, cand.code, now.date())
        notice_trace = getattr(self.notice_judge, "last_trace", None)
        news_trace = None
        if verdict == "pass":
            verdict, why = self._check(self.news_judge, cand.code, q.name, now)
            news_trace = getattr(self.news_judge, "last_trace", None)
        trace = {"strategy": "board",
                 "candidate": {"name": q.name, "price": q.price, "pct": q.pct,
                               "ret5": self.ret5.get(cand.code), "concept": cand.concept,
                               "heat": cand.heat, "score": cand.score, "ranks": cand.ranks},
                 "notices": notice_trace, "news": news_trace,
                 "outcome": {"bought": False, "shares": None, "price": None}}
        self.verdicts[cand.code] = (verdict, why, trace)
        self._log(now, cand.code, verdict, why, trace)
        if verdict in ("reject", "error"):
            self.watch.pop(cand.code, None)
            self.verdicts.pop(cand.code, None)
            self.rejected.add(cand.code)

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
