"""经理的一万元账户（dj 2026-11-11）：实盘只有一万块、只能单票全仓，模拟也照这个来，让经理像自己炒股一样操作。

经理不自己找票：候选只来自交易员当天给的数据——两位 AI 交易员最近一轮下的单、推荐的票、被关掉开仓时想买的票、他们的持仓，
加上策略交易员推上来的票。经理从里面挑一只全仓，或者卖掉手里的那只，或者不动。
每个时点排在交易员和策略席后面跑（09:26 / 10:30 / 13:20 / 14:35），订单写进 manager_orders.json，
由 daemon 里和 AI 交易员同一个执行器按实时行情成交、每分钟盯止损（jevq/strategies/flex.py，sid="mgr"：单票可全仓、只拿一只）。
这个账户的盈亏就是对经理的考核。只写模拟订单文件，不碰实盘。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from jevq import config
from jevq.ledger import affordable_shares

from dashboard import flex_trader as ft
from dashboard import manager as mg

SID = "mgr"
RUNTIME = ft.RUNTIME
ORDERS_PATH = RUNTIME / "manager_orders.json"
LOG_PATH = RUNTIME / "manager_trade_log.jsonl"
STATE_PATH = RUNTIME / "manager_trade_state.json"
SLOTS = ("09:26", "10:30", "13:20", "14:35")
MAIN_BOARD = ("60", "00")

SYSTEM_PROMPT = (
    "你是 A 股短线交易台的经理。除了给交易员定方向，你自己还有一个模拟账户：本金一万元（和老板 dj 的实盘资金一样大），"
    "同一时间只拿一只股票，买就是全仓。这个账户的盈亏就是对你的考核，老板会照着它决定实盘怎么做。\n\n"
    "【你能买什么】只能从 pool 里挑：那是你的交易员今天给的票（from 写了是谁、为什么给的），你不自己找票。"
    "pool 里每只票有现价 price、涨幅 pct、是否已封板 sealed。\n\n"
    "【现实约束——程序强制】\n"
    "1. 一手 100 股：现价 × 100 超过账户资金的买不起；仓位由程序按全仓算，你不用写。\n"
    "2. T+1：今天买的明天才能卖，当天跌了也止不了损，所以下手前想清楚最坏情况。\n"
    "3. 只有一个仓位：手里有票就要先卖（而且得是昨天或更早买的）才能换；一轮最多一卖一买。\n"
    "4. 已封板的买不进；只做沪深主板。\n"
    "5. 每笔买入必须写止损价 stop（低于现价、不超过 12% 远）和 invalid（什么情况说明这笔判断错了）；"
    "止损由程序每分钟盯着执行。ref_price 写你看到的现价，成交价偏离超过 2% 不追。\n"
    f"6. 越涨越卖（程序自动执行）：浮盈到过 {config.FLEX_PROTECT_AT:.0%} 以后止损自动上移到成本价以上，并跟着最高价走，从最高价回撤 {config.FLEX_TRAIL:.0%} 就全卖。"
    "你这个账户通常只有一手，拆不开，所以没有分批，只有这条移动止盈线。持仓里的 stop 就是现在真正会触发卖出的那条线。\n"
    f"7. 高位不买：每只票带 rise5 / rise20（昨收比 5 / 20 个交易日前涨了多少 %）和 fromLow60（比近 60 日最低高多少 %）。"
    f"rise20 ≥ {config.FLEX_MAX_RISE20:.0f}% 的程序直接拒单；30% 以上就算高位，全仓一只票更不能买在山顶。"
    "「回测」字段是回测员写的历史同档表现（5 日中位、胜率、5 天内跌超 10% 的比例），下单前先看——"
    "2026-11-12 冰山冷热一个月翻倍，你和交易员都当成弱转强买了。\n\n"
    "【目标】score 是这个账户的记分：今日 / 本周 / 本月的实际收益率 actual、目标 target、还差多少元 gapYuan。"
    "目标要靠选对那一只来完成，不是靠每轮都出手：全仓一只票，错一次就是整个账户的亏损。"
    "没有够好的票就空着，手里的票预期还在就拿着；落后时不许为了追目标去买自己都不信的票。\n\n"
    "【风险档位】stance 是你这一周给自己定的档位（低 / 中 / 中高 / 高；老板的风格是中高）。中高和高：有够好的票就敢全仓上；中：只做几位交易员同时给、理由互相印证的票；低：没有十足把握就空着。\n\n"
    "【怎么想】directive 是你自己今天给交易台定的行情和判断，买卖要和它一致；earlier 是你今天前几轮对这个账户的想法。"
    "同一只票被几位交易员同时给出、理由互相印证的，比只有一个人提的可靠。\n\n"
    "输出：snapshotId 原样返回；think（两三句：比较了哪几只、为什么选这只或者为什么不动）；"
    "orders（可以为空；每张 {action: 买入/卖出, code, reason}，买入还要 stop、invalid、ref_price）；plan（接下来怎么处理这个仓位）。"
)


# ---------- 候选池：只装交易员今天给的票 ----------
def build_pool(rows: dict, submissions: dict | None, accounts: dict) -> dict[str, list[str]]:
    """代码 → 是谁、为什么给的。只看每位 AI 交易员今天最近的一轮（更早的想法已经过时），只留沪深主板。"""
    pool: dict[str, list[str]] = {}

    def add(code, text):
        code = str(code or "")
        if code.startswith(MAIN_BOARD) and len(code) == 6:
            pool.setdefault(code, []).append(text[:90])
    for sid, t in ft.TRADERS.items():
        last = ((rows or {}).get(sid) or [None])[-1] or {}
        for o in last.get("orders") or []:
            if o.get("action") == "买入":
                add(o.get("code"), f"{t.label} 下单｜{o.get('pattern') or ''}｜{o.get('reason') or ''}")
        for p in last.get("picks") or []:
            if p.get("verdict") == "推荐":
                add(p.get("code"), f"{t.label} 推荐｜{p.get('why') or ''}")
        for d in last.get("dropped") or []:
            if d.get("action") == "买入" and "经理" in str(d.get("why")):
                add(d.get("code"), f"{t.label} 想买（当时你关了他的开仓）")
        for p in ((accounts or {}).get(sid) or {}).get("positions") or []:
            add(p.get("code"), f"{t.label} 持仓 {float(p.get('pnlPct') or 0):+.1f}%")
    for s in (submissions or {}).get("traders") or []:
        for p in s.get("picks") or []:
            add(p.get("code"), f"{s.get('label')} 推荐｜{p.get('why') or ''}")
    for i in ((submissions or {}).get("final") or {}).get("items") or []:
        if i.get("do"):
            add(i.get("code"), f"经理点了要做｜{i.get('why') or ''}")
    return pool


# ---------- 调度 ----------
def due_slot(now: datetime, state_path: Path | None = None) -> str | None:
    if now.weekday() >= 5:
        return None
    state = ft._read_state(Path(state_path or STATE_PATH))
    done = set(state.get("done", [])) if state.get("date") == now.date().isoformat() else set()
    due = None
    for slot in SLOTS:
        h, m = map(int, slot.split(":"))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if start <= now <= start + ft.SLOT_WINDOW and slot not in done:
            due = slot
    return due


# ---------- 提示词与答案 ----------
def answer_schema(snapshot_id: str) -> dict:
    order = {"type": "object", "additionalProperties": False, "required": ["action", "code", "reason"],
             "properties": {"action": {"enum": ["买入", "卖出"]}, "code": {"type": "string"}, "stop": {"type": "number"},
                            "ref_price": {"type": "number"}, "invalid": {"type": "string", "maxLength": 80},
                            "reason": {"type": "string", "maxLength": 120}}}
    return {"type": "object", "additionalProperties": False, "required": ["snapshotId", "think", "orders", "plan"],
            "properties": {"snapshotId": {"const": snapshot_id}, "think": {"type": "string", "maxLength": 300},
                           "orders": {"type": "array", "maxItems": 2, "items": order}, "plan": {"type": "string", "maxLength": 200}}}


def build_prompt(ctx: dict) -> tuple[str, str]:
    prompt = ("模拟账户，不下真单。快照编号必须原样返回。不调用任何工具。\n现场：\n" + json.dumps(ctx, ensure_ascii=False)
              + "\n\n请按系统提示的输出要求返回一个 JSON 对象：snapshotId、think、orders、plan。")
    return SYSTEM_PROMPT, prompt


def validate(answer, ctx: dict) -> dict:
    """只留做得到的订单：卖出必须是能卖的持仓；买入必须在交易员给的票里、没封板、一万块买得起一手、而且仓位是空的（或这一轮先卖掉）。
    买入一律按全仓（pct=1.0）交给执行器；止损、偏离参考价这些护栏由执行器判。"""
    if not isinstance(answer, dict):
        raise ValueError("经理账户的回答不是 JSON 对象")
    if answer.get("snapshotId") != ctx["snapshotId"]:
        raise ValueError("经理账户的回答与快照编号不一致")
    pool, account = ctx.get("pool") or {}, ctx.get("account") or {}
    held = {p["code"]: p for p in account.get("positions") or []}
    orders, dropped, selling, bought = [], [], set(), False

    def keep(action, code, raw, **extra):
        orders.append({"id": f"{ctx['snapshotId']}-{len(orders) + 1}", "action": action, "code": code,
                       "reason": ft._text(raw.get("reason"), 120), **extra})
    raw_orders = [r for r in (answer.get("orders") or []) if isinstance(r, dict)]
    for raw in sorted(raw_orders, key=lambda r: r.get("action") != "卖出"):      # 先卖后买：只有一个仓位
        action, code = ft._action_of(raw), str(raw.get("code", ""))
        drop = lambda why: dropped.append({"action": str(action), "code": code, "why": why})   # noqa: E731
        if action == "卖出":
            if code not in held:
                drop("没有这只持仓")
            elif held[code].get("canSellToday") is False:
                drop("T+1：今天买的明天才能卖")
            else:
                selling.add(code)
                keep("卖出", code, raw)
        elif action == "买入":
            q = pool.get(code)
            staying = [c for c in held if c not in selling]
            if q is None:
                drop("不在交易员给的票里")
            elif not q.get("price"):
                drop("没有实时行情")
            elif q.get("sealed"):
                drop("已封板，买不进")
            elif (q.get("rise20") or 0) >= config.FLEX_MAX_RISE20:
                drop(f"高位不买：近 20 个交易日已涨 {q['rise20']:.0f}%")
            elif staying:
                drop(f"手里还有 {staying[0]}，先卖掉才能换")
            elif affordable_shares(float(account.get("equity") or 0), float(q["price"])) == 0:
                drop(f"一万块买不起一手（现价 {q['price']}）")
            elif bought:
                drop("只有一个仓位，一轮只买一只")
            else:
                bought = True
                who = str((q.get("from") or ["交易员"])[0]).split("｜")[0]
                keep("买入", code, raw, pct=1.0, stop=ft._number(raw.get("stop")), ref_price=ft._number(raw.get("ref_price")),
                     invalid=ft._text(raw.get("invalid"), 80), mode="模式内", pattern=("经理·" + who)[:20])
        else:
            drop("动作不是买入/卖出")
    return {"snapshotId": ctx["snapshotId"], "think": ft._text(answer.get("think"), 300), "plan": ft._text(answer.get("plan"), 200),
            "orders": orders, "dropped": dropped}


def _write_orders(path: Path, ts: datetime, orders: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": ts.isoformat(timespec="seconds"), "orders": orders}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def run(now: datetime, slot: str, *, gather, ask, notifier=None, orders_path: Path | None = None, log_path: Path | None = None,
        state_path: Path | None = None, clock=None) -> dict:
    """gather(now, slot) -> {pool, account, directive, score}；ask(system, prompt, schema) -> 回答。失败直接抛出：不写订单、不记已跑。"""
    orders_path, log_path, state_path = Path(orders_path or ORDERS_PATH), Path(log_path or LOG_PATH), Path(state_path or STATE_PATH)
    scene = gather(now, slot)
    pool, account = scene.get("pool") or {}, scene.get("account") or {}
    record = {"date": now.date().isoformat(), "ts": now.isoformat(timespec="seconds"), "slot": slot}

    def finish(result: dict) -> None:
        _write_orders(orders_path, clock() if clock else now, result.get("orders") or [])   # 有效期从写入这一刻算
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({**record, **result, "pool": {c: v.get("from") for c, v in pool.items()}}, ensure_ascii=False) + "\n")
        ft.mark_done(slot, now, state_path)
    if not pool and not account.get("positions"):
        why = "交易员今天还没给票，账户空仓"
        finish({"orders": [], "dropped": [], "skipped": why})
        return {"slot": slot, "orders": 0, "skipped": why}
    earlier = [{"slot": r.get("slot"), "think": r.get("think"), "plan": r.get("plan"),
                "orders": [f"{o.get('action')}{o.get('code')}" for o in r.get("orders") or []]}
               for r in mg._today_rows(log_path, now.date().isoformat()) if not r.get("skipped")][-3:]
    ctx = {"time": f"{now:%H:%M}", "slot": slot, "snapshotId": f"MGRT-{now:%Y%m%d}-{slot.replace(':', '')}", "score": scene.get("score"),
           "account": account, "pool": pool, "directive": scene.get("directive"), "stance": scene.get("stance"), "earlier": earlier}
    system, prompt = build_prompt(ctx)
    result = validate(ask(system, prompt, answer_schema(ctx["snapshotId"])), ctx)
    finish(result)
    if notifier is not None and result["orders"]:
        name = lambda c: f"{(pool.get(c) or {}).get('name') or c}（{c}）"          # noqa: E731
        lines = []
        for o in result["orders"]:
            if o["action"] == "买入":
                q = pool.get(o["code"]) or {}
                lines.append(f"买入 {name(o['code'])} 全仓｜现价 {q.get('price')}｜止损 {o.get('stop')}｜{o['reason']}\n"
                             f"　谁给的：{'；'.join(q.get('from') or [])}\n　看错的信号：{o.get('invalid')}")
            else:
                lines.append(f"卖出 {name(o['code'])}｜{o['reason']}")
        if result["dropped"]:
            lines.append("⚠ 没做成：" + "；".join(f"{d['action']}{d['code']}（{d['why']}）" for d in result["dropped"]))
        title = "、".join(f"{o['action']}{(pool.get(o['code']) or {}).get('name') or o['code']}" for o in result["orders"])
        body = "\n\n".join([f"{now:%H:%M}｜净值 {float(account.get('equity') or 0):,.0f}", "【想法】" + result["think"], "【订单】\n" + "\n".join(lines),
                            "【计划】" + result["plan"], "一万元模拟账户，单票全仓，T+1；订单由 daemon 按实时行情执行，成交另推。不是真实委托。"])
        try:
            notifier.send(f"💼 经理账户｜{slot}｜{title}", body)
            notifier.flush(timeout=55)
        except Exception:                                          # noqa: BLE001
            pass
    return {"slot": slot, "orders": len(result["orders"])}


# ---------- 真实现场 ----------
def gather_scene(now: datetime, slot: str) -> dict:
    """交易员今天的日志和持仓、策略席的汇总 → 候选池；再取这些票和经理持仓的实时行情、经理账户现状、今天的定性、账户记分。"""
    from dashboard import strategy_desk
    from jevq.data import tencent
    day = now.date()
    rows = {sid: mg._today_rows(t.log_path, day.isoformat()) for sid, t in ft.TRADERS.items()}
    accounts = {sid: ft._account_state(ft.DB_PATH, {}, day, sid) for sid in ft.TRADERS}
    subs = ft._read_json(strategy_desk.SUBMIT_PATH)
    sources = build_pool(rows, subs if subs.get("date") == day.isoformat() else None, accounts)
    mine = ft._account_state(ft.DB_PATH, {}, day, SID)
    codes = sorted(set(sources) | {p["code"] for p in mine.get("positions") or []})
    try:
        quotes = tencent.fetch_quotes(codes) if codes else {}
    except Exception:                                            # noqa: BLE001  行情取不到：这一轮没有可买的票，持仓照旧
        quotes = {}
    positions = ft._positions(ft.DB_PATH, list(quotes), day)
    pool = {c: {"from": sources[c], "name": q.name, "price": q.price, "pct": q.pct, "amount亿": round(q.amount / 1e8, 2),
                "sealed": q.price >= q.limit_up}
            for c, q in quotes.items() if c in sources and q.price > 0 and q.ts.date() == day}
    from dashboard import backtester
    pool = backtester.annotate(pool, positions)
    account = ft._account_state(ft.DB_PATH, {c: SimpleNamespace(price=q.price) for c, q in quotes.items()}, day, SID)
    directive = ft._read_json(mg.DIRECTIVE_PATH)
    directive = {k: directive.get(k) for k in ("slot", "regime", "read", "watch")} if directive.get("date") == day.isoformat() else None
    score = None
    try:
        conn = sqlite3.connect(f"{Path(ft.DB_PATH).as_uri()}?mode=ro", uri=True, timeout=3)
        try:
            score = mg.company_score(conn, [(config.STRATEGIES[SID]["accounts"][0], account["equity"])], now, targets=config.MGR_TARGETS)
        finally:
            conn.close()
    except (sqlite3.Error, KeyError, TypeError, OSError):
        score = None
    from dashboard import weekly_review
    return {"pool": pool, "account": account, "directive": directive, "score": score, "stance": weekly_review.read_stance()}
