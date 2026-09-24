"""自定义策略运行时：按用户配置的规则选股、买入、卖出。"""
import json
import sqlite3
from datetime import date, datetime, time

from jevq.config import MAX_CHECKS_PER_TICK, MAX_ERRORS_PER_TICK, MAX_PER_TRADE
from jevq.ledger import Ledger, affordable_shares
from jevq.models import Fill, Position, Quote
from jevq.rules import board as board_rules
from jevq.rules.custom import FIELDS, eval_cond, eval_group, first_exit, rank_codes
from jevq.rules.metrics import avg_pct, body_pct, committee, outer_ratio, seal_amount, seal_ratio


def minute_context(quotes: dict[str, Quote], universe, concept_map: dict[str, list[str]],
                   features: dict[str, dict] | None = None):
    """每分钟一次的全市场预计算：(amount_ranks, concept_heats, leader_ranks)。
    amount_rank：股票池内按成交额排名（1 起）；concept_heat：该股所属题材中当日涨超 5% 成员数最大值；
    leader_rank：见 leader_ranks。"""
    uni_quotes = sorted(((c, q) for c, q in quotes.items() if c in universe),
                        key=lambda cq: cq[1].amount, reverse=True)
    amount_ranks = {c: i + 1 for i, (c, _q) in enumerate(uni_quotes)}
    concept_counts: dict[str, int] = {}
    for code, q in uni_quotes:
        if q.pct > 5.0:
            for concept in concept_map.get(code, []):
                concept_counts[concept] = concept_counts.get(concept, 0) + 1
    concept_heats = {code: max((concept_counts.get(c, 0) for c in concept_map.get(code, [])), default=0)
                     for code in universe}
    return amount_ranks, concept_heats, leader_ranks(dict(uni_quotes), concept_map, features or {})


def leader_ranks(quotes: dict[str, Quote], concept_map: dict[str, list[str]],
                 features: dict[str, dict]) -> dict[str, int]:
    """题材内龙头名次，与扫首板 board.leader 同口径：题材的强势成员（非 ST、未停牌、涨幅 ≥ HOT_PCT）里，
    按「涨幅名次 + 成交额名次 + 5 日涨幅名次」之和升序（同分看涨幅名次），第 1 名就是 board.leader 选出的龙头。
    一只股票属于多个题材时取最热的那个（强势成员最多；同样热取名次更好的）。
    自己不是强势成员、或缺 5 日涨幅 → 没有名次（条件不满足）。题材热度门槛交给「题材热度」字段，这里不设。
    quotes 应只含股票池内代码。"""
    ret5 = {}
    for code in quotes:
        v = ((features.get(code) or {}).get("ret") or {}).get(5)
        if v is not None:
            ret5[code] = v
    strong: dict[str, list[str]] = {}
    for code, q in quotes.items():
        if q.is_st or q.suspended or q.pct < board_rules.HOT_PCT:
            continue
        for concept in concept_map.get(code, []):
            strong.setdefault(concept, []).append(code)
    best: dict[str, tuple[int, int]] = {}         # code → (热度, 名次)
    for members in strong.values():
        pool = [c for c in members if c in ret5]
        if not pool:
            continue
        rp = board_rules._rank(pool, lambda c: quotes[c].pct)
        ra = board_rules._rank(pool, lambda c: quotes[c].amount)
        rr = board_rules._rank(pool, lambda c: ret5[c])
        ordered = sorted(pool, key=lambda c: (rp[c] + ra[c] + rr[c], rp[c]))
        heat = len(members)
        for i, code in enumerate(ordered, 1):
            prev = best.get(code)
            if prev is None or heat > prev[0] or (heat == prev[0] and i < prev[1]):
                best[code] = (heat, i)
    return {code: rank for code, (_heat, rank) in best.items()}


def hold_days(buy_date: date, trade_days: list[date]) -> int:
    """持有天数（按交易日计）：trade_days 中晚于买入日的天数。trade_days 应截止到今天（含）。"""
    return sum(1 for d in trade_days or () if d > buy_date)


