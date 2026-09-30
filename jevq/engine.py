import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta

from jevq.config import ACCOUNTS, SENTIMENT_REFRESH_MIN
from jevq.ledger import Ledger
from jevq.models import Fill, Quote


class Engine:
    """各策略共用的市场服务（低吸已下线，自己不买不卖）：每分钟取一次行情、记成交额，
    买入阶段刷新市场情绪（超跌反弹、自定义策略读 sentiment_label），行情连续失败置 halted。"""

    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, market_judge, fetch_quotes, fetch_pools,
                 universe: dict[str, str], prev_day: date, overseas: str | None,
                 accounts_provider: Callable[[], tuple[str, ...]] | None = None):
        self.conn, self.ledger = conn, ledger
        # 持仓代码（_fetch 用来决定该拉哪些行情）要看全部账户；默认取内置账户，
        # 自定义策略跑起来后由调用方注入一个能看到「今天在跑」账户全集的 provider（见 jevq.accounts.all_accounts）。
        self.accounts_provider = accounts_provider or (lambda: ACCOUNTS)
        self.market_judge = market_judge
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

    def _sentiment(self, now: datetime, quotes: dict[str, Quote]):
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
            detail = json.dumps({"stats": stats, "label": self.sentiment_label, "overseas": self.overseas},
                                ensure_ascii=False)
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
        uni = [q for c, q in quotes.items() if c in self.universe]
        stats["上涨家数"] = sum(q.pct > 0 for q in uni)
        stats["下跌家数"] = sum(q.pct < 0 for q in uni)
        prev = self.conn.execute("SELECT amount FROM turnover WHERE ts=?",
                                 (f"{self.prev_day.isoformat()} {now:%H:%M}",)).fetchone()
        if prev and prev[0] > 0:
            cur = sum(q.amount for q in uni)
            stats["成交额较昨日同期%"] = round((cur / prev[0] - 1) * 100, 1)
        return stats
