import json
import re
import threading
from collections import deque
from datetime import date, datetime
from pathlib import Path

from jevq import accounts as accounts_mod
from jevq import config, db, runlock
from jevq.config import DB_PATH, REPORT_DIR, STRATEGIES
from jevq.data import board_events, custom_store, tencent
from jevq.ledger import Ledger

REPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


class Api:
    """暴露给 pywebview 前端的方法；下划线开头的成员不会暴露给 JS。"""

    def __init__(self, db_path: str = DB_PATH, report_dir: str = REPORT_DIR,
                 runner=None, fetch_quotes=tencent.fetch_quotes):
        if runner is None:
            from run import run_day as runner
        self._db_path, self._report_dir = db_path, Path(report_dir)
        self._runner, self._fetch_quotes = runner, fetch_quotes
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._logs: deque[str] = deque(maxlen=500)
        self._state: dict = {}
        self._names: dict[str, str] = {}
        conn = db.connect(self._db_path)           # 仅此一次写：建库、建表、建现金行；之后查询全走只读连接
        try:
            Ledger(conn)
        finally:
            conn.close()

    # ---- 运行控制 ----
    def start(self) -> dict:
        if self.is_running():
            return {"ok": False, "msg": "已经在运行"}
        if runlock.is_held(Path(self._db_path).parent / runlock.DAEMON_LOCK_NAME):
            return {"ok": False, "msg": "后台程序已接管今日交易，面板只需查看"}
        self._stop = threading.Event()
        self._state.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return {"ok": True, "msg": "已启动"}

    def stop(self) -> dict:
        self._stop.set()
        return {"ok": True, "msg": "正在停止"}

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        self._log("开始运行")
        try:
            self._runner(stop=self._stop, log=self._log, state=self._state)
        except Exception as e:
            self._log(f"出错：{e}")
        finally:
            self._state["phase"] = "未运行"
            self._log("运行结束")

    def _log(self, msg: str):
        self._logs.append(f"{datetime.now():%H:%M:%S} {msg}")

    # ---- 查询 ----
    def _lookup_names(self, codes, quotes: dict | None = None) -> dict[str, str]:
        known = {c: q.name for c, q in (quotes or {}).items()}
        known.update(self._names)
        known.update(self._state.get("names") or {})      # 股票池名称优先级最高
        missing = sorted({c for c in codes if c and c not in known})
        if missing:
            try:
                known.update({c: q.name for c, q in self._fetch_quotes(missing).items()})
            except Exception as e:
                self._log(f"股票名称获取失败：{e}")
        self._names.update({c: known[c] for c in codes if c in known})
        return {c: known.get(c, "") for c in codes}

    def status(self) -> dict:
        conn = db.connect_readonly(self._db_path)
        try:
            ledger = Ledger(conn, init=False)
            today = date.today()
            account_list = accounts_mod.all_accounts(conn, today)
            account_names = accounts_mod.account_names(conn, today)
            account_strategy = accounts_mod.account_strategy(conn, today)
            strategy_names = dict({sid: s["name"] for sid, s in STRATEGIES.items()})
            for s in custom_store.active_for(conn, today) + custom_store.holders(conn, today):
                strategy_names[s["id"]] = s["name"]
            prices = dict(self._state.get("prices") or {})
            held = {p.code for a in account_list for p in ledger.positions(a)}
            missing = sorted(held - prices.keys())
            quotes = {}
            if missing:
                try:
                    quotes = self._fetch_quotes(missing)
                    prices.update({c: q.price for c, q in quotes.items()})
                except Exception as e:
                    self._log(f"持仓行情获取失败：{e}")
            names = self._lookup_names(held, quotes)
            accounts = [self._account(ledger, a, prices, names, account_names, account_strategy, strategy_names)
                       for a in account_list]
        finally:
            conn.close()
        running = self.is_running()
        background = not running and runlock.is_held(Path(self._db_path).parent / "runner.lock")
        daemon = runlock.is_held(Path(self._db_path).parent / runlock.DAEMON_LOCK_NAME)
        return {"running": running, "background": background, "daemon": daemon,
                "phase": self._state.get("phase", "未运行"),
                "overseas": self._state.get("overseas"), "sentiment": self._state.get("sentiment"),
                "warning": self._state.get("warning"), "board_watch": self._state.get("board_watch") or [],
                "today": date.today().isoformat(), "accounts": accounts}

    def _account(self, ledger: Ledger, account: str, prices: dict[str, float], names: dict[str, str],
                account_names: dict[str, str], account_strategy: dict[str, str],
                strategy_names: dict[str, str]) -> dict:
        value = ledger.equity(account, prices)
        positions = []
        for p in ledger.positions(account):
            price = prices.get(p.code)
            positions.append({"code": p.code, "name": names.get(p.code, ""), "shares": p.shares,
                              "buy_price": p.buy_price,
                              "buy_date": p.buy_date.isoformat(), "price": price,
                              "pnl_pct": None if price is None else round((price / p.buy_price - 1) * 100, 2)})
        strategy = account_strategy.get(account, "")
        baseline = accounts_mod.capital_baseline(ledger.conn, strategy)
        return {"id": account, "name": account_names.get(account, account), "equity": value,
                "cash": round(ledger.cash(account), 2),
                "return_pct": round((value / baseline - 1) * 100, 2), "positions": positions,
                "strategy": strategy, "strategy_name": strategy_names.get(strategy, strategy)}

    def fills(self, day: str) -> list[dict]:
        conn = db.connect_readonly(self._db_path)
        try:
            fills = Ledger(conn, init=False).fills_on(date.fromisoformat(day))
            account_names = accounts_mod.account_names(conn, date.today())
        finally:
            conn.close()
        names = self._lookup_names({f.code for f in fills})
        return [{"time": f.ts.strftime("%H:%M"), "account": account_names.get(f.account, f.account),
                 "code": f.code, "name": names[f.code],
                 "side": "买" if f.side == "buy" else "卖", "price": f.price, "shares": f.shares,
                 "fee": f.fee, "reason": f.reason}
                for f in fills]

    def decisions(self, day: str, strategy: str | None = None) -> list[dict]:
        conn = db.connect_readonly(self._db_path)
        try:
            if strategy is None:
                rows = conn.execute(
                    "SELECT id, ts, code, verdict, detail, trace IS NOT NULL FROM decisions "
                    "WHERE substr(ts, 1, 10)=? ORDER BY id DESC LIMIT 300", (day,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, ts, code, verdict, detail, trace IS NOT NULL FROM decisions "
                    "WHERE substr(ts, 1, 10)=? AND COALESCE(strategy, 'lowbuy')=? ORDER BY id DESC LIMIT 300",
                    (day, strategy)).fetchall()
        finally:
            conn.close()
        names = self._lookup_names({code for _, _, code, _, _, _ in rows if code})
        return [{"id": did, "time": ts[11:16], "code": code or "市场",
                 "name": names.get(code, "") if code else "",
                 "verdict": verdict, "detail": detail, "has_trace": bool(has_trace)}
                for did, ts, code, verdict, detail, has_trace in rows]

    def decision(self, decision_id: int) -> dict | None:
        try:
            decision_id = int(decision_id)
        except (TypeError, ValueError):
            return None
        conn = db.connect_readonly(self._db_path)
        try:
            row = conn.execute("SELECT id, ts, code, verdict, detail, trace FROM decisions WHERE id=?",
                               (decision_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        did, ts, code, verdict, detail, raw = row
        try:
            trace = json.loads(raw) if raw else None
        except ValueError:
            trace = None
        name = self._lookup_names({code})[code] if code else ""
        return {"id": did, "time": ts[11:16], "code": code or "市场", "name": name,
                "verdict": verdict, "detail": detail, "trace": trace}

    def equity_curve(self) -> dict[str, list]:
        conn = db.connect_readonly(self._db_path)
        try:
            rows = conn.execute("SELECT date, account, value FROM equity ORDER BY date").fetchall()
            custom_ids = conn.execute("SELECT DISTINCT id FROM custom_strategies").fetchall()
        finally:
            conn.close()
        accounts = list(config.builtin_accounts())
        for (sid,) in custom_ids:
            accounts += [f"{sid}_main", f"{sid}_shadow"]
        out: dict[str, list] = {a: [] for a in accounts}
        for d, a, v in rows:
            out.setdefault(a, []).append([d, v])
        return out

    def reports(self) -> list[str]:
        if not self._report_dir.exists():
            return []
        return sorted((p.name for p in self._report_dir.glob("*.md") if REPORT_NAME.match(p.name)),
                      reverse=True)

    def report(self, name: str) -> str:
        if not REPORT_NAME.match(name):
            raise ValueError(f"非法报告名：{name}")
        return (self._report_dir / name).read_text(encoding="utf-8")

    def logs(self) -> list[str]:
        return list(self._logs)

    def board_stats(self) -> list[dict]:
        conn = db.connect_readonly(self._db_path)
        try:
            return board_events.summary(conn)
        finally:
            conn.close()

    # ---- 规则（自定义策略）CRUD ----
    def rules_list(self) -> dict:
        """返回所有自定义策略（启用 + 停用但持仓）及它们的运行状态。"""
        conn = db.connect_readonly(self._db_path)
        try:
            today = date.today()
            active = custom_store.active_for(conn, today)
            held = custom_store.holders(conn, today)
            all_rules = active + held
            return {"ok": True, "rules": all_rules}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            conn.close()

    def rule_detail(self, sid: str) -> dict:
        """返回指定策略的完整历史版本。"""
        conn = db.connect_readonly(self._db_path)
        try:
            history = custom_store.history(conn, sid)
            if not history:
                return {"ok": False, "error": f"未知策略 {sid}"}
            return {"ok": True, "id": sid, "history": history}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            conn.close()

    def rule_create(self, spec: dict) -> dict:
        """创建新的自定义策略，次日生效。"""
        from jevq.data import akdata
        conn = db.connect(self._db_path)
        try:
            today = date.today()
            days = akdata.trade_days()
            sid, version = custom_store.save(conn, spec, today, days)
            if sid is None:
                return {"ok": False, "errors": version}
            # 创建对应的主账户和影子账户
            capital = spec.get("capital", config.INITIAL_CASH)
            ledger = Ledger(conn, init=False)
            ledger.ensure_account(f"{sid}_main", capital)
            ledger.ensure_account(f"{sid}_shadow", capital)
            conn.commit()
            return {"ok": True, "id": sid, "version": version}
        except Exception as e:
            conn.rollback()
            return {"ok": False, "errors": [str(e)]}
        finally:
            conn.close()

    def rule_update(self, sid: str, spec: dict) -> dict:
        """更新现有策略，次日生效。"""
        from jevq.data import akdata
        conn = db.connect(self._db_path)
        try:
            today = date.today()
            days = akdata.trade_days()
            result_sid, version = custom_store.save(conn, spec, today, days, sid=sid)
            if result_sid is None:
                return {"ok": False, "errors": version}
            return {"ok": True, "id": result_sid, "version": version}
        except Exception as e:
            conn.rollback()
            return {"ok": False, "errors": [str(e)]}
        finally:
            conn.close()

    def rule_disable(self, sid: str) -> dict:
        """停用策略，次日生效；已有持仓照旧按原规则卖出。"""
        from jevq.data import akdata
        conn = db.connect(self._db_path)
        try:
            today = date.today()
            days = akdata.trade_days()
            version, errors = custom_store.disable(conn, sid, today, days)
            if version is None:
                return {"ok": False, "errors": errors}
            return {"ok": True, "version": version}
        except Exception as e:
            conn.rollback()
            return {"ok": False, "errors": [str(e)]}
        finally:
            conn.close()

    def rule_fields(self) -> dict:
        """返回规则引擎的字段元数据。"""
        try:
            from jevq.rules import custom
            meta = custom.fields_meta()
            return {"ok": True, "fields": meta}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def rule_validate(self, spec: dict) -> dict:
        """校验规则定义，返回错误和警告。"""
        try:
            from jevq.rules import custom
            errors, warnings = custom.validate(spec)
            return {"ok": True, "errors": errors, "warnings": warnings}
        except Exception as e:
            import traceback
            return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}

    def rule_preview(self, spec: dict) -> dict:
        """预览规则在最近一次行情上的命中结果（前20只）。"""
        try:
            from jevq.rules import custom
            from jevq.data import features, akdata
            import time

            # 使用缓存的行情数据（60秒内有效）
            now = time.time()
            if not hasattr(self, '_preview_cache') or now - self._preview_cache.get('time', 0) > 60:
                conn = db.connect_readonly(self._db_path)
                try:
                    universe = set(config.UNIVERSE)
                    quotes = self._fetch_quotes(universe)
                    today = date.today()
                    hist_features = features.history_features(conn, universe, today)

                    # 简化的题材映射（不调用board实时数据）
                    concept_map = {}

                    self._preview_cache = {
                        'time': now,
                        'quotes': quotes,
                        'features': hist_features,
                        'concept_map': concept_map,
                        'today': today
                    }
                finally:
                    conn.close()

            cache = self._preview_cache
            quotes = cache['quotes']
            hist_features = cache['features']
            concept_map = cache['concept_map']
            today = cache['today']

            # 评估选股条件
            select_group = spec.get("select", {})
            candidates = []

            for code, q in quotes.items():
                if code not in config.UNIVERSE:
                    continue

                # 构造取值函数
                def make_getter(code, q, hist_features):
                    hist = hist_features.get(code, {})
                    def getter(field, param=None):
                        if field == "pct":
                            return q.pct
                        elif field == "open_pct":
                            return q.open_pct
                        elif field == "high_pct":
                            return q.high_pct
                        elif field == "low_pct":
                            return q.low_pct
                        elif field == "drawdown":
                            return q.high_pct - q.pct
                        elif field == "sealed":
                            limit_up = round(q.prev_close * 1.1, 2)
                            return q.price >= limit_up
                        elif field == "broke":
                            limit_up = round(q.prev_close * 1.1, 2)
                            return q.high >= limit_up and q.price < limit_up
                        elif field == "amount":
                            return q.amount / 100_000_000
                        elif field == "turnover":
                            return q.turnover
                        elif field == "volume_ratio":
                            return q.volume_ratio
                        elif field == "float_mcap":
                            return q.float_mcap
                        elif field == "total_mcap":
                            return q.total_mcap
                        elif field == "ret_n":
                            return hist.get("ret", {}).get(param)
                        elif field == "limit_up_in_n":
                            return hist.get("limit_up", {}).get(param)
                        elif field == "boards":
                            return hist.get("boards")
                        elif field == "ma_pos_n":
                            ma = hist.get("ma", {}).get(param)
                            if ma is None:
                                return None
                            return round((q.price / ma - 1) * 100, 2)
                        elif field == "concept_heat":
                            concepts = concept_map.get(code, [])
                            if not concepts:
                                return 0
                            return max(len(concept_map.get(c, [])) for c in concepts)
                        elif field == "time":
                            return datetime.now().time()
                        return None
                    return getter

                getter = make_getter(code, q, hist_features)

                # 评估选股条件
                if custom.eval_group(select_group, getter):
                    candidates.append((code, q, getter))

            # 排序
            rank_spec = spec.get("rank")
            if rank_spec:
                field = rank_spec.get("field")
                param_n = (rank_spec.get("param") or {}).get("n")
                order = rank_spec.get("order", "desc")

                def rank_key(item):
                    code, q, getter = item
                    val = getter(field, param_n)
                    if val is None:
                        return float('-inf') if order == "desc" else float('inf')
                    return val

                candidates.sort(key=rank_key, reverse=(order == "desc"))

            # 取前20只，返回字段值
            rows = []
            for code, q, getter in candidates[:20]:
                values = {}
                # 收集所有在选股条件中用到的字段
                for cond in select_group.get("conditions", []):
                    field_key = cond.get("field")
                    if not field_key:
                        continue
                    f = custom.FIELDS.get(field_key)
                    if not f:
                        continue
                    param_n = (cond.get("param") or {}).get("n")
                    val = getter(field_key, param_n)
                    label = f.label
                    if param_n:
                        label = f"{f.label}(N={param_n})"

                    if val is None:
                        values[label] = "—"
                    elif isinstance(val, bool):
                        values[label] = "是" if val else "否"
                    elif isinstance(val, (int, float)):
                        values[label] = f"{val:.2f}{f.unit}"
                    else:
                        values[label] = str(val)

                rows.append({
                    "code": code,
                    "name": q.name,
                    "rank": len(rows) + 1,
                    "values": values
                })

            return {
                "ok": True,
                "total": len(candidates),
                "rows": rows
            }

        except Exception as e:
            import traceback
            return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
