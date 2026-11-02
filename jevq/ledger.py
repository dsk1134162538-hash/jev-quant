import math
import sqlite3
from datetime import date, datetime

from jevq.config import (ACCOUNT_CAPITAL, ACCOUNT_MAX_POSITIONS, COMMISSION_MIN, COMMISSION_RATE, INITIAL_CASH,
                         LOT, MAX_PER_TRADE, MAX_POSITIONS, STAMP_RATE, TRANSFER_RATE, builtin_accounts)
from jevq.models import Fill, Position, Quote


def buy_fee(amount: float) -> float:
    return round(max(COMMISSION_MIN, amount * COMMISSION_RATE) + amount * TRANSFER_RATE, 2)


def sell_fee(amount: float) -> float:
    return round(max(COMMISSION_MIN, amount * COMMISSION_RATE)
                 + amount * (STAMP_RATE + TRANSFER_RATE), 2)


def affordable_shares(budget: float, price: float) -> int:
    lots = int(budget // (price * LOT))
    while lots > 0 and lots * LOT * price + buy_fee(lots * LOT * price) > budget:
        lots -= 1
    return lots * LOT


class Ledger:
    def __init__(self, conn: sqlite3.Connection, init: bool = True):
        self.conn = conn
        if not init:
            return
        for a in builtin_accounts():
            conn.execute("INSERT OR IGNORE INTO cash(account, amount) VALUES (?, ?)",
                         (a, ACCOUNT_CAPITAL.get(a, INITIAL_CASH)))
        conn.commit()

    def ensure_account(self, account: str, capital: float) -> None:
        """给自定义策略的账户（c{n}_main/_shadow）开户；已存在则什么都不做。"""
        self.conn.execute("INSERT OR IGNORE INTO cash(account, amount) VALUES (?, ?)",
                          (account, capital))
        self.conn.commit()

    def cash(self, account: str) -> float:
        return self.conn.execute("SELECT amount FROM cash WHERE account=?", (account,)).fetchone()[0]

    def positions(self, account: str) -> list[Position]:
        rows = self.conn.execute(
            "SELECT account, code, shares, cost, buy_price, buy_date FROM positions "
            "WHERE account=? ORDER BY buy_date, code", (account,)).fetchall()
        return [Position(a, c, s, cost, bp, date.fromisoformat(bd)) for a, c, s, cost, bp, bd in rows]

    def holds(self, account: str, code: str) -> bool:
        return self.conn.execute("SELECT 1 FROM positions WHERE account=? AND code=?",
                                 (account, code)).fetchone() is not None

    def free_slots(self, account: str) -> int:
        return ACCOUNT_MAX_POSITIONS.get(account, MAX_POSITIONS) - len(self.positions(account))

    def buy(self, account: str, q: Quote, ts: datetime, reason: str,
            budget: float | None = None) -> Fill | None:
        """budget 是含买入费用的单笔总预算；缺省保持原有账户配置行为。"""
        if budget is not None and (not math.isfinite(budget) or budget <= 0):
            raise ValueError("单笔买入预算必须是有限正数")
        if self.free_slots(account) <= 0 or self.holds(account, q.code):
            return None
        if q.suspended or q.price >= q.limit_up:
            return None
        configured_budget = MAX_PER_TRADE if budget is None else budget
        shares = affordable_shares(min(self.cash(account), configured_budget), q.price)
        if shares == 0:
            return None
        amount = shares * q.price
        fee = buy_fee(amount)
        self.conn.execute("UPDATE cash SET amount = amount - ? WHERE account=?", (amount + fee, account))
        self.conn.execute("INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?)",
                          (account, q.code, shares, amount + fee, q.price, ts.date().isoformat()))
        return self._record(Fill(account, q.code, "buy", q.price, shares, fee, ts, reason))

    def sell(self, account: str, pos: Position, q: Quote, ts: datetime, reason: str) -> Fill | None:
        if q.suspended or q.price <= q.limit_down:
            return None
        # 先删持仓：另一个连接已经卖掉时 rowcount 为 0，不能再入账一次现金。
        cur = self.conn.execute("DELETE FROM positions WHERE account=? AND code=?", (account, pos.code))
        if cur.rowcount != 1:
            self.conn.rollback()
            return None
        amount = pos.shares * q.price
        fee = sell_fee(amount)
        self.conn.execute("UPDATE cash SET amount = amount + ? WHERE account=?", (amount - fee, account))
        return self._record(Fill(account, pos.code, "sell", q.price, pos.shares, fee, ts, reason))

    def equity(self, account: str, prices: dict[str, float]) -> float:
        held = sum(p.shares * prices.get(p.code, p.buy_price) for p in self.positions(account))
        return round(self.cash(account) + held, 2)

    def fills_on(self, day: date) -> list[Fill]:
        rows = self.conn.execute(
            "SELECT account, code, side, price, shares, fee, ts, reason FROM fills "
            "WHERE substr(ts, 1, 10)=? ORDER BY id", (day.isoformat(),)).fetchall()
        return [Fill(a, c, s, p, n, f, datetime.fromisoformat(t), r) for a, c, s, p, n, f, t, r in rows]

    def _record(self, f: Fill) -> Fill:
        self.conn.execute(
            "INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f.account, f.code, f.side, f.price, f.shares, f.fee, f.ts.isoformat(), f.reason))
        self.conn.commit()
        return f
