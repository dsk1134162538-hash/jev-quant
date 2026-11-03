"""冰点修复篮子（dj 2026-11-03，知识库「冰点、修复和拐点」）：连续第二个冰点日的 14:50，买主板一篮子，持有 3 个交易日。

回测 research/kb_events.py：连续第 2 个冰点日 21 个事件（约 17 个独立冰点），持有 3 天等权均 +2.34%、81% 为正、最差 −9.1%；
单个冰点日只有 +0.48%，所以必须是「连续」。一年只有 2~3 次，大部分日子什么都不做，只在 14:50 记一条当天状态
（decisions，strategy='ice'，verdict='state'），第二天要靠它判断「昨天是不是冰点」。没有昨天的状态就不买（宁可错过）。
不碰 Jev：篮子本来就是全市场等权的近似，逐只排雷没有意义。实盘不跟。
"""
import json
import sqlite3
from datetime import date, datetime, time

from jevq import config
from jevq.ledger import Ledger
from jevq.models import Fill, Quote
from jevq.rules.ice import decide_ice_sell, ice_basket, is_ice, market_state

SID = "ice"


class IceStrategy:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, trade_days: list[date] | None = None,
                 can_buy: bool = True):
        self.conn, self.ledger = conn, ledger
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
            d = decide_ice_sell(pos, q, now, self.calendar)
            if d.sell and (f := self.ledger.sell(self.account, pos, q, now, d.reason)):
                fills.append(f)
        return fills

    def buy_step(self, quotes: dict[str, Quote], now: datetime, deadline: float | None = None) -> list[Fill]:
        day = now.date()
        if not self.can_buy or not quotes or now.time() < time(*config.ICE_BUY_AT) or now.time() >= time(14, 57):
            return []
        if self._today_logged(day):
            return []                                   # 今天已经评估过（含买过）：重启也不重复
        state = market_state(quotes, day)
        if state is None:
            return []
        prev = self._yesterday_state(day)
        signal = is_ice(state) and is_ice(prev)
        fills: list[Fill] = []
        if signal:
            for q in ice_basket(quotes, day, config.ICE_BASKET):
                if f := self.ledger.buy(self.account, q, now, f"冰点修复｜连续冰点日｜跌停{state['dt']} 炸板率{state['rate']}%",
                                        budget=config.ICE_PER_TRADE):
                    fills.append(f)
        self._log(now, "pass" if signal else "state",
                  f"跌停 {state['dt']}、涨停 {state['zt']}、炸板率 {state['rate']}%｜"
                  + ("连续冰点日，买入一篮子 %d 只" % len(fills) if signal else
                     "今天是冰点日，昨天不是或没有记录" if is_ice(state) else "今天不是冰点日"), state)
        return fills

    def _today_logged(self, day: date) -> bool:
        return self.conn.execute("SELECT 1 FROM decisions WHERE strategy=? AND substr(ts, 1, 10)=? LIMIT 1",
                                 (SID, day.isoformat())).fetchone() is not None

    def _yesterday_state(self, day: date) -> dict | None:
        """上一个有记录的交易日的状态（本策略自己 14:50 记的）。要求就是上一交易日：中间漏了一天就当没有。"""
        days = [d for d in (self.calendar or []) if d < day]
        if not days:
            return None
        row = self.conn.execute(
            "SELECT trace FROM decisions WHERE strategy=? AND verdict IN ('state', 'pass') AND substr(ts, 1, 10)=? "
            "ORDER BY ts DESC, rowid DESC LIMIT 1", (SID, days[-1].isoformat())).fetchone()
        try:
            return json.loads(row[0]) if row and row[0] else None
        except ValueError:
            return None

    def _log(self, now: datetime, verdict: str, detail: str, trace: dict):
        self.conn.execute(
            "INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, '', ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), verdict, detail, json.dumps(trace, ensure_ascii=False), SID))
        self.conn.commit()
