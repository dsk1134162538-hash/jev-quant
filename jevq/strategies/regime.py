"""情绪轮动（用户 2026-10-09）：按大盘情绪决定这一刻跟哪个策略，实盘跟这个账户（config.LIVE_ACCOUNT）。

自己不选股、不问 Jev：扫首板、低吸、超跌反弹的主账户（都过了 Jev 公告排雷 + 消息面）成交一笔，轮动账户
按当时的情绪决定跟不跟——跟就同价同时买进；那个策略把它卖掉时同价卖出（卖法完全沿用原策略）。
2026-10-14 起按大盘环境跟（全市场 5 日涨幅中位数，回测见 docs/HANDOFF.md 第 27 项）：
- 大盘普跌（≤ REGIME_WEAK_MARKET_RET5）：只跟超跌反弹
- 大盘不弱：10:30（扫板窗口）前只等扫首板，之后没买到再跟低吸
- 冰点/情绪没判出来：不买（盘中情绪只用来把关）
（原来按情绪：火热扫首板、正常先扫首板后低吸、偏弱低吸——算不出大盘时照旧用）
- 当天确认了情绪拐点（Engine.turn_on）：超跌反弹的「拐点抄底」优先，偏弱/冰点也买
全仓单票，同一分钟多个策略都买了按上面的先后取一个。
已知局限：某个策略的主账户正拿着票（当时情绪不对没跟）时，它当天就不会再出新买点，轮动跟不到。
兜底：轮动拿着的票，原策略已经不拿了（比如跟卖那一下出了错、重启）而且不是今天买的 → 照当前价卖掉。
"""
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from jevq import config
from jevq.ledger import Ledger
from jevq.models import Fill, Quote

SID = "regime"
ACCOUNT = config.STRATEGIES[SID]["accounts"][0]
SOURCE_OF = {"board_main": "board", "lowbuy_main": "lowbuy", "rebound_main": "rebound"}
MAIN_OF = {v: k for k, v in SOURCE_OF.items()}


def market_env(market_ret5: float | None) -> str | None:
    if market_ret5 is None or not config.REGIME_BY_MARKET:
        return None
    return "大盘普跌" if market_ret5 <= config.REGIME_WEAK_MARKET_RET5 else "大盘不弱"


def allowed_sources(sentiment: str | None, turn: bool, now: datetime,
                    market_ret5: float | None = None) -> tuple[str, ...]:
    """这一刻允许跟的策略，按优先级。
    按大盘环境（用户 2026-10-14，REGIME_BY_MARKET）：普跌只跟超跌反弹；不弱跟扫首板、10:30 后没买到再跟低吸；
    冰点/情绪没判出来不买；确认拐点超跌优先。算不出大盘（market_ret5=None）照旧按情绪（REGIME_SOURCES）。"""
    env = market_env(market_ret5)
    if not sentiment or (sentiment == "冰点" and not (env == "大盘普跌" and config.REBOUND_MAIN_BUY_ON_ICE)):
        srcs = []
    elif env == "大盘普跌":
        srcs = ["rebound"]
    elif env == "大盘不弱":
        srcs = ["board", "lowbuy"]
    else:
        srcs = list(config.REGIME_SOURCES.get(sentiment, ()))
    if (env == "大盘不弱" or (env is None and sentiment == "正常")) and now.time() < config.REGIME_BOARD_FIRST_UNTIL:
        srcs = [s for s in srcs if s == "board"]
    if turn:
        srcs = ["rebound"] + [s for s in srcs if s != "rebound"]
    return tuple(srcs)


class RegimeStrategy:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, sentiment_provider: Callable[[], str | None],
                 turn_provider: Callable[[], bool] | None = None, can_buy: bool = True,
                 market_ret5: float | None = None):
        self.conn, self.ledger = conn, ledger
        self.sentiment_provider = sentiment_provider
        self.turn_provider = turn_provider or (lambda: False)
        self.can_buy = can_buy
        self.market_ret5 = market_ret5        # 全市场 5 日涨幅中位数（截至昨天收盘）；None = 照旧按情绪跟

    def mirror(self, fills: list[Fill], quote_of: Callable[[str], Quote | None], now: datetime) -> list[Fill]:
        """跟随这一轮各策略主账户的成交。先卖后买。"""
        out = []
        for f in fills:
            if f.side != "sell" or f.account not in SOURCE_OF:
                continue
            if self._source(f.code) == SOURCE_OF[f.account] and (got := self._sell(f.code, f.price, now,
                                                                                   f"跟{self._name(f.account)}卖｜{f.reason}", quote_of)):
                out.append(got)
        if self.can_buy and self.ledger.free_slots(ACCOUNT) > 0:
            sentiment, turn = self.sentiment_provider(), bool(self.turn_provider())
            allowed = allowed_sources(sentiment, turn, now, self.market_ret5)
            buys = sorted((f for f in fills if f.side == "buy" and SOURCE_OF.get(f.account) in allowed),
                          key=lambda f: allowed.index(SOURCE_OF[f.account]))
            for f in buys:
                q = quote_of(f.code)
                if q is None:
                    continue
                env = market_env(self.market_ret5)
                tag = f"{sentiment or '未知'}{'·拐点' if turn else ''}{f'·{env}' if env else ''}"
                got = self.ledger.buy(ACCOUNT, replace(q, price=f.price, ts=f.ts), now,
                                      f"轮动[{tag}]跟{self._name(f.account)}｜{f.reason}")
                if got:
                    self.conn.execute("INSERT OR REPLACE INTO regime_sources VALUES (?, ?, ?)",
                                      (f.code, SOURCE_OF[f.account], now.isoformat(timespec="seconds")))
                    self.conn.commit()
                    out.append(got)
                    break
        return out

    def reconcile(self, quote_of: Callable[[str], Quote | None], now: datetime) -> list[Fill]:
        """兜底：原策略已经不拿、又不是今天买的 → 按现价卖。"""
        out = []
        for pos in self.ledger.positions(ACCOUNT):
            src = self._source(pos.code)
            if pos.buy_date >= now.date() or (src and self.ledger.holds(MAIN_OF[src], pos.code)):
                continue
            q = quote_of(pos.code)
            if q is None or q.ts.date() < now.date():
                continue
            if got := self._sell(pos.code, q.price, now, "原策略已卖出，补卖", quote_of):
                out.append(got)
        return out

    def _sell(self, code: str, price: float, now: datetime, reason: str, quote_of) -> Fill | None:
        pos = next((p for p in self.ledger.positions(ACCOUNT) if p.code == code), None)
        q = quote_of(code)
        if pos is None or q is None:
            return None
        got = self.ledger.sell(ACCOUNT, pos, replace(q, price=price), now, reason)
        if got:
            self.conn.execute("DELETE FROM regime_sources WHERE code=?", (code,))
            self.conn.commit()
        return got

    def _source(self, code: str) -> str | None:
        row = self.conn.execute("SELECT source FROM regime_sources WHERE code=?", (code,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _name(account: str) -> str:
        return config.STRATEGIES[SOURCE_OF[account]]["name"]