def make_getter(code: str, q: Quote, *, features: dict, amount_ranks: dict[str, int],
                concept_heats: dict[str, int], env: dict, t: time,
                pos: Position | None = None, trade_days: list[date] = (),
                leader_ranks: dict[str, int] | None = None):
    """取值函数 get(key, n)：覆盖全部字段，缺数据返回 None。运行时（CustomStrategy）与试跑预览共用，
    保证「预览命中」和「盘中命中」是同一套口径。"""
    feat = features

    def get(key: str, n: int | None = None):
        # 价格相关
        if key == "pct":
            return q.pct
        if key == "open_pct":
            return q.open_pct
        if key == "high_pct":
            return q._pct(q.high)
        if key == "low_pct":
            return q.low_pct
        if key == "drawdown":
            return round(q._pct(q.high) - q.pct, 2)
        if key == "sealed":
            return q.price >= q.limit_up
        if key == "broke":
            return q.high >= q.limit_up and q.price < q.limit_up
        if key == "amplitude":
            return q.amplitude
        if key == "avg_pct":
            return avg_pct(q)
        if key == "body_pct":
            return body_pct(q)
        if key == "ytd_pct":
            return q.ytd_pct

        # 量能相关
        if key == "amount":
            return round(q.amount / 1e8, 4)
        if key == "amount_rank":
            return amount_ranks.get(code)
        if key == "turnover":
            return q.turnover
        if key == "volume_ratio":
            return q.volume_ratio
        if key == "committee":
            return committee(q)
        if key == "outer_ratio":
            return outer_ratio(q)
        if key == "seal_amount":
            return seal_amount(q)
        if key == "seal_ratio":
            return seal_ratio(q)

        # 估值相关
        if key == "float_mcap":
            return q.float_mcap
        if key == "total_mcap":
            return q.total_mcap
        if key == "pe_dyn":
            return q.pe_dyn if q.pe_dyn != 0 else None
        if key == "pe_static":
            return q.pe_static if q.pe_static != 0 else None
        if key == "pe_ttm":
            return q.pe_ttm if q.pe_ttm != 0 else None
        if key == "pb":
            return q.pb if q.pb != 0 else None
        if key == "float_shares":
            return round(q.float_shares / 1e8, 4) if q.float_shares > 0 else None
        if key == "total_shares":
            return round(q.total_shares / 1e8, 4) if q.total_shares > 0 else None

        # 历史相关
        if key == "ret_n":
            return feat.get("ret", {}).get(n)
        if key == "limit_up_in_n":
            return feat.get("limit_up", {}).get(n)
        if key == "boards":
            return feat.get("boards")
        if key == "ma_pos_n":
            ma = feat.get("ma", {}).get(n)
            if ma is None or ma <= 0:
                return None
            return round((q.price / ma - 1) * 100, 2)
        if key == "up_streak":
            return feat.get("up_streak")

        # 题材相关
        if key == "concept_heat":
            return concept_heats.get(code)
        if key == "leader_rank":
            return (leader_ranks or {}).get(code)

        # 持仓相关
        if key == "hold_days":
            if pos is None:
                return None
            return hold_days(pos.buy_date, trade_days)
        if key == "pnl_pct":
            if pos is None:
                return None
            return round((q.price / pos.buy_price - 1) * 100, 2)

        # 环境相关
        if key == "time":
            return t
        if key == "sentiment":
            return env.get("sentiment")
        if key == "overseas":
            return env.get("overseas")

        return None

    return get

