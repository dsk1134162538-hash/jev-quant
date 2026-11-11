"""AI 灵活交易账户的执行器（dj 2026-11-03「要灵活交易，有时候就得突破一下」）。

DeepSeek 在关键时点（ai_loop，dashboard/flex_trader.py）自己判断买什么、买多少、什么时候卖，把订单写进
dashboard/runtime/flex_orders.json；这里每分钟读订单，按实时行情成交，并实时盯着 AI 自己设的止损线。
AI 可以突破任何选股规则（订单里标「模式外」，复盘时和「模式内」分开统计），但有四条保命的护栏，不是选股规则
（知识库「仓位与风控」：先保证活着，再谈赚多少），代码里强制执行，AI 绕不过去：
  1. 单票最多占账户净值 FLEX_MAX_PCT（40%），持仓最多 FLEX_MAX_POSITIONS（3）只，亏损的持仓不加仓；
  2. 买入必须同时给：止损位（低于现价、不超过 12% 远）、「错了的信号」、模式（模式内 / 模式外）；
  3. 当日净值亏到 FLEX_DAY_LOSS（3%）当天不再开新仓；
  4. 连亏 FLEX_LOSS_STREAK（3）笔，当天不再开新仓。
只做沪深主板；T+1（今天买的明天才能卖，止损也一样）；订单 10 分钟内有效，同一张订单只成交一次（重启也不重复）。
所有订单、拒绝原因、止损位都记在 decisions（strategy='aiflex'），做复盘用。实盘不跟。
"""
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from jevq import config
from jevq.ledger import Ledger
from jevq.models import Fill, Position, Quote

SID = "aiflex"
ROOT = Path(__file__).resolve().parents[2]
ORDERS_PATH = ROOT / "dashboard" / "runtime" / "flex_orders.json"
ORDERS2_PATH = ROOT / "dashboard" / "runtime" / "flex2_orders.json"      # 二号交易员（趋势风格）
MGR_ORDERS_PATH = ROOT / "dashboard" / "runtime" / "manager_orders.json"  # 经理的一万元账户（dashboard/manager_trade.py 写）
ORDER_TTL_SECONDS = 600
MAX_STOP_DISTANCE = 0.12          # 止损位最多低于现价 12%
MAX_REF_DEVIATION = 0.02          # 成交价相对 AI 看到的参考价最多偏离 2%
MAIN_BOARD = ("60", "00")
MODES = ("模式内", "模式外")


def _num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


