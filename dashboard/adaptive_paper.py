"""Kimi 动态策略 → 隔离模拟成交 → 独立对账。模块没有任何券商或真实委托依赖。"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime, time
from pathlib import Path

from jevq import db
from jevq import config as jevq_config
from jevq.config import AI_ACCOUNT, AI_TOTAL_BUDGET
from jevq.ledger import Ledger, buy_fee, sell_fee

INITIAL_CAPITAL = AI_TOTAL_BUDGET
MAX_DAILY_BUYS = 2
MIN_CONFIDENCE = 0.65
MAX_QUOTE_AGE_SECONDS = 180
POSTURES = {"防守": "证据偏弱，优先降低模拟暴露", "平衡": "证据分化，保留少量模拟评估",
            "积极": "证据共振偏强，但仍受硬风控限制"}
PORTFOLIO_ACTIONS = {"观察": "只保留研究，不生成模拟成交", "模拟买入": "申请隔离账户模拟买入",
                     "模拟卖出": "申请隔离账户模拟卖出"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_paper_state(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ai_paper_strategies(
  id TEXT PRIMARY KEY, decision_id TEXT UNIQUE NOT NULL, ts TEXT NOT NULL, model TEXT,
  status TEXT NOT NULL, portfolio_json TEXT, context_json TEXT, execution_json TEXT, error TEXT);
"""


class ReconciliationError(RuntimeError):
    """模拟账与成交流水不一致，必须暂停新的策略处理。"""


def build_portfolio_questions(quotes: dict) -> dict:
    return {"portfolio": {
        "type": "portfolio",
        "instructions": ("根据最新市场、行业、指数、首板观察池和模拟持仓，给出这一版策略：每只票一个动作，"
                         "理由带当前数据。首板候选同意打就「模拟买入」，不看好就「观察」。"
                         "sector 写这只票所属板块或题材（先用行情里的 industry，如「传媒娱乐」，有更贴切的题材可以写题材）；next 写后续怎么操作和触发条件："
                         "出现什么价格、量能、竞价、板块或情绪信号时做什么（打板、低吸、卖出、回本走），什么情况彻底放弃。"),
        "criteria": {"posture": POSTURES, "positionPct": [0, 0.1, 0.2],
                     "codes": {code: quote.name for code, quote in quotes.items()},
                     "actions": PORTFOLIO_ACTIONS},
    }}


def _plain_code(value: str) -> str:
    code = value.split(".")[0]
    return code[-6:] if len(code) >= 6 else code


def _quote_valid(quote, now: datetime) -> bool:
    if quote is None or not all(math.isfinite(float(getattr(quote, key, 0) or 0))
                                for key in ("price", "pre_close", "limit_up", "limit_down")):
        return False
    age = (now - quote.ts).total_seconds()
    return -30 <= age <= MAX_QUOTE_AGE_SECONDS and quote.price > 0 and quote.pre_close > 0