def preview(spec: dict, quotes: dict[str, Quote], universe, features: dict[str, dict],
            concept_map: dict[str, list[str]], env: dict, now: datetime, limit: int = 20) -> dict:
    """试跑：选股 → 排序 → 买点，与 CustomStrategy 同一套取值函数与规则引擎；不调用 Jev、不下单。
    与盘中不同，不过滤非当日行情（收盘后/盘前预览用最近一个交易日的行情，spec §4.2）。"""
    amount_ranks, concept_heats, leaders = minute_context(quotes, universe, concept_map, features)
    getters = {}

    def getter(code):
        if code not in getters:
            getters[code] = make_getter(code, quotes[code], features=features.get(code) or {},
                                        amount_ranks=amount_ranks, concept_heats=concept_heats,
                                        env=env, t=now.time(), leader_ranks=leaders)
        return getters[code]

    selected = [c for c in sorted(quotes) if c in universe
                and eval_group(spec.get("select"), getter(c), empty=False)]
    ranked = rank_codes(selected, lambda c, key, n: getter(c)(key, n), spec.get("rank"))
    hits = [c for c in ranked if eval_group(spec.get("entry"), getter(c), empty=True)]

    shown = []       # 要展示的字段：选股/买点条件 + 排序字段，去重保序
    conds = [*((spec.get("select") or {}).get("conditions") or []),
             *((spec.get("entry") or {}).get("conditions") or []), spec.get("rank") or {}]
    for c in conds:
        f = FIELDS.get(c.get("field"))
        n = (c.get("param") or {}).get("n")
        if f is not None and (f.key, n) not in shown:
            shown.append((f.key, n))

    rows = []
    for rank, code in enumerate(hits[:limit], 1):
        get = getter(code)
        values = {}
        for key, n in shown:
            f = FIELDS[key]
            label = f"{f.label}(N={n})" if n is not None else f.label
            values[label] = _fmt_value(get(key, n), f.unit)
        rows.append({"code": code, "name": universe.get(code) or quotes[code].name, "rank": rank,
                     "values": values})
    return {"total": len(hits), "rows": rows}


def _fmt_value(v, unit: str) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, int):
        return f"{v}{unit}"
    if isinstance(v, float):          # 小数统一两位（-2.60% 与 -2.21% 对齐），不再用 :g 忽长忽短
        return f"{v:.2f}{unit}"
    if isinstance(v, time):
        return v.strftime("%H:%M")
    return str(v)


