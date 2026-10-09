import json
import re
import threading
import time as time_mod
from collections import deque
from datetime import date, datetime
from pathlib import Path

from jevq import accounts as accounts_mod
from jevq import config, db, runlock
from jevq.config import DB_PATH, REPORT_DIR, STRATEGIES
from jevq.data import board_events, concepts, custom_store, tencent
from jevq.data import features as features_mod
from jevq.ledger import Ledger
from jevq.rules import custom as rules_custom
from jevq.strategies import custom as custom_runtime


def _rebound_volume_lines() -> list[str]:
    """超跌反弹的成交量条件说明；REBOUND_MIN_SHRINK_DAYS = 0 表示不看成交量，不写这几行。"""
    if config.REBOUND_MIN_SHRINK_DAYS <= 0:
        return []
    return [f"成交量：连续缩量（含今天、不看涨跌）至少 {config.REBOUND_MIN_SHRINK_DAYS} 天；"
            f"盘中按已开盘分钟折算全天量，09:45 前不判断，尾盘最准",
            f"连续缩量 {config.REBOUND_PRIORITY_SHRINK_DAYS} 天及以上的优先"]


def builtin_summaries() -> list[dict]:
    """内置策略的只读说明（面板「规则」页左侧列表），数字取自配置常量，改参数时说明跟着变。"""
    from jevq.rules import board as board_rules
    from jevq.rules import board_sell

    hm = lambda t: t.strftime("%H:%M")      # noqa: E731
    windows = "、".join(f"{hm(a)}~{hm(b)}" for a, b in board_rules.SCAN_WINDOWS)
    return [
        {"id": "board", "name": STRATEGIES["board"]["name"], "accounts": list(STRATEGIES["board"]["accounts"]),
         "sections": [
             {"title": "选股", "lines": [
                 f"题材内至少 {board_rules.HOT_MIN} 只涨超 {board_rules.HOT_PCT:g}%",
                 f"首板（昨日未涨停），涨幅 ≥ {board_rules.ENTER_PCT:g}% 进观察池，最多 {board_rules.WATCH_MAX} 只，"
                 f"跌破 {board_rules.EXIT_PCT:g}% 移出",
                 "hi189 晚报点名的热门题材排在前面（只调顺序，不降门槛）"]},
             {"title": "买点", "lines": [
                 f"扫描窗口 {windows} 内每 3 秒检查，涨幅到 {board_rules.TRIGGER_PCT:g}% 触发",
                 "主账户经 Jev 排雷，影子账户不拦截",
                 f"情绪闸门（试验中，只记录不拦截）：上一交易日收盘前炸板率高于 {board_rules.GATE_BREAK_RATE:g}%、"
                 f"跌停多于 {board_rules.GATE_LIMIT_DOWN} 家或最高连板不到 {board_rules.GATE_MIN_BOARD} 板时，"
                 "当天记为「会拦」，买卖照旧"]},
             {"title": "卖出（次日起）", "lines": [
                 f"跌到 {board_sell.STOP:g}% 止损；封死涨停继续持有",
                 f"炸板卖；冲高回落 {board_sell.PULLBACK:g} 点卖",
                 f"{hm(config.EOD_SELL)} 尾盘清仓"]}]},
        {"id": "rebound", "name": STRATEGIES["rebound"]["name"], "accounts": list(STRATEGIES["rebound"]["accounts"]),
         "sections": [
             {"title": "选股", "lines": [
                 f"主板 60/00、非 ST、上市满 {config.MIN_LISTED_TRADING_DAYS} 个交易日",
                 f"当前涨幅在 {config.BUY_ZONE_LOW:g}% ~ {config.BUY_ZONE_HIGH:g}% 之间",
                 f"近 5 日涨幅在 {config.REBOUND_RET5_LOW:g}% ~ {config.REBOUND_RET5_HIGH:g}% 之间",
                 f"近 {config.REBOUND_BOARD_LOOKBACK} 个交易日出现过 {config.REBOUND_MIN_BOARDS} 连板的排除",
                 *_rebound_volume_lines(),
                 ("同档内" if config.REBOUND_MIN_SHRINK_DAYS > 0 else "")
                 + "按近 5 日涨幅从低到高排序（跌得多的优先）"]},
             {"title": "买点", "lines": [
                 f"只看情绪：正常或火热时盘中可买（到 {hm(config.BUY_END)}），偏弱、冰点或情绪没判出来时不买（不看外围）",
                 f"情绪拐点抄底：今天红盘占比最低到过 {config.TURN_PANIC_UP * 100:g}% 以下（恐慌），之后不再创新低满 "
                 f"{config.TURN_LOW_AGE} 分钟、红盘回到 {config.TURN_UP_MIN * 100:g}% 以上且比最低多 {config.TURN_UP_GAIN * 100:g} 个点、"
                 f"跌停降到 {config.TURN_LD_FLOOR} 家以内或最多时的 {config.TURN_LD_SHRINK * 100:g}%、成交额较昨日同期在放大，"
                 + ("再经 Jev（看广度轨迹、外围、7x24 快讯、知识库经验）确认修复拐点" if config.TURN_JEV_REQUIRED
                    else "规则满足就确认（Jev 的看法只记录）")
                 + f" → 偏弱/冰点也立即盘中买，实盘跟（"
                 f"{hm(config.TURN_START)}~{hm(config.TURN_END)}）",
                 "情绪判断会参考 hi189 当日市场解读文章（取不到时和以前一样）",
                 f"主账户经 Jev 公告排雷 + 消息面判断，每分钟最多检查 {config.MAX_CHECKS_PER_TICK} 只",
                 f"同一只票当天卖出后，现价要比卖出价低 {config.REBOUND_REBUY_DROP:g}% 以上才允许再买（防止卖出后原价买回）"]},
             {"title": "卖出（次日起）", "lines": [
                 f"相对买入价亏到 {config.REBOUND_STOP:g}% 止损；主账户要连续 {config.REBOUND_STOP_CONFIRM} 分钟在线下才算，"
                 f"亏到 {config.REBOUND_HARD_STOP:g}% 直接卖（硬止损）",
                 f"开盘 ≤ {config.REBOUND_LOW_OPEN:g}% 时，从当天最低拉起 {config.REBOUND_LOW_OPEN_BOUNCE:g} 个点必须卖",
                 "主账户止损、低开反抽前先问 Jev 今天走向：判「" + "/".join(config.REBOUND_TREND_HOLD)
                 + "」先留着，下一分钟再看；Jev 没答上照卖。影子账户不问，当对照组",
                 f"当天最高盈利到 +{config.REBOUND_TRAIL_START:g}% 后，从最高回落 {config.REBOUND_TRAIL_PULLBACK:g} 个点卖；"
                 f"最近 {config.REBOUND_FLOW_MINUTES} 分钟主买大于主卖时先留着，跌回成本价必卖",
                 f"最多拿 {config.REBOUND_MAX_HOLD_DAYS} 个交易日，最后一天 {hm(config.EOD_SELL)} 清仓"]}]},
    ]


REPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
PREVIEW_TTL = 60.0      # 试跑行情缓存秒数
KNOWLEDGE_PATH = Path(__file__).with_name("knowledge.json")


class Api:
    """暴露给 pywebview 前端的方法；下划线开头的成员不会暴露给 JS。"""

    def __init__(self, db_path: str = DB_PATH, report_dir: str = REPORT_DIR,
                 runner=None, fetch_quotes=tencent.fetch_quotes, load_universe=None, trade_days=None):
        if runner is None:
            from run import run_day as runner
        self._db_path, self._report_dir = db_path, Path(report_dir)
        self._runner, self._fetch_quotes = runner, fetch_quotes
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._logs: deque[str] = deque(maxlen=500)
        self._state: dict = {}
        self._names: dict[str, str] = {}
        # 股票池/交易日历：None 表示用 akdata（联网），测试注入假的
        self._load_universe, self._trade_days = load_universe, trade_days
        self._universe_cache: tuple[date, dict[str, str]] | None = None
        self._preview_cache: dict | None = None
        conn = db.connect(self._db_path)           # 建库、建表、建现金行；之后查询全走只读连接（只有规则保存/停用另开写连接）
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
            market = {"sentiment": self._state.get("sentiment"), "overseas": self._state.get("overseas")}
            if market["sentiment"] is None and market["overseas"] is None:
                market = self._todays_market(conn, today)
        finally:
            conn.close()
        running = self.is_running()
        background = not running and runlock.is_held(Path(self._db_path).parent / "runner.lock")
        daemon = runlock.is_held(Path(self._db_path).parent / runlock.DAEMON_LOCK_NAME)
        return {"running": running, "background": background, "daemon": daemon,
                "phase": self._state.get("phase", "未运行"),
                "overseas": market["overseas"], "sentiment": market["sentiment"],
                "warning": self._state.get("warning"), "board_watch": self._state.get("board_watch") or [],
                "today": date.today().isoformat(), "accounts": accounts}

    @staticmethod
    def _todays_market(conn, today: date) -> dict:
        """顶栏的情绪/外围：面板自己没在跑时（交易由后台 daemon 接管），读今天最近一条情绪记录；今天没有就是空。"""
        row = conn.execute("SELECT detail FROM decisions WHERE verdict='sentiment' AND substr(ts, 1, 10) = ? "
                           "ORDER BY ts DESC, id DESC LIMIT 1", (today.isoformat(),)).fetchone()
        try:
            detail = json.loads(row[0]) if row and row[0] else {}
        except ValueError:
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        return {"sentiment": detail.get("label"), "overseas": detail.get("overseas")}

    def _account(self, ledger: Ledger, account: str, prices: dict[str, float], names: dict[str, str],
                account_names: dict[str, str], account_strategy: dict[str, str],
                strategy_names: dict[str, str]) -> dict:
        strategy = account_strategy.get(account, "")
        baseline = accounts_mod.capital_baseline(ledger.conn, strategy)
        opened = ledger.conn.execute("SELECT 1 FROM cash WHERE account=?", (account,)).fetchone()
        if opened is None:
            # 自定义策略今天生效、但交易进程还没构造 CustomStrategy 开户（盘前/进程未起）：
            # 没有现金行，按本金空仓显示，不能让 status 跨桥抛异常把整个面板拖垮
            return {"id": account, "name": account_names.get(account, account), "equity": baseline,
                    "cash": baseline, "baseline": baseline, "return_pct": 0.0, "positions": [],
                    "strategy": strategy, "strategy_name": strategy_names.get(strategy, strategy)}
        value = ledger.equity(account, prices)
        positions = []
        for p in ledger.positions(account):
            price = prices.get(p.code)
            positions.append({"code": p.code, "name": names.get(p.code, ""), "shares": p.shares,
                              "buy_price": p.buy_price,
                              "buy_date": p.buy_date.isoformat(), "price": price,
                              "pnl_pct": None if price is None else round((price / p.buy_price - 1) * 100, 2)})
        return {"id": account, "name": account_names.get(account, account), "equity": value,
                "cash": round(ledger.cash(account), 2), "baseline": baseline,
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
                 "account_id": f.account, "code": f.code, "name": names[f.code],
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
                    "WHERE substr(ts, 1, 10)=? AND strategy=? ORDER BY id DESC LIMIT 300",
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
            if a in out:                     # 已下线的低吸 main/shadow 净值留在库里，面板不画
                out[a].append([d, v])
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

    def candidates(self) -> dict:
        """「候选」页：后台最近一次写下的候选快照（扫首板观察池、超跌反弹入选名单，均已按优先顺序）。"""
        from jevq.data import candidates
        conn = db.connect_readonly(self._db_path)
        try:
            return candidates.latest(conn)
        finally:
            conn.close()

    def sentiment_board(self, days: int = 60) -> dict:
        """「情绪」页：每天一根情绪 K 线（Jev 情绪分的开高低收）+ 涨停/跌停/炸板率/拐点（只读）。"""
        from jevq.app import sentiment_board
        conn = db.connect_readonly(self._db_path)
        try:
            return sentiment_board.board(conn, int(days))
        except Exception as e:
            self._log(f"情绪面板读取失败：{e}")
            return {"days": []}
        finally:
            conn.close()

    def own_sectors(self) -> dict:
        """「自建板块」页（只记录，不参与交易）：每个板块最近一天快照 + 成员（含依据、待确认）。"""
        from jevq.data import own_sectors
        conn = db.connect_readonly(self._db_path)
        try:
            return own_sectors.overview(conn)
        except Exception as e:      # 库里还没有这些表（后台没升级到新代码）→ 空页面，不让面板报错
            self._log(f"自建板块读取失败：{e}")
            return {"date": None, "sectors": []}
        finally:
            conn.close()

    def own_sector_review(self, sector_id: str, code: str, decision: str) -> dict:
        """确认/驳回一个成员（pending 或 auto）。只改自建板块自己的表；驳回的以后不会再被自动加入。"""
        status = {"confirm": "confirmed", "reject": "rejected"}.get(decision)
        if status is None:
            return {"ok": False, "errors": ["decision 只能是 confirm 或 reject"]}
        conn = None
        try:
            conn = db.connect(self._db_path)
            cur = conn.execute("UPDATE own_members SET status=? WHERE sector_id=? AND code=?",
                               (status, str(sector_id), str(code)))
            conn.commit()
            return {"ok": cur.rowcount == 1, "errors": [] if cur.rowcount == 1 else ["没有这个成员"]}
        except Exception as e:
            return {"ok": False, "errors": [str(e)]}
        finally:
            if conn is not None:
                conn.close()

    def own_sector_decide(self, sector_id: str, decision: str, product_def: str = "") -> dict:
        """处理晚报提议的新板块：activate（可同时补上产品定义）/ reject。"""
        status = {"activate": "active", "reject": "rejected"}.get(decision)
        if status is None:
            return {"ok": False, "errors": ["decision 只能是 activate 或 reject"]}
        if status == "active" and len(str(product_def).strip()) < 6:
            return {"ok": False, "errors": ["启用新板块必须写清楚产品定义（至少几个字，说明核心产品是什么）"]}
        conn = None
        try:
            conn = db.connect(self._db_path)
            if status == "active":
                cur = conn.execute("UPDATE own_sectors SET status='active', product_def=? WHERE id=? "
                                   "AND status='proposed'", (str(product_def).strip(), str(sector_id)))
            else:
                cur = conn.execute("UPDATE own_sectors SET status='rejected' WHERE id=? AND status='proposed'",
                                   (str(sector_id),))
            conn.commit()
            return {"ok": cur.rowcount == 1, "errors": [] if cur.rowcount == 1 else ["没有这个待确认板块"]}
        except Exception as e:
            return {"ok": False, "errors": [str(e)]}
        finally:
            if conn is not None:
                conn.close()

    def knowledge(self) -> dict:
        """知识库（专题手册 + 文章索引）：只读，内容整份放在 knowledge.json，要改就改那个文件。"""
        return json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))

    # ---- 规则（自定义策略） ----
    # 写操作一律用 db.connect（WAL + 15 秒 busy timeout，custom_store.save/disable 内部 BEGIN IMMEDIATE），
    # 与交易进程并发写不会互相踩；只读查询用只读连接。坏输入/异常一律返回 {"ok": False, "errors": [...]}，
    # 不跨桥抛异常。交易日历、股票池都走构造时注入的函数，测试里不联网。
    def _calendar(self) -> list[date]:
        if self._trade_days is not None:
            return self._trade_days()
        from jevq.data import akdata
        return akdata.trade_days()

    def _universe(self, today: date) -> dict[str, str]:
        """股票池与 run.py 同口径（akdata.load_universe：主板、非 ST、上市满 60 日），按天缓存。"""
        if self._universe_cache and self._universe_cache[0] == today:
            return self._universe_cache[1]
        load = self._load_universe
        if load is None:
            from jevq.data import akdata
            load = akdata.load_universe
        universe = load(today, self._calendar())
        self._universe_cache = (today, universe)
        self._names.update(universe)
        return universe

    def _preview_data(self) -> dict:
        """试跑用的全市场行情 + 历史特征 + 题材，60 秒内复用（面板每改一个条件就会试跑一次）。"""
        now = time_mod.monotonic()
        cache = self._preview_cache
        if cache and now - cache["at"] < PREVIEW_TTL and cache["today"] == date.today():
            return cache
        today = date.today()
        universe = self._universe(today)
        quotes = self._fetch_quotes(sorted(universe))
        conn = db.connect_readonly(self._db_path)
        try:
            features = features_mod.history_features(conn, list(universe), today)
            concept_map = concepts.load_concepts(conn)
        finally:
            conn.close()
        self._preview_cache = {"at": now, "today": today, "universe": universe, "quotes": quotes,
                               "features": features, "concepts": concept_map}
        return self._preview_cache

    def rules_list(self) -> dict:
        """全部自定义策略（含明天才生效的新建策略、已停用的），每个 id 一行，看最新版本。"""
        conn = db.connect_readonly(self._db_path)
        try:
            today = date.today()
            running = {s["id"] for s in custom_store.active_for(conn, today) + custom_store.holders(conn, today)}
            held = {a.rsplit("_", 1)[0] for (a,) in conn.execute("SELECT DISTINCT account FROM positions")}
            ids = [sid for (sid,) in conn.execute("SELECT DISTINCT id FROM custom_strategies ORDER BY id")]
            rules = []
            for sid in ids:
                latest = custom_store.history(conn, sid)[-1]
                rules.append({"id": sid, "name": latest["name"], "version": latest["version"],
                              "spec": latest["spec"], "active_from": latest["active_from"],
                              "enabled": latest["enabled"], "running": sid in running,
                              "can_buy": latest["enabled"],      # 兼容旧字段，与 enabled 相同
                              "held": sid in held})
            return {"ok": True, "rules": rules, "builtin": builtin_summaries()}
        except Exception as e:
            return {"ok": False, "error": str(e), "errors": [str(e)]}
        finally:
            conn.close()

    def rule_detail(self, sid: str) -> dict:
        """返回指定策略的完整历史版本。"""
        conn = db.connect_readonly(self._db_path)
        try:
            history = custom_store.history(conn, sid)
            if not history:
                return {"ok": False, "error": f"未知策略 {sid}", "errors": [f"未知策略 {sid}"]}
            return {"ok": True, "id": sid, "history": history}
        except Exception as e:
            return {"ok": False, "error": str(e), "errors": [str(e)]}
        finally:
            conn.close()

    def _save(self, spec, sid: str | None) -> dict:
        """新建/新版本，次日生效。账户资金不在这里开：CustomStrategy 生效当天按 capital_of 开户。"""
        conn = None
        try:
            conn = db.connect(self._db_path)
            new_sid, version = custom_store.save(conn, spec, date.today(), self._calendar(), sid=sid)
            if new_sid is None:
                return {"ok": False, "errors": version}
            return {"ok": True, "id": new_sid, "version": version}
        except Exception as e:
            if conn is not None:
                conn.rollback()
            return {"ok": False, "errors": [str(e)]}
        finally:
            if conn is not None:
                conn.close()

    def rule_create(self, spec: dict) -> dict:
        """创建新的自定义策略，次日生效。"""
        return self._save(spec, None)

    def rule_update(self, sid: str, spec: dict) -> dict:
        """更新现有策略，次日生效。"""
        if not isinstance(sid, str) or not sid:
            return {"ok": False, "errors": ["缺少策略 id"]}
        return self._save(spec, sid)

    def rule_disable(self, sid: str) -> dict:
        """停用策略，次日生效；已有持仓照旧按原规则卖出。"""
        conn = None
        try:
            conn = db.connect(self._db_path)
            version, errors = custom_store.disable(conn, sid, date.today(), self._calendar())
            if version is None:
                return {"ok": False, "errors": errors}
            return {"ok": True, "version": version}
        except Exception as e:
            if conn is not None:
                conn.rollback()
            return {"ok": False, "errors": [str(e)]}
        finally:
            if conn is not None:
                conn.close()

    def rule_fields(self) -> dict:
        """返回规则引擎的字段元数据。"""
        try:
            return {"ok": True, "fields": rules_custom.fields_meta()}
        except Exception as e:
            return {"ok": False, "error": str(e), "errors": [str(e)]}

    def rule_validate(self, spec: dict) -> dict:
        """校验规则定义，返回错误和警告。"""
        try:
            errors, warnings = rules_custom.validate(spec)
            return {"ok": True, "errors": errors, "warnings": warnings}
        except Exception as e:
            return {"ok": False, "error": str(e), "errors": [str(e)], "warnings": []}

    def _preview_env(self) -> dict:
        """试跑用的市场情绪/外围：面板自己在跑交易时用面板状态；否则（通常是后台 daemon 在跑）
        从决策表里最近一条情绪记录读——面板状态是空的，不读的话含这两个条件的规则试跑恒为 0。"""
        env = {"sentiment": self._state.get("sentiment"), "overseas": self._state.get("overseas")}
        if env["sentiment"] is not None or env["overseas"] is not None:
            return env
        conn = db.connect_readonly(self._db_path)
        try:
            row = conn.execute("SELECT detail FROM decisions WHERE verdict='sentiment' "
                               "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        try:
            detail = json.loads(row[0]) if row and row[0] else {}
        except ValueError:
            detail = {}
        if isinstance(detail, dict):
            env = {"sentiment": detail.get("label"), "overseas": detail.get("overseas")}
        return env

    def rule_preview(self, spec: dict) -> dict:
        """试跑：最近一次全市场行情上 选股 → 排序 → 买点，返回命中总数与前 20 只。
        与盘中 CustomStrategy 共用同一个取值函数（jevq.strategies.custom.make_getter）；不调用 Jev、不下单。"""
        if not isinstance(spec, dict):
            return {"ok": False, "errors": ["策略格式错误"]}
        try:
            data = self._preview_data()
            env = self._preview_env()
            result = custom_runtime.preview(spec, data["quotes"], data["universe"], data["features"],
                                            data["concepts"], env, datetime.now())
            return {"ok": True, **result}
        except Exception as e:
            self._log(f"试跑失败：{e!r}")
            return {"ok": False, "errors": [f"试跑失败：{e}"]}
