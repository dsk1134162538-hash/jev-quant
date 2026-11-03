"""小市值轮动（dj 2026-11-03，research/size_study.py）：成交额最小 10% 里选 20 只等权，持有 20 个交易日后整体调仓。

对照基线，不是主力：回测年化 +12%、最大回撤 −34%，逐年极不均匀，未校正退市票的幸存者偏差。
调仓日 14:50：先卖持满 20 个交易日的（sell_step 在 buy_step 之前跑），空仓后同一分钟补买新一批。手里还有没卖掉的（跌停）就不补买。
不碰 Jev。实盘不跟。
"""
import json
import sqlite3
from datetime import date, datetime, time

from jevq import config
from jevq.ledger import Ledger
from jevq.models import Fill, Quote
from jevq.rules.smallcap import decide_sc_sell, pick_smallcap

SID = "smallcap"


class SmallcapStrategy:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, amount60: dict[str, float],
                 trade_days: list[date] | None = None, can_buy: bool = True):
        self.conn, self.ledger = conn, ledger
        self.amount60 = amount60
        self.calendar = sorted(trade_days) if trade_days else None
        self.can_buy = can_buy
        self.account = config.STRATEGIES[SID]["accounts"][0]
        self.accounts = (self.account,)

    def sell_step(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        fills = []
        for pos in self.ledger.positions(self.account):
            q = quotes.get(pos.code)
            if q is None or q.ts.date() < now.date():
                continue
            d = decide_sc_sell(pos, q, now, self.calendar)
            if d.sell and (f := self.ledger.sell(self.account, pos, q, now, d.reason)):
                fills.append(f)
        return fills

    def buy_step(self, quotes: dict[str, Quote], now: datetime, deadline: float | None = None) -> list[Fill]:
        day = now.date()
        if (not self.can_buy or not quotes or not self.amount60
                or now.time() < time(*config.SC_BUY_AT) or now.time() >= time(14, 57)):
            return []
        if self.ledger.positions(self.account) or self._bought_today(day):
            return []
        fills = []
        for q in pick_smallcap(quotes, self.amount60, day, config.SC_BASKET):
            if f := self.ledger.buy(self.account, q, now, "小市值轮动｜成交额最小 10%", budget=config.SC_PER_TRADE):
                fills.append(f)
        if fills:
            self._log(now, f"调仓买入 {len(fills)} 只（成交额最小 {config.SC_BOTTOM_PCT:.0%}，每笔 {config.SC_PER_TRADE:.0f} 元）")
        return fills

    def _bought_today(self, day: date) -> bool:
        return self.conn.execute("SELECT 1 FROM fills WHERE side='buy' AND account=? AND substr(ts, 1, 10)=? LIMIT 1",
                                 (self.account, day.isoformat())).fetchone() is not None

    def _log(self, now: datetime, detail: str):
        self.conn.execute("INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, '', 'pass', ?, ?, ?)",
                          (now.isoformat(timespec="seconds"), detail, json.dumps({"strategy": SID}), SID))
        self.conn.commit()