class CustomStrategy:
    """自定义策略运行时：主账户走 Jev，影子账户不走。"""

    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, sid: str, version: int, spec: dict,
                 notice_judge, news_judge, universe: dict[str, str], features: dict[str, dict],
                 concept_map: dict[str, list[str]], env_provider, prev_day: date, can_buy: bool = True,
                 trade_days: list[date] | None = None):
        self.conn, self.ledger = conn, ledger
        self.sid, self.version, self.spec = sid, version, spec
        self.notice_judge, self.news_judge = notice_judge, news_judge
        self.universe, self.features, self.concept_map = universe, features, concept_map
        self.env_provider = env_provider
        self.prev_day = prev_day
        self.can_buy = can_buy
        self.calendar = sorted(trade_days) if trade_days else None     # 交易日历；None = 日历不可用
        self.accounts = (f"{sid}_main", f"{sid}_shadow")

        # 初始化账户
        from jevq.data.custom_store import capital_of
        capital = capital_of(conn, sid) or spec.get("capital", 30000)
        for account in self.accounts:
            ledger.ensure_account(account, capital)

        # 当日否决集和错误计数
        self.rejected: set[str] = set()
        self._rejected_day: date | None = None
        # 每分钟的 Jev 检查数 / 错误数（与低吸 MAX_CHECKS_PER_TICK / MAX_ERRORS_PER_TICK 同口径，每轮清零）
        self.check_count = 0
        self.error_count = 0

        # 预计算缓存（每分钟更新一次）
        self.amount_ranks: dict[str, int] = {}
        self.concept_heats: dict[str, int] = {}
        self.leader_ranks: dict[str, int] = {}
        self._cache_minute: str | None = None

    def on_minute(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        """每分钟决策：先卖出，再买入（can_buy 且有空位时）。"""
        if self._rejected_day != now.date():
            self._restore_rejected(now.date())

        if not quotes:
            return []

        # 更新预计算缓存
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        if self._cache_minute != minute_key:
            self._update_cache(quotes)
            self._cache_minute = minute_key

        fills = self._sells(quotes, now)

        self.check_count = 0
        self.error_count = 0

        if self.can_buy:
            fills += self._buys(quotes, now)

        return fills

    def _restore_rejected(self, day: date):
        """重启后恢复当日已否决/出错的票。"""
        rows = self.conn.execute(
            "SELECT DISTINCT code FROM decisions WHERE strategy=? AND verdict IN ('reject', 'error') "
            "AND code != '' AND substr(ts, 1, 10) = ?", (self.sid, day.isoformat())).fetchall()
        self.rejected = {r[0] for r in rows}
        self._rejected_day = day

    def _update_cache(self, quotes: dict[str, Quote]):
        """每分钟预计算 amount_rank 和 concept_heat。"""
        self.amount_ranks, self.concept_heats, self.leader_ranks = minute_context(
            quotes, self.universe, self.concept_map, self.features)

    def _sells(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        """卖出：对两个账户持仓，用绑定该股的取值函数 first_exit。"""
        fills = []
        trade_days = self._trade_days(now.date())

        for account in self.accounts:
            for pos in self.ledger.positions(account):
                try:
                    q = quotes.get(pos.code)
                    if q is None or q.ts.date() < now.date():
                        continue

                    # T+1：买入当天不卖
                    if pos.buy_date >= now.date():
                        continue

                    get = self._make_getter(pos.code, q, pos, now.time(), trade_days)
                    reason = first_exit(self.spec.get("exits") or [], get)

                    if reason and (f := self.ledger.sell(account, pos, q, now, reason)):
                        fills.append(f)

                except Exception as e:
                    self.conn.rollback()
                    print(f"[{self.sid}] {now:%H:%M} 卖出 {account}/{pos.code} 出错: {e!r}")

        return fills

    def _buys(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        """买入：候选 = 股票池内当日行情中 eval_group(select) 为真者 → rank_codes → 逐只检查 entry。"""
        main, shadow = self.accounts
        main_slots = self.ledger.free_slots(main)
        shadow_slots = self.ledger.free_slots(shadow)

        if main_slots <= 0 and shadow_slots <= 0:
            return []

        # 选股
        candidates = []
        trade_days = self._trade_days(now.date())
        for code, q in quotes.items():
            if code not in self.universe or q.ts.date() < now.date():
                continue
            try:
                get = self._make_getter(code, q, None, now.time(), trade_days)
                if eval_group(self.spec.get("select"), get, empty=False):
                    candidates.append(code)
            except Exception as e:
                print(f"[{self.sid}] {now:%H:%M} 选股 {code} 出错: {e!r}")

        if not candidates:
            return []

        # 排序
        def rank_get(code, key, n):
            q = quotes.get(code)
            if q is None:
                return None
            return self._make_getter(code, q, None, now.time(), trade_days)(key, n)

        ranked = rank_codes(candidates, rank_get, self.spec.get("rank"))

        fills = []

        # 影子账户：同候选同 entry、不走 Jev、买排名第一个满足 entry 的
        if shadow_slots > 0:
            for code in ranked:
                if self.ledger.holds(shadow, code):
                    continue
                try:
                    q = quotes.get(code)
                    if q is None:
                        continue
                    get = self._make_getter(code, q, None, now.time(), trade_days)
                    if eval_group(self.spec.get("entry"), get, empty=True):
                        if f := self.ledger.buy(shadow, q, now, f"影子｜{self.spec['name']}"):
                            fills.append(f)
                            break  # 影子只买一只
                except Exception as e:
                    self.conn.rollback()
                    print(f"[{self.sid}] {now:%H:%M} 影子买入 {code} 出错: {e!r}")

        # 主账户：逐只检查 entry → Jev → 买入
        if main_slots > 0:
            for code in ranked:
                if main_slots <= 0:
                    break

                if self.ledger.holds(main, code):
                    continue

                # 每分钟最多检查 MAX_CHECKS_PER_TICK 只；错误 MAX_ERRORS_PER_TICK 次熔断（都只管这一分钟）
                if self.check_count >= MAX_CHECKS_PER_TICK or self.error_count >= MAX_ERRORS_PER_TICK:
                    break

                try:
                    q = quotes.get(code)
                    if q is None:
                        continue

                    get = self._make_getter(code, q, None, now.time(), trade_days)

                    # 检查 entry
                    if not eval_group(self.spec.get("entry"), get, empty=True):
                        continue

                    # 可买性预检：停牌 / 涨停封死 / 钱不够一手 → 反正买不进，别浪费 Jev 调用和检查额度
                    if (q.suspended or q.price >= q.limit_up
                            or affordable_shares(min(self.ledger.cash(main), MAX_PER_TRADE), q.price) == 0):
                        continue

                    # 收集命中的条件
                    hits = self._collect_hits(code, q, None, now.time(), trade_days)

                    # Jev 判断
                    verdict, outcome, notice_trace, news_trace = self._judge_jev(code, q, now, hits)

                    if verdict == "pass":
                        if f := self.ledger.buy(main, q, now, self.spec["name"]):
                            fills.append(f)
                            outcome["bought"] = True
                            outcome["shares"] = f.shares
                            outcome["price"] = f.price
                            main_slots -= 1
                            self._log(now, code, "pass", self.spec["name"], hits, outcome,
                                    notice_trace, news_trace)

                except Exception as e:
                    self.conn.rollback()
                    self.error_count += 1
                    print(f"[{self.sid}] {now:%H:%M} 主账户买入 {code} 出错: {e!r}")
                    # 记录 error verdict
                    self._log(now, code, "error", f"异常：{e!r}", [], {"bought": False})
                    self.rejected.add(code)

        return fills

    def _judge_jev(self, code: str, q: Quote, now: datetime, hits: list) -> tuple[str, dict, any, any]:
        """Jev 判断：按 spec.jev 开关，复用 NoticeJudge/NewsJudge 与当日否决集。
        返回 (verdict, outcome, notice_trace, news_trace)。"""
        if code in self.rejected:
            return "reject", {"bought": False}, None, None

        self.check_count += 1

        jev_spec = self.spec.get("jev") or {}
        notice_trace, news_trace = None, None

        # 公告判断
        if jev_spec.get("notices", True):
            try:
                verdict, why = self.notice_judge.check(code, now.date())
                notice_trace = getattr(self.notice_judge, "last_trace", None)
                if verdict != "pass":
                    if verdict == "error":      # Jev 超时/报错（JevClient 内部已重试）也计入本分钟熔断
                        self.error_count += 1
                    self.rejected.add(code)
                    self._log(now, code, verdict, why, hits, {"bought": False}, notice_trace, news_trace)
                    return verdict, {"bought": False}, notice_trace, news_trace
            except Exception as e:
                self.error_count += 1
                self.rejected.add(code)
                self._log(now, code, "error", f"公告判断异常：{e!r}", hits, {"bought": False})
                return "error", {"bought": False}, None, None

        # 消息面判断
        if jev_spec.get("news", True):
            try:
                verdict, why = self.news_judge.check(code, q.name, now)
                news_trace = getattr(self.news_judge, "last_trace", None)
                if verdict != "pass":
                    if verdict == "error":      # Jev 超时/报错（JevClient 内部已重试）也计入本分钟熔断
                        self.error_count += 1
                    self.rejected.add(code)
                    self._log(now, code, verdict, why, hits, {"bought": False}, notice_trace, news_trace)
                    return verdict, {"bought": False}, notice_trace, news_trace
            except Exception as e:
                self.error_count += 1
                self.rejected.add(code)
                self._log(now, code, "error", f"消息面判断异常：{e!r}", hits, {"bought": False}, notice_trace)
                return "error", {"bought": False}, notice_trace, None

        return "pass", {"bought": False}, notice_trace, news_trace

    def _collect_hits(self, code: str, q: Quote, pos: Position | None, t: time,
                      trade_days: list[date]) -> list[dict]:
        """收集命中的选股与买点条件的中文描述+当时取值。"""
        hits = []
        get = self._make_getter(code, q, pos, t, trade_days)

        # 选股条件
        select_group = self.spec.get("select") or {}
        for cond in select_group.get("conditions") or []:
            if eval_cond(cond, get):
                hits.append(self._describe_cond(cond, get))

        # 买点条件
        entry_group = self.spec.get("entry") or {}
        for cond in entry_group.get("conditions") or []:
            if eval_cond(cond, get):
                hits.append(self._describe_cond(cond, get))

        return hits

    def _describe_cond(self, cond: dict, get) -> dict:
        """条件的中文描述。"""
        from jevq.rules.custom import FIELDS, OP_LABELS

        field_key = cond.get("field")
        field = FIELDS.get(field_key)
        if field is None:
            return {"field": field_key, "value": None, "desc": "未知字段"}

        n = (cond.get("param") or {}).get("n")
        value = get(field_key, n)
        op = cond.get("op")
        cond_value = cond.get("value")

        desc_parts = [field.label]
        if n is not None:
            desc_parts.append(f"(N={n})")
        desc_parts.append(OP_LABELS.get(op, op))

        if op == "between":
            desc_parts.append(f"{cond_value[0]}~{cond_value[1]}")
        elif op == "in":
            desc_parts.append("/".join(str(v) for v in cond_value))
        elif op == "is":
            desc_parts.append("是" if cond_value else "否")
        else:
            desc_parts.append(str(cond_value))

        if field.unit:
            desc_parts.append(field.unit)

        if isinstance(value, time):  # 「时间」字段取值是 datetime.time，要能写进 JSON trace
            value = value.strftime("%H:%M")
        return {"field": field.label, "value": value, "desc": "".join(desc_parts)}

    def _make_getter(self, code: str, q: Quote, pos: Position | None, t: time,
                     trade_days: list[date]):
        """取值函数：逻辑在模块级 make_getter，试跑预览（Api.rule_preview）复用同一份。"""
        return make_getter(code, q, features=self.features.get(code) or {}, amount_ranks=self.amount_ranks,
                           concept_heats=self.concept_heats, env=self.env_provider(), t=t,
                           pos=pos, trade_days=trade_days, leader_ranks=self.leader_ranks)

    def _trade_days(self, today: date) -> list[date]:
        """截止到今天（含）的交易日，供「持有天数」用。优先用交易日历；日历不可用时退回 closes 表
        里有收盘价的日子 + 今天（盘中今天的收盘价还没入库）。"""
        if self.calendar is not None:
            return [d for d in self.calendar if d <= today]
        rows = self.conn.execute("SELECT DISTINCT date FROM closes WHERE date < ?", (today.isoformat(),)).fetchall()
        return sorted({date.fromisoformat(r[0]) for r in rows} | {today})

    def _log(self, now: datetime, code: str, verdict: str, detail: str, hits: list,
             outcome: dict, notice_trace=None, news_trace=None):
        """记录决策。"""
        trace = {
            "strategy": self.sid,
            "version": self.version,
            "candidate": {
                "name": self.universe.get(code, ""),
                "hits": hits
            },
            "notices": notice_trace,
            "news": news_trace,
            "outcome": outcome
        }

        self.conn.execute(
            "INSERT INTO decisions(ts, code, verdict, detail, trace, strategy, version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), code, verdict, detail,
             json.dumps(trace, ensure_ascii=False), self.sid, self.version))
        self.conn.commit()
