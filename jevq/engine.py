import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta

from jevq import config
from jevq.config import ACCOUNTS, SENTIMENT_REFRESH_MIN
from jevq.ledger import Ledger
from jevq.models import Fill, Quote
from jevq.judge.market import SENTIMENT_LEVELS
from jevq.rules import turn as turn_rules


class Engine:
    """各策略共用的市场服务（低吸已下线，自己不买不卖）：每分钟取一次行情、记成交额，
    买入阶段刷新市场情绪（超跌反弹、自定义策略读 sentiment_label），行情连续失败置 halted。"""

    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, market_judge, fetch_quotes, fetch_pools,
                 universe: dict[str, str], prev_day: date, overseas: str | None,
                 accounts_provider: Callable[[], tuple[str, ...]] | None = None,
                 articles_provider: Callable[[datetime], list[dict]] | None = None,
                 turn_judge=None, flash=None, overseas_pcts: dict | None = None):
        self.conn, self.ledger = conn, ledger
        # 情绪拐点抄底（jevq.rules.turn + jevq.judge.turn.TurnJudge）：None = 不判拐点
        self.turn_judge = turn_judge
        self.turn_gate = None                          # 拐点把关（Claude，jevq.judge.turn_claude）；None = 不把关
        self.flash = flash                             # 7x24 快讯（jevq.data.news7x24.Flash）；None = 不接
        self.overseas_pcts = overseas_pcts or {}
        self.breadth_series: list = []                 # 今天每分钟的全市场广度（turn_rules.Breadth）
        self.turn_on = False                           # 今天 Jev 已确认情绪拐点 → 超跌反弹当天盘中可买
        self.turn_detail: dict | None = None
        self._turn_asked_at: datetime | None = None
        self._level: int | None = None                 # 迟滞后的情绪档位（SENTIMENT_LEVELS 下标）
        # 持仓代码（_fetch 用来决定该拉哪些行情）要看全部账户；默认取内置账户，
        # 自定义策略跑起来后由调用方注入一个能看到「今天在跑」账户全集的 provider（见 jevq.accounts.all_accounts）。
        self.accounts_provider = accounts_provider or (lambda: ACCOUNTS)
        self.market_judge = market_judge
        self.articles_provider = articles_provider     # hi189 文章；None = 不接
        self.articles: list[dict] = []
        self.sectors: list = []                        # 文章里解析出的板块（扫首板点名用）
        # 涨停补充数据（jevq.data.ztpool），run.py 构造后挂上；None = 不接
        self.zt_extra: Callable[[date], dict] | None = None        # 昨日涨停今日表现 → 并进情绪统计
        self.zt_reasons: Callable[[date], list] | None = None      # 涨停原因题材 → 主线判断的「点名」板块
        self.zt_sectors: list = []
        self.catalyst_provider: Callable[[date], list[str]] | None = None   # 隔夜消息催化 → 大盘情绪（Claude 判时挂上）
        self.fetch_quotes, self.fetch_pools = fetch_quotes, fetch_pools
        self.universe, self.prev_day, self.overseas = universe, prev_day, overseas
        self.fail_streak = 0
        self.halted = False
        self.last_quotes: dict[str, Quote] = {}
        self.last_fetch_minute: str | None = None   # 最近一次取数成功的分钟（"%Y-%m-%d %H:%M"）
        self.sentiment_label: str | None = None
        self.sentiment_trace: dict | None = None
        self._sentiment_at: datetime | None = None
        self._restored_day: date | None = None
        self._fetched_minute: tuple[datetime, dict[str, Quote]] | None = None   # 本分钟卖出阶段取到的行情

    def tick(self, now: datetime) -> list[Fill]:
        """一分钟一轮 = sell_step + buy_step。"""
        return self.sell_step(now) + self.buy_step(now)

    def sell_step(self, now: datetime) -> list[Fill]:
        """卖出阶段：只取行情、记成交额，不碰 Jev；行情留给同一分钟的 buy_step 刷新情绪。自己不卖，返回 []。"""
        self._fetched_minute = None
        if self._sentiment_at is None and self._restored_day != now.date():
            self._restore_sentiment(now.date())
            self._restore_turn(now.date())
        quotes = self._fetch(now)
        if quotes is not None:
            self._fetched_minute = (now, quotes)
        return []

    def buy_step(self, now: datetime) -> list[Fill]:
        """买入阶段：按 SENTIMENT_REFRESH_MIN 刷新市场情绪。必须在同一分钟的 sell_step 之后调用——
        所有策略卖完才问 Jev，慢 Jev 永远拖不住卖出；halted 也照常刷新。自己不买，返回 []。"""
        if self._fetched_minute is None or self._fetched_minute[0] != now:
            return []
        quotes = self._fetched_minute[1]
        self._fetched_minute = None               # 每分钟只刷一轮
        self._sentiment(now, quotes)
        self._turn_step(now)
        return []

    def _refresh_articles(self, now: datetime):
        """查 hi189 新文章；失败保留已有的，不影响情绪刷新。"""
        if self.articles_provider is None:
            return
        try:
            from jevq.data.hi189 import sectors_of
            self.articles = self.articles_provider(now) or []
            self.sectors = sectors_of(self.articles)
        except Exception as e:
            print(f"[engine] hi189 文章刷新失败: {e!r}")

    def _refresh_zt_reasons(self, now: datetime):
        """涨停原因题材板块，跟情绪同频刷新；失败保留上一轮的。"""
        if self.zt_reasons is None:
            return
        try:
            self.zt_sectors = list(self.zt_reasons(now.date()))
        except Exception as e:
            print(f"[engine] 涨停原因刷新失败: {e!r}")

    def all_sectors(self) -> list:
        """主线判断用的「点名」板块：hi189 文章 + 当天涨停原因题材。"""
        return list(self.sectors) + list(self.zt_sectors)

    def _settle(self, now: datetime, raw: str | None, trace: dict | None) -> tuple[str | None, float | None, str | None]:
        """Jev 原始标签 → 各策略用的情绪。返回（标签, 期望分, 为什么没跟 Jev：开盘未定/迟滞/None）。
        - 开盘未定（SENTIMENT_SETTLE 之前）：沿用上一交易日最后一次的情绪，没有就用 Jev 的。
        - 迟滞：期望分越过档位分界再多 SENTIMENT_HYSTERESIS 才换档；明显的变化（一次跨过）立刻换，不拖慢。
        - Jev 没判出来 → None（各策略照旧按「未知」处理），档位留着，下次接着比。"""
        from jevq.app.sentiment_board import score as expected
        if raw is None:
            return None, None, None
        s = expected(raw, (trace or {}).get("probabilities"))
        if now.time() < config.SENTIMENT_SETTLE:
            carried = self._prev_close_label()
            if carried in SENTIMENT_LEVELS:
                self._level = SENTIMENT_LEVELS.index(carried)
                return carried, s, "开盘未定"
            self._level = SENTIMENT_LEVELS.index(raw) if raw in SENTIMENT_LEVELS else None
            return raw, s, None
        if s is None:
            return raw, None, None
        if self._level is None:
            self._level = max(0, min(3, int(s + 0.5)))
        else:
            d = config.SENTIMENT_HYSTERESIS
            while self._level < 3 and s >= self._level + 0.5 + d:
                self._level += 1
            while self._level > 0 and s <= self._level - 0.5 - d:
                self._level -= 1
        label = SENTIMENT_LEVELS[self._level]
        return label, s, (None if label == raw else "迟滞")

    def _prev_close_label(self) -> str | None:
        try:
            row = self.conn.execute(
                "SELECT detail FROM decisions WHERE verdict='sentiment' "
                "AND (strategy IN ('lowbuy', 'market') OR strategy IS NULL) AND substr(ts, 1, 10) = ? "
                "ORDER BY ts DESC, rowid DESC LIMIT 1", (self.prev_day.isoformat(),)).fetchone()
            return json.loads(row[0]).get("label") if row else None
        except Exception:
            return None

    def _news(self, now: datetime) -> list[str]:
        if self.flash is None:
            return []
        try:
            if self.catalyst_provider is not None:       # 交给 Claude 判（用户 2026-10-14）：上下文大，多给些快讯
                return self.flash.recent(now, minutes=config.CLAUDE_NEWS_MINUTES, limit=config.CLAUDE_NEWS_LIMIT)
            return self.flash.recent(now, minutes=60, limit=20)
        except Exception as e:
            print(f"[engine] 7x24 快讯失败: {e!r}")
            return []

    def _catalyst(self, now: datetime) -> list[str]:
        """隔夜消息催化摘要（jevq.data.catalyst.summary_lines）；没挂 provider 或失败 = 空。"""
        if self.catalyst_provider is None:
            return []
        try:
            return list(self.catalyst_provider(now.date()))
        except Exception as e:
            print(f"[engine] 隔夜消息催化读取失败: {e!r}")
            return []

    def _restore_sentiment(self, day: date):
        """重启后恢复当日最后一次情绪标签，只给卖出阶段（自定义退出条件）一个合理值；
        _sentiment_at 不动，买入阶段照旧立即重新判断。低吸下线前写的情绪行 strategy 是 'lowbuy' 或空。"""
        self._restored_day = day
        try:
            row = self.conn.execute(
                "SELECT detail FROM decisions WHERE verdict='sentiment' "
                "AND (strategy IN ('lowbuy', 'market') OR strategy IS NULL) "
                "AND substr(ts, 1, 10) = ? ORDER BY ts DESC, rowid DESC LIMIT 1", (day.isoformat(),)).fetchone()
            label = json.loads(row[0]).get("label") if row else None
        except Exception as e:
            print(f"[engine] 恢复当日情绪失败: {e!r}")
            return
        self.sentiment_label = label if isinstance(label, str) else None
        if self.sentiment_label in SENTIMENT_LEVELS:          # 迟滞从恢复的档位接着比
            self._level = SENTIMENT_LEVELS.index(self.sentiment_label)

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
        try:            # 全市场广度（拐点判断、情绪面板用）：只记录，失败不影响行情
            self._record_breadth(now, quotes, total)
        except Exception as e:
            self.conn.rollback()
            print(f"[engine] 广度记录失败: {e!r}")
        return quotes

    def _record_breadth(self, now: datetime, quotes: dict[str, Quote], total: float):
        prev = self.conn.execute("SELECT amount FROM turnover WHERE ts=?",
                                 (f"{self.prev_day.isoformat()} {now:%H:%M}",)).fetchone()
        ratio = round((total / prev[0] - 1) * 100, 1) if prev and prev[0] > 0 else None
        b = turn_rules.breadth(quotes, self.universe, now, ratio)
        if not b.total:
            return
        if self.breadth_series and self.breadth_series[-1].ts.date() != now.date():
            self.breadth_series = []
        if self.breadth_series and self.breadth_series[-1].ts == b.ts:
            self.breadth_series[-1] = b
        else:
            self.breadth_series.append(b)
        self.conn.execute("INSERT OR REPLACE INTO market_breadth VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (b.ts.strftime("%Y-%m-%d %H:%M"), b.up, b.down, b.total, b.limit_up, b.limit_down,
                           b.amount_ratio))
        self.conn.commit()

    def _restore_turn(self, day: date):
        """重启后恢复今天的广度序列和「已确认拐点」，拐点规则要看今天最低点、跌停最多是多少。"""
        try:
            rows = self.conn.execute(
                "SELECT ts, up, down, total, limit_up, limit_down, amount_ratio FROM market_breadth "
                "WHERE substr(ts, 1, 10) = ? ORDER BY ts", (day.isoformat(),)).fetchall()
            self.breadth_series = [turn_rules.Breadth(datetime.strptime(ts, "%Y-%m-%d %H:%M"), *rest)
                                   for ts, *rest in rows]
            row = self.conn.execute(
                "SELECT detail FROM decisions WHERE verdict='turn' AND strategy='market' AND substr(ts, 1, 10) = ? "
                "ORDER BY ts DESC, rowid DESC LIMIT 1", (day.isoformat(),)).fetchone()
            detail = json.loads(row[0]) if row else None
        except Exception as e:
            print(f"[engine] 恢复今日广度/拐点失败: {e!r}")
            return
        if detail and detail.get("confirmed"):
            self.turn_on, self.turn_detail = True, detail

    def _turn_step(self, now: datetime):
        """规则看到恐慌后的修复 → 确认，当天锁定 turn_on。
        挂了 turn_gate（Claude，用户 2026-10-14）：必须它同意（≥ TURN_CLAUDE_MIN）才确认，Jev 照问只记录。
        没挂：TURN_JEV_REQUIRED=True 要 Jev 确认，False 规则满足就确认、Jev 只记录。
        把关的没确认、规则还满足时每 TURN_JEV_RETRY 分钟再问；没答上下一分钟就再问。每次问都记一条 decisions。"""
        if self.turn_on or self.halted or (self.turn_judge is None and self.turn_gate is None):
            return
        ok, detail = turn_rules.turn_check(self.breadth_series, now)
        if not ok:
            return
        if self._turn_asked_at and now - self._turn_asked_at < timedelta(minutes=config.TURN_JEV_RETRY):
            return
        prob = None
        if self.turn_judge is not None:
            try:
                prob = self.turn_judge.confirm(self.breadth_series, detail, now, self.sentiment_label,
                                               self.overseas_pcts)
            except Exception as e:
                print(f"[engine] 拐点确认（Jev）异常: {e!r}")
        jev_ok = prob is not None and prob >= config.TURN_JEV_MIN
        record = {"prob": prob, "jev_agrees": jev_ok, "text": turn_rules.describe(detail), "rules": detail}
        trace = {"jev": getattr(self.turn_judge, "last_trace", None)}
        if self.turn_gate is not None:
            try:
                gate_prob = self.turn_gate.confirm(self.breadth_series, detail, now)
            except Exception as e:
                print(f"[engine] 拐点确认（Claude）异常: {e!r}")
                gate_prob = None
            gate_trace = getattr(self.turn_gate, "last_trace", None) or {}
            confirmed = gate_prob is not None and gate_prob >= config.TURN_CLAUDE_MIN
            answered = gate_prob is not None
            record.update(by="规则+Claude", claude_prob=gate_prob, claude_reason=gate_trace.get("reason"))
            trace["claude"] = gate_trace
        else:
            confirmed = jev_ok if config.TURN_JEV_REQUIRED else True
            answered = prob is not None
            record["by"] = "规则+Jev" if config.TURN_JEV_REQUIRED else "规则"
        record["confirmed"] = confirmed
        self._turn_asked_at = now if answered else None
        self.conn.execute(
            "INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, '', 'turn', ?, ?, 'market')",
            (now.isoformat(timespec="seconds"), json.dumps(record, ensure_ascii=False),
             json.dumps(trace, ensure_ascii=False)))
        self.conn.commit()
        if confirmed:
            self.turn_on, self.turn_detail = True, record

    def _sentiment(self, now: datetime, quotes: dict[str, Quote]):
        fresh = self._sentiment_at and now - self._sentiment_at < timedelta(minutes=SENTIMENT_REFRESH_MIN)
        if not fresh:
            self._refresh_articles(now)
            self._refresh_zt_reasons(now)
            stats = self._market_stats(now, quotes)
            if stats:
                try:
                    from jevq.data.hi189 import for_judge
                    judged = for_judge(self.articles)         # 中报在演练期只记录，不交给 Jev
                    news = self._news(now)
                    extra = {k: v for k, v in (("news", news), ("catalyst", self._catalyst(now))) if v}
                    if extra:
                        raw = self.market_judge.sentiment(stats, judged or None, **extra)
                    else:
                        raw = (self.market_judge.sentiment(stats, judged) if judged
                               else self.market_judge.sentiment(stats))
                    self.sentiment_trace = getattr(self.market_judge, "sentiment_trace", None)
                except Exception as e:
                    print(f"[engine] 情绪判断异常: {e!r}")
                    raw, self.sentiment_trace = None, None
            else:
                raw, self.sentiment_trace = None, None
            self.sentiment_label, score, held = self._settle(now, raw, self.sentiment_trace)
            self._sentiment_at = now
            # 外围一起记下：面板试跑（后台 daemon 在交易、面板自己没跑时）从这一行读最近的情绪与外围
            # label = 迟滞/开盘未定之后各策略实际用的情绪；raw_label = Jev 这一次判的
            detail = json.dumps({"stats": stats, "label": self.sentiment_label, "overseas": self.overseas,
                                 "raw_label": raw, "score": score, "held": held,
                                 "zt_reasons": {s.name: len(s.limit_up) for s in self.zt_sectors}}, ensure_ascii=False)
            trace = None if self.sentiment_trace is None else json.dumps(self.sentiment_trace, ensure_ascii=False)
            self.conn.execute(
                "INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) "
                "VALUES (?, '', 'sentiment', ?, ?, 'market')",
                (now.isoformat(timespec="seconds"), detail, trace))
            self.conn.commit()

    def _market_stats(self, now: datetime, quotes: dict[str, Quote]) -> dict:
        try:
            stats = dict(self.fetch_pools(now.date()))
        except Exception as e:
            print(f"[engine] 涨跌停池失败: {e!r}")
            return {}
        if self.zt_extra is not None:
            try:
                stats.update(self.zt_extra(now.date()))
            except Exception as e:
                print(f"[engine] 昨日涨停表现失败: {e!r}")
        uni = [q for c, q in quotes.items() if c in self.universe]
        stats["上涨家数"] = sum(q.pct > 0 for q in uni)
        stats["下跌家数"] = sum(q.pct < 0 for q in uni)
        prev = self.conn.execute("SELECT amount FROM turnover WHERE ts=?",
                                 (f"{self.prev_day.isoformat()} {now:%H:%M}",)).fetchone()
        if prev and prev[0] > 0:
            cur = sum(q.amount for q in uni)
            stats["成交额较昨日同期%"] = round((cur / prev[0] - 1) * 100, 1)
        return stats