class AiflexStrategy:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, trade_days: list[date] | None = None,
                 orders_path: Path = ORDERS_PATH, can_buy: bool = True, sid: str = SID):
        self.conn, self.ledger = conn, ledger
        self.sid = sid
        self.calendar = sorted(trade_days) if trade_days else None
        self.orders_path = Path(orders_path)
        self.can_buy = can_buy
        self.account = config.STRATEGIES[sid]["accounts"][0]
        self.accounts = (self.account,)
        limits = config.FLEX_LIMITS.get(sid, {})                   # 经理账户：单票可以全仓、只拿一只
        self.max_pct = limits.get("max_pct", config.FLEX_MAX_PCT)
        self.max_positions = limits.get("max_positions", config.FLEX_MAX_POSITIONS)
        self.label = limits.get("label", "AI灵活")
        self.capital = config.ACCOUNT_CAPITAL.get(self.account, config.FLEX_CAPITAL)

    # ---------- 读订单 ----------
    def _load(self) -> tuple[datetime | None, list[dict]]:
        try:
            data = json.loads(self.orders_path.read_text(encoding="utf-8"))
            return datetime.fromisoformat(data["ts"]), [o for o in data.get("orders", []) if isinstance(o, dict) and o.get("id")]
        except (OSError, ValueError, KeyError, TypeError):
            return None, []

    def _done_ids(self, day: date) -> set[str]:
        out = set()
        for (trace,) in self.conn.execute(
                "SELECT trace FROM decisions WHERE strategy=? AND verdict IN ('order_ok', 'order_reject') "
                "AND substr(ts, 1, 10)=?", (self.sid, day.isoformat())):
            try:
                out.add(json.loads(trace)["id"])
            except (ValueError, KeyError, TypeError):
                pass
        return out

    def _log(self, now: datetime, verdict: str, code: str, detail: str, trace: dict):
        self.conn.execute("INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, ?, ?, ?, ?, ?)",
                          (now.isoformat(timespec="seconds"), code, verdict, detail[:300],
                           json.dumps(trace, ensure_ascii=False), self.sid))
        self.conn.commit()

    def _reject(self, now: datetime, order: dict, why: str):
        self._log(now, "order_reject", str(order.get("code", "")), f"{order.get('action', '?')} 被拒：{why}", {**order, "id": order["id"]})

    # ---------- 护栏状态 ----------
    def _equity(self, quotes: dict[str, Quote]) -> float:
        return self.ledger.equity(self.account, {c: q.price for c, q in quotes.items()})

    def _results(self) -> list[tuple[str, float]]:
        """已了结的每笔（按卖出顺序）：[(卖出日期, 盈亏元)]。"""
        opens: dict[str, tuple[float, int]] = {}
        out = []
        for code, side, price, shares, fee, ts in self.conn.execute(
                "SELECT code, side, price, shares, fee, ts FROM fills WHERE account=? ORDER BY id", (self.account,)):
            if side == "buy":
                opens[code] = (price * shares + fee, shares)
            elif code in opens:
                cost, _ = opens.pop(code)
                out.append((ts[:10], price * shares - fee - cost))
        return out

    def _blocked(self, equity: float, day: date) -> str:
        row = self.conn.execute("SELECT value FROM equity WHERE account=? AND date<? ORDER BY date DESC LIMIT 1",
                                (self.account, day.isoformat())).fetchone()
        prev = row[0] if row else self.capital
        if prev and equity / prev - 1 <= -config.FLEX_DAY_LOSS:
            return f"当日亏损 {abs(equity / prev - 1) * 100:.1f}%（线 {config.FLEX_DAY_LOSS:.0%}），今天不再开新仓"
        last = self._results()[-config.FLEX_LOSS_STREAK:]
        if len(last) == config.FLEX_LOSS_STREAK and all(p < 0 for _, p in last) and last[-1][0] == day.isoformat():
            return f"连亏 {config.FLEX_LOSS_STREAK} 笔，今天不再开新仓"
        return ""

    # ---------- 买 ----------
    def buy_step(self, quotes: dict[str, Quote], now: datetime, deadline: float | None = None) -> list[Fill]:
        if not self.can_buy or not quotes:
            return []
        ts, orders = self._load()
        day = now.date()
        todo = [o for o in orders if o.get("action") == "买入" and o["id"] not in self._done_ids(day)]
        fills = []
        for order in todo:
            if ts is None or (now - ts).total_seconds() > ORDER_TTL_SECONDS:
                self._reject(now, order, f"订单已过期（超过 {ORDER_TTL_SECONDS // 60} 分钟）")
                continue
            f = self._buy(order, quotes, now)
            if f:
                fills.append(f)
        return fills

    def _buy(self, order: dict, quotes: dict[str, Quote], now: datetime) -> Fill | None:
        code = str(order.get("code", ""))
        if not code.startswith(MAIN_BOARD):
            return self._reject(now, order, "只做沪深主板（60、00 开头）")
        q = quotes.get(code)
        if q is None or q.ts.date() != now.date() or (now - q.ts).total_seconds() > 180 or q.suspended:
            return self._reject(now, order, "行情缺失、过期或停牌")
        if q.price >= q.limit_up or q.price <= q.limit_down:
            return self._reject(now, order, "涨停或跌停价，买不到")
        pct = _num(order.get("pct"))
        if not pct or pct <= 0:
            return self._reject(now, order, "仓位比例无效")
        stop = _num(order.get("stop"))
        if stop is None:
            return self._reject(now, order, "缺少止损位")
        if not 0 < stop < q.price:
            return self._reject(now, order, f"止损位 {stop} 必须低于现价 {q.price}")
        if stop < q.price * (1 - MAX_STOP_DISTANCE):
            return self._reject(now, order, f"止损线太远（{(1 - stop / q.price) * 100:.0f}%，最多 {MAX_STOP_DISTANCE:.0%}）")
        if len(str(order.get("invalid") or "").strip()) < 4:
            return self._reject(now, order, "缺少「错了的信号」（什么情况说明这笔判断错了）")
        if order.get("mode") not in MODES:
            return self._reject(now, order, "模式必须是「模式内」或「模式外」")
        ref = _num(order.get("ref_price"))
        if ref and abs(q.price / ref - 1) > MAX_REF_DEVIATION:
            return self._reject(now, order, f"现价 {q.price} 已偏离参考价 {ref} 超过 {MAX_REF_DEVIATION:.0%}，不追")
        held = {p.code: p for p in self.ledger.positions(self.account)}
        if code in held and q.price < held[code].buy_price:
            return self._reject(now, order, "亏损持仓不加仓")
        if code not in held and len(held) >= self.max_positions:
            return self._reject(now, order, f"持仓已满（{self.max_positions} 只）")
        equity = self._equity(quotes)
        why = self._blocked(equity, now.date())
        if why:
            return self._reject(now, order, why)
        budget = min(min(pct, self.max_pct) * equity, self.ledger.cash(self.account))
        reason = f"{self.label}｜{order.get('pattern', '')}｜{order['mode']}｜{order.get('reason', '')}"[:120]
        fill = self.ledger.buy(self.account, q, now, reason, budget=budget)
        if fill is None:
            return self._reject(now, order, "资金不足一手或持仓限制，没有成交")
        self._log(now, "stop", code, f"止损 {stop}｜错了的信号：{order['invalid']}",
                  {"code": code, "stop": stop, "invalid": order["invalid"], "mode": order["mode"],
                   "pattern": order.get("pattern", ""), "reason": order.get("reason", ""), "order_id": order["id"]})
        self._log(now, "order_ok", code, f"买入 {fill.shares} 股 @{fill.price:.2f}（{order['mode']}·{order.get('pattern', '')}）", {**order, "id": order["id"]})
        return fill

    # ---------- 卖 ----------
    def sell_step(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        fills = []
        day = now.date()
        for pos in self.ledger.positions(self.account):
            q = quotes.get(pos.code)
            if q is None or q.ts.date() != day or pos.buy_date >= day:
                continue
            stop = self._stop_of(pos.code)
            if stop is not None and q.price <= stop:
                f = self.ledger.sell(self.account, pos, q, now, f"止损（AI 自设 {stop}）｜{self._invalid_of(pos.code)}"[:120])
                if f:
                    fills.append(f)
        ts, orders = self._load()
        for order in [o for o in orders if o.get("action") == "卖出" and o["id"] not in self._done_ids(day)]:
            if ts is None or (now - ts).total_seconds() > ORDER_TTL_SECONDS:
                self._reject(now, order, f"订单已过期（超过 {ORDER_TTL_SECONDS // 60} 分钟）")
                continue
            f = self._sell(order, quotes, now)
            if f:
                fills.append(f)
        return fills

    def _sell(self, order: dict, quotes: dict[str, Quote], now: datetime) -> Fill | None:
        code = str(order.get("code", ""))
        pos = next((p for p in self.ledger.positions(self.account) if p.code == code), None)
        q = quotes.get(code)
        if pos is None:
            return self._reject(now, order, "没有这只持仓")
        if pos.buy_date >= now.date():
            return self._reject(now, order, "T+1：今天买入的明天才能卖")
        if q is None or q.ts.date() != now.date():
            return self._reject(now, order, "行情缺失或过期")
        fill = self.ledger.sell(self.account, pos, q, now, f"{self.label}｜卖出｜{order.get('reason', '')}"[:120])
        if fill is None:
            return self._reject(now, order, "跌停或停牌，卖不出去")
        self._log(now, "order_ok", code, f"卖出 {fill.shares} 股 @{fill.price:.2f}", {**order, "id": order["id"]})
        return fill

    def _stop_row(self, code: str) -> dict | None:
        row = self.conn.execute("SELECT trace FROM decisions WHERE strategy=? AND verdict='stop' AND code=? "
                                "ORDER BY ts DESC, rowid DESC LIMIT 1", (self.sid, code)).fetchone()
        try:
            return json.loads(row[0]) if row else None
        except ValueError:
            return None

    def _stop_of(self, code: str) -> float | None:
        row = self._stop_row(code)
        return _num(row.get("stop")) if row else None

    def _invalid_of(self, code: str) -> str:
        row = self._stop_row(code)
        return (row or {}).get("invalid", "")