class AdaptivePaperEngine:
    def __init__(self, db_path: Path, judge=None, quote_provider=None, clock=datetime.now, external: bool = False,
                 candidate_codes=None):
        self.db_path = Path(db_path).resolve()
        # 订阅/WorkBuddy 路径：候选就是快照里那批（含首板观察池），不再自己从行业龙头重算，免得两边对不上
        self.fixed_codes = list(candidate_codes) if candidate_codes is not None else None
        self.judge = judge
        self.external = external
        self.quote_provider = quote_provider or (lambda codes: {})
        self.clock = datetime.now if clock is datetime else clock
        self._state_lock = threading.Lock()
        self._running = False
        self._fast_risk = {"checkedAt": None, "status": "clear", "alerts": [], "liveTrading": False}
        self._init_db()

    def _init_db(self) -> None:
        conn = db.connect(str(self.db_path))
        try:
            conn.executescript(SCHEMA)
            Ledger(conn, init=False).ensure_account(AI_ACCOUNT, INITIAL_CAPITAL)
            interrupted = conn.execute("SELECT id FROM ai_paper_strategies WHERE status='processing'").fetchall()
            if interrupted:
                conn.execute("UPDATE ai_paper_strategies SET status='interrupted', error=? WHERE status='processing'",
                             ("服务重启导致处理中断，已暂停等待对账",))
                self._put_state(conn, "paused", "上次策略处理中断，需核对模拟账")
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _read_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _state(conn, key: str) -> str | None:
        row = conn.execute("SELECT value FROM ai_paper_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _put_state(conn, key: str, value) -> None:
        conn.execute("INSERT INTO ai_paper_state(key,value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, None if value is None else str(value)))

    def _candidate_codes(self, conn, context: dict) -> list[str]:
        codes = [row[0] for row in conn.execute(
            "SELECT code FROM positions WHERE account=? AND shares>0 ORDER BY code", (AI_ACCOUNT,))]
        # 只看沪深主板（dj 2026-11-02）：创业板/科创板/北交所的龙头买不了，不当候选
        for row in context.get("sectorBreadth", {}).get("leaders", [])[:3]:
            code = _plain_code(row.get("leaderCode", ""))
            if len(code) == 6 and code.startswith(("60", "00")) and code not in codes:
                codes.append(code)
        # 不再并入 context["observations"]：那是 dashboard/server.py 写死的三只演示票，不是任何规则选出来的（2026-11-03）
        return codes

    @staticmethod
    def _quote_payload(quotes: dict) -> dict:
        return {code: {
            "name": q.name, "price": q.price, "preClose": q.pre_close, "pct": round(q.pct, 2),
            "openPct": round(q.open_pct, 2), "amount": q.amount, "volume": q.volume,
            "volumeRatio": getattr(q, "volume_ratio", 0), "turnover": getattr(q, "turnover", 0),
            "limitUp": q.limit_up, "limitDown": q.limit_down, "suspended": q.suspended,
            "ts": q.ts.isoformat(),
        } for code, q in quotes.items()}

    def _account_snapshot(self, conn, quotes: dict) -> dict:
        cash = float(conn.execute("SELECT amount FROM cash WHERE account=?", (AI_ACCOUNT,)).fetchone()[0])
        positions = []
        market_value = 0.0
        for row in conn.execute("SELECT * FROM positions WHERE account=? ORDER BY code", (AI_ACCOUNT,)):
            q = quotes.get(row["code"])
            price = q.price if q is not None else row["buy_price"]
            market_value += price * row["shares"]
            positions.append({"code": row["code"], "shares": row["shares"], "buyPrice": row["buy_price"],
                              "buyDate": row["buy_date"], "price": price})
        return {"cash": cash, "equity": round(cash + market_value, 2),
                "marketValue": round(market_value, 2), "positions": positions}

    def _reconcile(self, conn, now: datetime) -> None:
        """从初始资金和全部成交流水独立重算，不信任当前现金和持仓行。"""
        cash_row = conn.execute("SELECT amount FROM cash WHERE account=?", (AI_ACCOUNT,)).fetchone()
        if cash_row is None:
            raise ReconciliationError("模拟账户不存在")
        expected_cash = INITIAL_CAPITAL
        net_shares: dict[str, int] = {}
        last_buy_dates: dict[str, str] = {}
        for row in conn.execute(
                "SELECT * FROM fills WHERE account=? ORDER BY id", (AI_ACCOUNT,)):
            try:
                shares, price, fee = int(row["shares"]), float(row["price"]), float(row["fee"])
                fill_time = datetime.fromisoformat(row["ts"])
            except (TypeError, ValueError) as exc:
                raise ReconciliationError(f"成交 {row['id']} 字段无效") from exc
            if shares <= 0 or shares % 100 or not math.isfinite(price) or price <= 0:
                raise ReconciliationError(f"成交 {row['id']} 的价格或股数无效")
            amount = shares * price
            expected_fee = buy_fee(amount) if row["side"] == "buy" else sell_fee(amount)
            if abs(fee - expected_fee) > 0.011:
                raise ReconciliationError(f"成交 {row['id']} 费用不一致")
            if row["side"] == "buy":
                expected_cash -= amount + fee
                net_shares[row["code"]] = net_shares.get(row["code"], 0) + shares
                last_buy_dates[row["code"]] = row["ts"][:10]
            elif row["side"] == "sell":
                previous_buy = conn.execute(
                    "SELECT ts FROM fills WHERE account=? AND code=? AND side='buy' AND id<? "
                    "ORDER BY id DESC LIMIT 1", (AI_ACCOUNT, row["code"], row["id"])).fetchone()
                if previous_buy is None or fill_time.date() <= datetime.fromisoformat(previous_buy[0]).date():
                    raise ReconciliationError(f"成交 {row['id']} 违反 T+1 或无对应买入")
                expected_cash += amount - fee
                net_shares[row["code"]] = net_shares.get(row["code"], 0) - shares
            else:
                raise ReconciliationError(f"成交 {row['id']} 方向无效")
            if min(net_shares.values(), default=0) < 0:
                raise ReconciliationError(f"成交 {row['id']} 导致负持仓")
        if abs(float(cash_row[0]) - round(expected_cash, 2)) > 0.011:
            raise ReconciliationError("模拟现金与成交流水重算不一致")
        actual = {row["code"]: row for row in conn.execute(
            "SELECT * FROM positions WHERE account=?", (AI_ACCOUNT,))}
        expected = {code: shares for code, shares in net_shares.items() if shares > 0}
        if set(actual) != set(expected):
            raise ReconciliationError("模拟持仓代码与成交流水不一致")
        for code, shares in expected.items():
            if actual[code]["shares"] != shares or actual[code]["buy_date"] != last_buy_dates.get(code):
                raise ReconciliationError(f"{code} 持仓数量或买入日期与流水不一致")

    def _begin(self, record: dict, strategy_context: dict) -> bool:
        conn = self._connect()
        try:
            if self._state(conn, "paused"):
                return False
            if conn.execute("SELECT 1 FROM ai_paper_strategies WHERE decision_id=?",
                            (record["id"],)).fetchone():
                return False
            strategy_id = f"STRAT-{record['id']}"
            conn.execute("INSERT INTO ai_paper_strategies(id,decision_id,ts,model,status,context_json) "
                         "VALUES (?,?,?,?,?,?)",
                         (strategy_id, record["id"], self.clock().isoformat(timespec="seconds"),
                          getattr(self.judge, "model_name", record.get("model", "unknown")), "processing",
                          json.dumps(strategy_context, ensure_ascii=False)))
            conn.commit()
            return True
        finally:
            conn.close()

    def process_record(self, record: dict, context: dict) -> None:
        """DecisionJournal 的回调；同一决策 id 幂等，异常不会伪造成交。"""
        with self._state_lock:
            if self._running or self.judge is None:
                return
            self._running = True
        strategy_id = f"STRAT-{record.get('id', 'unknown')}"
        try:
            conn = self._connect()
            try:
                if self._state(conn, "paused") or conn.execute(
                        "SELECT 1 FROM ai_paper_strategies WHERE decision_id=?", (record["id"],)).fetchone():
                    return
            finally:
                conn.close()
            now = self.clock()
            market, sectors = context.get("marketBreadth", {}), context.get("sectorBreadth", {})
            if (context.get("snapshot", {}).get("phase") != "盘中" or record.get("action") not in
                    {"空仓等待", "仅观察候选", "允许模拟小仓评估"} or
                    not all((market.get("complete"), sectors.get("complete"),
                             not market.get("stale"), not sectors.get("stale")))):
                raise ValueError("盘中证据不完整或已失效，不处理模拟策略")
            conn = self._connect()
            try:
                self._reconcile(conn, now)
                codes = self.fixed_codes if self.fixed_codes is not None else self._candidate_codes(conn, context)
            finally:
                conn.close()
            if not codes:
                raise ValueError("没有可评价的候选代码")
            quotes = self.quote_provider(codes)
            if set(quotes) != set(codes) or not all(_quote_valid(quotes.get(code), now) for code in codes):
                raise ValueError("候选股行情缺失、过期或价格无效")
            conn = self._connect()
            try:
                account = self._account_snapshot(conn, quotes)
            finally:
                conn.close()
            strategy_context = {
                "decision": {key: record.get(key) for key in ("id", "regime", "action", "confidence")},
                "snapshot": context.get("snapshot"), "marketBreadth": market, "sectorBreadth": sectors,
                "account": account, "quotes": self._quote_payload(quotes),
                "constraints": {"account": AI_ACCOUNT, "liveTrading": False, "initialCapital": INITIAL_CAPITAL,
                                "maxPositions": 1, "maxDailyBuys": MAX_DAILY_BUYS,
                                "minConfidence": MIN_CONFIDENCE, "tPlusOne": True, "lot": 100},
            }
            if not self._begin(record, strategy_context):
                return
            answers = self.judge.ask(strategy_context, build_portfolio_questions(quotes))
            portfolio = answers["portfolio"]
            execution = self._execute(record, portfolio, quotes, now)
            conn = self._connect()
            try:
                self._reconcile(conn, now)
                conn.execute("UPDATE ai_paper_strategies SET status='done', portfolio_json=?, execution_json=?, "
                             "error=NULL WHERE id=?",
                             (json.dumps(portfolio, ensure_ascii=False),
                              json.dumps(execution, ensure_ascii=False), strategy_id))
                self._put_state(conn, "lastDecisionId", record["id"])
                self._put_state(conn, "lastError", None)
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            conn = self._connect()
            try:
                if conn.execute("SELECT 1 FROM ai_paper_strategies WHERE id=?", (strategy_id,)).fetchone():
                    conn.execute("UPDATE ai_paper_strategies SET status=?, error=? WHERE id=?",
                                 ("paused" if isinstance(exc, ReconciliationError) else "error", str(exc), strategy_id))
                self._put_state(conn, "lastError", str(exc))
                if isinstance(exc, ReconciliationError):
                    self._put_state(conn, "paused", str(exc))
                conn.commit()
            finally:
                conn.close()
        finally:
            with self._state_lock:
                self._running = False

    def _execute(self, record: dict, portfolio: dict, quotes: dict, now: datetime) -> dict:
        conn = self._connect()
        fills, skipped = [], []
        try:
            self._reconcile(conn, now)
            ledger = Ledger(conn, init=False)
            positions = {p.code: p for p in ledger.positions(AI_ACCOUNT)}
            orders = portfolio["orders"]
            current_time = now.time()
            sell_window = (time(9, 30) <= current_time < time(11, 30) or
                           time(13) <= current_time < time(15))
            for code, order in orders.items():
                if order["action"] != "模拟卖出":
                    continue
                position, quote = positions.get(code), quotes[code]
                reason = f"{record['id']} · {order['reason']}"
                if position is None:
                    skipped.append(f"{code} 无模拟持仓，卖出否决")
                elif position.buy_date >= now.date():
                    skipped.append(f"{code} T+1，卖出否决")
                elif not sell_window:
                    skipped.append(f"{code} 不在模拟卖出时段")
                elif order["confidence"] < MIN_CONFIDENCE:
                    skipped.append(f"{code} 置信度不足，卖出否决")
                else:
                    fill = ledger.sell(AI_ACCOUNT, position, quote, now, reason)
                    if fill:
                        fills.append({"side": "sell", "code": code, "shares": fill.shares, "price": fill.price})
                        positions.pop(code, None)
                    else:
                        skipped.append(f"{code} 停牌或跌停，卖出未成交")
            buy_window = (time(9, 30) <= current_time < time(11, 30) or
                          time(13) <= current_time < time(14, 57))
            buys_today = conn.execute(
                "SELECT COUNT(*) FROM fills WHERE account=? AND side='buy' AND substr(ts,1,10)=?",
                (AI_ACCOUNT, now.date().isoformat())).fetchone()[0]
            if jevq_config.AI_BOARD_ENABLED:
                # dj 2026-11-01：AI 只做打首板，买点在后台 3 秒扫板（aiboard 账户）；这里的「模拟买入」只当表态
                skipped.append("买入交给 AI 打首板执行器（aiboard 账户），本账户不开新仓")
            elif record["action"] != "允许模拟小仓评估":
                skipped.append("大盘研究动作未允许模拟小仓评估，买入全部 veto")
            elif portfolio["positionPct"] <= 0:
                skipped.append("策略总仓位上限为 0，买入全部 veto")
            elif not buy_window:
                skipped.append("不在模拟买入时段，买入全部 veto")
            elif buys_today >= MAX_DAILY_BUYS:
                skipped.append("达到每日模拟买入上限，买入全部 veto")
            else:
                candidates = sorted(orders.items(), key=lambda item: (-item[1]["confidence"], item[0]))
                for code, order in candidates:
                    if order["action"] != "模拟买入":
                        continue
                    quote = quotes[code]
                    if code in positions:
                        skipped.append(f"{code} 已持仓，买入 veto")
                    elif len(positions) >= 1:
                        skipped.append(f"{code} 单票上限，买入 veto")
                    elif order["confidence"] < MIN_CONFIDENCE:
                        skipped.append(f"{code} 置信度不足，买入 veto")
                    elif not code.startswith(("60", "00")) or quote.is_st:
                        skipped.append(f"{code} 非主板或 ST，买入 veto")
                    else:
                        account = self._account_snapshot(conn, quotes)
                        exposure_cap = min(account["equity"], AI_TOTAL_BUDGET) * portfolio["positionPct"]
                        budget = min(account["cash"], exposure_cap - account["marketValue"])
                        reason = f"{record['id']} · {order['reason']}"
                        fill = ledger.buy(AI_ACCOUNT, quote, now, reason, budget=budget) if budget > 0 else None
                        if fill:
                            fills.append({"side": "buy", "code": code, "shares": fill.shares, "price": fill.price})
                            positions = {p.code: p for p in ledger.positions(AI_ACCOUNT)}
                            buys_today += 1
                        else:
                            skipped.append(f"{code} 涨停、停牌或仓位预算不足一手，买入未成交")
                    if len(positions) >= 1 or buys_today >= MAX_DAILY_BUYS:
                        break
            self._reconcile(conn, now)
            return {"fills": fills, "skipped": skipped, "liveTrading": False}
        finally:
            conn.close()

    def check_fast_risk(self, quotes: dict) -> dict:
        """秒级检查模拟持仓的可成交性；只报警，不自动卖出或触碰真实账户。"""
        now = self.clock()
        conn = self._read_connect()
        try:
            codes = [row[0] for row in conn.execute(
                "SELECT code FROM positions WHERE account=? AND shares>0", (AI_ACCOUNT,))]
        finally:
            conn.close()
        alerts = []
        for code in codes:
            quote = quotes.get(code)
            if quote is None:
                alerts.append(f"{code} 秒级行情未返回，等待全市场快照复核")
            elif not _quote_valid(quote, now):
                alerts.append(f"{code} 秒级行情过期或价格无效，禁止新增模拟动作")
            elif quote.suspended:
                alerts.append(f"{code} 停牌，模拟卖出不可成交")
            elif quote.limit_down and quote.price <= quote.limit_down:
                alerts.append(f"{code} 已触及跌停，模拟卖出可能无法成交")
        risk = {"checkedAt": now.isoformat(timespec="seconds"),
                "status": "attention" if alerts else "clear", "alerts": alerts, "liveTrading": False}
        with self._state_lock:
            self._fast_risk = risk
        return risk

    def status(self) -> dict:
        now = self.clock()
        conn = self._read_connect()
        try:
            codes = [row[0] for row in conn.execute(
                "SELECT code FROM positions WHERE account=? AND shares>0", (AI_ACCOUNT,))]
            quotes = self.quote_provider(codes) if codes else {}
            account = self._account_snapshot(conn, quotes)
            today = now.date().isoformat()
            fills_today = conn.execute("SELECT COUNT(*) FROM fills WHERE account=? AND substr(ts,1,10)=?",
                                       (AI_ACCOUNT, today)).fetchone()[0]
            latest = conn.execute("SELECT * FROM ai_paper_strategies ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
            paused = self._state(conn, "paused")
            error = self._state(conn, "lastError") or ""
            last_decision_id = self._state(conn, "lastDecisionId")
            try:
                self._reconcile(conn, now)
                reconciled = True
                reconciliation_error = ""
            except ReconciliationError as exc:
                reconciled = False
                reconciliation_error = str(exc)
        finally:
            conn.close()
        equity = account["equity"]
        latest_strategy = None
        if latest is not None:
            latest_strategy = {"id": latest["id"], "decisionId": latest["decision_id"], "ts": latest["ts"],
                               "model": latest["model"], "status": latest["status"], "error": latest["error"],
                               "portfolio": json.loads(latest["portfolio_json"]) if latest["portfolio_json"] else None,
                               "execution": json.loads(latest["execution_json"]) if latest["execution_json"] else None}
        with self._state_lock:
            running = self._running
            fast_risk = {**self._fast_risk, "alerts": list(self._fast_risk["alerts"])}
        return {
            "enabled": self.judge is not None or self.external, "running": running, "paused": paused,
            "error": error, "reconciled": reconciled, "reconciliationError": reconciliation_error,
            "lastDecisionId": last_decision_id, "fastRisk": fast_risk,
            "latestStrategy": latest_strategy, "liveTrading": False,
            "account": {"available": True, "total": equity, "cash": account["cash"],
                        "positionPct": account["marketValue"] / equity * 100 if equity > 0 else 0,
                        "todayPnl": None, "positions": len(account["positions"]), "positionDetails": account["positions"],
                        "fillsToday": fills_today, "source": "隔离模拟 SQLite · ai_paper.sqlite"},
        }
