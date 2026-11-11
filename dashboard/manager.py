"""交易台经理（dj 2026-11-09）：汇报员用代码算，经理调 DeepSeek 判断今天是什么行情、开哪位交易员。

汇报员（desk_reports）把现场整理成一行一个岗位的报告：宽度、梯队、高标、异动、扫板、持仓、风控。
经理在 09:26 / 10:30 / 13:20 各判断一次（比交易员先跑，同一轮交易员就能读到新指令），输出：
今天是打板日 / 趋势日 / 混沌日 / 空仓日，每位交易员开不开新仓、单笔上限，以及否证条件（什么情况说明这个判断错了）。
经理不选股、不下单：交易员照常选股，被关掉开仓的那位买单进「被丢弃」，卖出照常。经理没给指令（失败、还没跑）时一切照旧。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from jevq import config

from dashboard import flex_trader as ft

RUNTIME = ft.RUNTIME
DIRECTIVE_PATH = RUNTIME / "manager_directive.json"
LOG_PATH = RUNTIME / "manager_log.jsonl"
STATE_PATH = RUNTIME / "manager_state.json"
REPORTS_PATH = RUNTIME / "desk_reports.json"
SLOTS = ("09:26", "10:30", "13:20")
REGIMES = ("打板日", "趋势日", "混沌日", "空仓日")
KINDS = ("行情", "算术", "扫描", "账户")
MIN_PCT = 0.03


# ---------- 汇报员（纯代码） ----------
def _report(role, code, name, kind, line, level, now):
    return {"role": role, "code": code, "name": name, "kind": kind, "line": line, "level": level, "ts": f"{now:%H:%M}"}


def desk_reports(ctx: dict, accounts: dict, logs: dict, now: datetime) -> list[dict]:
    """现场 → 每个岗位一行报告。level：ok 正常 / warn 留意 / bad 不利 / idle 没数据。任何一块缺了只是那一行写「没数据」。"""
    ctx = ctx or {}
    market = ctx.get("market") or {}
    b = market.get("breadth") or {}
    s = market.get("sentiment") or {}
    out = []

    up, down = b.get("up"), b.get("down")
    if up is not None and down is not None and (up + down):
        share = up / (up + down)
        line = f"涨 {up} / 跌 {down}"
        if s.get("涨停家数") is not None:
            line += f" · 涨停 {s.get('涨停家数')} 跌停 {s.get('跌停家数', '—')}"
        if s.get("炸板率%") is not None:
            line += f" · 炸板 {s.get('炸板率%'):.0f}%"
        level = "ok" if share >= 0.6 else "bad" if share <= 0.35 else "warn"
    else:
        line, level = "宽度没数据", "idle"
    out.append(_report("breadth", "BREADTH", "宽度", "行情", line, level, now))

    ladder = ctx.get("themeLadder") or []
    if ladder:
        t = ladder[0]
        line = f"{t.get('theme')} {t.get('n')} 家 · 最高 {t.get('maxb')} 板 · {t.get('phase') or '—'}"
        if len(ladder) > 1:
            line += f" ｜次 {ladder[1].get('theme')} {ladder[1].get('n')} 家"
        level = "ok" if (t.get("n") or 0) >= 5 else "warn"
    else:
        line, level = "没有成形的题材梯队", "idle"
    out.append(_report("ladder", "LADDER", "梯队", "算术", line, level, now))

    hb = ctx.get("highBoards") or {}
    verdict = hb.get("verdict")
    if verdict in ("高标走弱", "高标强势"):
        top = (hb.get("items") or [{}])[0]
        line = f"昨日≥3板 {hb.get('count')} 只 · 走弱 {hb.get('weak')} · {verdict}"
        if top.get("name"):
            line += f" ｜{top['name']} {top.get('boards')}板 {top.get('pct', 0):+.1f}%"
        level = "bad" if verdict == "高标走弱" else "ok"
    else:
        line, level = verdict or "高标没数据", "idle"
    out.append(_report("high", "HIGH", "高标", "算术", line, level, now))

    quotes = ctx.get("quotes") or {}
    movers = sorted(((c, q) for c, q in quotes.items() if q.get("src") == "异动"),
                    key=lambda x: x[1].get("amount亿") or 0, reverse=True)
    line = f"异动 {len(movers)} 只" + (" · " + "、".join(f"{q.get('name')} {q.get('pct', 0):+.1f}%" for _, q in movers[:3]) if movers else "")
    out.append(_report("movers", "MOVERS", "异动", "扫描", line, "ok" if len(movers) >= 5 else "warn" if movers else "idle", now))

    sealed = sum(1 for q in quotes.values() if q.get("sealed"))
    scan = sum(1 for q in quotes.values() if q.get("src") == "扫板")
    line = f"候选 {len(quotes)} 只 · 封板 {sealed} · 扫板池 {scan}" if quotes else "候选没数据"
    out.append(_report("scan", "SCAN", "扫板", "扫描", line, "ok" if quotes else "idle", now))

    parts, blocked = [], []
    for sid, acct in (accounts or {}).items():
        label = ft.TRADERS[sid].label if sid in ft.TRADERS else sid
        parts.append(f"{label} {acct.get('equity', 0):,.0f} / {len(acct.get('positions') or [])} 只")
        if "【" in str(acct.get("guards", "")):
            blocked.append(f"{label}{str(acct['guards']).split('【', 1)[1].rstrip('】')}")
    out.append(_report("book", "BOOK", "持仓", "账户", " · ".join(parts) or "账户没数据", "ok" if parts else "idle", now))
    out.append(_report("risk", "RISK", "风控", "账户", "；".join(blocked) if blocked else ("护栏正常" if parts else "账户没数据"),
                       "bad" if blocked else "ok" if parts else "idle", now))

    for sid, rows in (logs or {}).items():
        if sid not in ft.TRADERS:
            continue
        last = rows[-1] if rows else None
        if last:
            a = last.get("analysis") or {}
            line = f"{last.get('slot')} {a.get('stage') or '—'} · 下单 {len(last.get('orders') or [])}"
            if last.get("dropped"):
                line += f" · 丢弃 {len(last['dropped'])}"
            if last.get("skipped"):
                line = f"{last.get('slot')} {last['skipped']}"
        else:
            line = "今天还没跑"
        out.append(_report(f"trader:{sid}", sid.upper(), ft.TRADERS[sid].label, "账户", line, "ok" if last else "idle", now))
    return out


# ---------- 给经理的现场 ----------
def manager_context(now: datetime, slot: str, ctx: dict, reports: list, accounts: dict, logs: dict, earlier: list | None = None) -> dict:
    """经理看汇报、市场概况、题材梯队（不带成员）、每位交易员的账户和今天的想法、今天之前的指令；不看整张行情表。"""
    ctx = ctx or {}
    traders = {}
    for sid, t in ft.TRADERS.items():
        acct = (accounts or {}).get(sid) or {}
        traders[sid] = {"label": t.label, "style": t.style,
                        "account": {"equity": acct.get("equity"), "cash": acct.get("cash"), "guards": acct.get("guards"),
                                    "positions": [{k: p.get(k) for k in ("code", "pnlPct", "canSellToday")} for p in acct.get("positions") or []]},
                        "today": [{"slot": r.get("slot"), "stage": (r.get("analysis") or {}).get("stage"),
                                   "plan": (r.get("analysis") or {}).get("plan"), "orders": [f"{o.get('action')}{o.get('code')}" for o in r.get("orders") or []],
                                   "skipped": r.get("skipped")} for r in (logs or {}).get(sid, [])][-4:]}
    return {"time": f"{now:%H:%M}", "slot": slot, "snapshotId": f"MGR-{now:%Y%m%d}-{slot.replace(':', '')}",
            "reports": [{"name": r["name"], "line": r["line"], "level": r["level"]} for r in reports],
            "market": ctx.get("market") or {}, "highBoards": ctx.get("highBoards") or {},
            "themeLadder": [{k: v for k, v in t.items() if k != "members"} for t in (ctx.get("themeLadder") or [])[:6]],
            "traders": traders, "earlier": (earlier or [])[-3:]}


SYSTEM_PROMPT = (
    "你是 A 股短线交易台的经理，手下有几位 DeepSeek 交易员（各管一个模拟账户，不下真单）和一组用代码算数的汇报员。"
    "你不选股、不下单：你的工作是判断今天是什么行情，决定哪位交易员今天开新仓、单笔最多多大。交易员照常自己选股，"
    "你关掉开仓的那位只管持仓（卖出照常）。\n\n"
    "【行情分类】\n"
    "打板日：赚钱效应集中在涨停板，连板梯队完整、高标强势、昨日涨停股今天红盘率高、炸板率低——打板/接力型交易员该上。\n"
    "趋势日：涨停接力吃面（高标走弱、炸板多），但普涨或板块放量走强、异动票多——趋势/强势跟随型交易员该上，打板型收手。\n"
    "混沌日：方向不清，题材轮动快、宽度一般——都可以开，但单笔上限放小（10%~15%），试错为主。\n"
    "空仓日：跌停扩散、宽度极差（上涨占比 <30%）、炸板率很高——都关新仓。全关只用在真的很差的日子：空仓有代价，"
    "dj 说过分析了半天一只不买是太严了。\n\n"
    "【怎么判断】先看汇报员（reports，level 是 ok/warn/bad/idle）和 highBoards、themeLadder；再看每位交易员今天前几轮的想法和账户；"
    "earlier 是你今天之前的指令和否证条件——先检查之前的否证条件有没有被触发，触发了就说明之前判断错了，要改。\n"
    "每个判断都是一个假设：必须写 falsify（否证条件）——到下一次判断前，出现什么具体、可观察的情况就说明你判断错了"
    "（例如「10:30 前先进封装涨停数掉到 3 家以下」「上涨家数跌破 2000」），不写模糊的话。\n\n"
    "输出：snapshotId 原样返回；regime（打板日/趋势日/混沌日/空仓日）；read（行情判断两三句，引用汇报里的数字）；"
    "traders：每位交易员 {buy: 开不开新仓, maxPct: 单笔上限（0.03~0.30，占账户净值）, why: 一句话}；falsify；watch（接下来盯什么）。"
)


def build_prompt(mctx: dict) -> tuple[str, str]:
    prompt = ("模拟账户，不下真单。快照编号必须原样返回。不调用任何工具。\n现场：\n" + json.dumps(mctx, ensure_ascii=False)
              + "\n\n请按系统提示的输出要求返回一个 JSON 对象：snapshotId、regime、read、traders、falsify、watch。")
    return SYSTEM_PROMPT, prompt


def answer_schema(snapshot_id: str, sids: list[str]) -> dict:
    one = {"type": "object", "additionalProperties": False, "required": ["buy", "maxPct", "why"],
           "properties": {"buy": {"type": "boolean"}, "maxPct": {"type": "number", "minimum": MIN_PCT, "maximum": config.FLEX_MAX_PCT},
                          "why": {"type": "string", "maxLength": 100}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["snapshotId", "regime", "read", "traders", "falsify", "watch"],
            "properties": {"snapshotId": {"const": snapshot_id}, "regime": {"enum": list(REGIMES)},
                           "read": {"type": "string", "maxLength": 300},
                           "traders": {"type": "object", "additionalProperties": False, "required": list(sids),
                                       "properties": {sid: one for sid in sids}},
                           "falsify": {"type": "string", "maxLength": 120}, "watch": {"type": "string", "maxLength": 200}}}


def validate_directive(answer, snapshot_id: str, sids: list[str]) -> dict:
    """编号必须对；漏掉的交易员当「照常开」；单笔上限夹在 [3%, 护栏上限]。"""
    if not isinstance(answer, dict):
        raise ValueError("经理的回答不是 JSON 对象")
    if answer.get("snapshotId") != snapshot_id:
        raise ValueError("经理的回答与快照编号不一致")
    raw = answer.get("traders") if isinstance(answer.get("traders"), dict) else {}
    traders = {}
    for sid in sids:
        r = raw.get(sid) if isinstance(raw.get(sid), dict) else None
        if r is None:
            traders[sid] = {"buy": True, "maxPct": config.FLEX_MAX_PCT, "why": "经理没提到，照常"}
            continue
        pct = ft._number(r.get("maxPct"))
        traders[sid] = {"buy": r.get("buy") is not False,
                        "maxPct": round(min(max(pct if pct is not None else config.FLEX_MAX_PCT, MIN_PCT), config.FLEX_MAX_PCT), 3),
                        "why": ft._text(r.get("why"), 100)}
    regime = answer.get("regime") if answer.get("regime") in REGIMES else "混沌日"
    return {"snapshotId": snapshot_id, "regime": regime, "read": ft._text(answer.get("read"), 300), "traders": traders,
            "falsify": ft._text(answer.get("falsify"), 120), "watch": ft._text(answer.get("watch"), 200)}


# ---------- 调度与落盘 ----------
def due_slot(now: datetime, state_path: Path | None = None) -> str | None:
    if now.weekday() >= 5:
        return None
    state = ft._read_state(state_path or STATE_PATH)
    done = set(state.get("done", [])) if state.get("date") == now.date().isoformat() else set()
    due = None
    for slot in SLOTS:
        h, m = map(int, slot.split(":"))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if start <= now <= start + ft.SLOT_WINDOW and slot not in done:
            due = slot
    return due


def _write_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _today_rows(path: Path, day: str, limit: int = 40) -> list[dict]:
    rows = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines()[-limit:]:
            row = json.loads(line)
            if str(row.get("ts", ""))[:10] == day:
                rows.append(row)
    except (OSError, ValueError):
        pass
    return rows


def run_manager(now: datetime, slot: str, *, gather, ask, notifier=None, directive_path: Path | None = None,
                log_path: Path | None = None, state_path: Path | None = None, reports_path: Path | None = None) -> dict:
    """gather(now, slot) -> {ctx, accounts, logs}；ask(system, prompt, schema) -> 回答。失败直接抛出：不写指令、不记已跑。"""
    directive_path, log_path = Path(directive_path or DIRECTIVE_PATH), Path(log_path or LOG_PATH)
    state_path, reports_path = Path(state_path or STATE_PATH), Path(reports_path or REPORTS_PATH)
    scene = gather(now, slot)
    reports = desk_reports(scene.get("ctx"), scene.get("accounts"), scene.get("logs"), now)
    _write_json(reports_path, {"ts": now.isoformat(timespec="seconds"), "reports": reports})
    earlier = [{"slot": r.get("slot"), "regime": r.get("regime"), "falsify": r.get("falsify"),
                "traders": {k: v.get("buy") for k, v in (r.get("traders") or {}).items()}}
               for r in _today_rows(log_path, now.date().isoformat())]
    mctx = manager_context(now, slot, scene.get("ctx"), reports, scene.get("accounts"), scene.get("logs"), earlier)
    system, prompt = build_prompt(mctx)
    sids = list(ft.TRADERS)
    d = validate_directive(ask(system, prompt, answer_schema(mctx["snapshotId"], sids)), mctx["snapshotId"], sids)
    record = {"date": now.date().isoformat(), "slot": slot, "ts": now.isoformat(timespec="seconds"), **d}
    _write_json(directive_path, record)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    ft.mark_done(slot, now, state_path)
    if notifier is not None:
        lines = [f"{ft.TRADERS[sid].label}：{'开' if t['buy'] else '关'}新仓"
                 + (f"（单笔≤{t['maxPct']:.0%}）" if t["buy"] else "") + f"｜{t['why']}" for sid, t in d["traders"].items()]
        body = "\n\n".join([f"{now:%H:%M}｜{d['regime']}", "【判断】" + d["read"], "【交易员】\n" + "\n".join(lines),
                            "【否证条件】" + d["falsify"], "【接下来盯】" + d["watch"],
                            "经理只决定开哪位交易员、单笔多大；选股下单仍由交易员，护栏照旧。模拟账户。"])
        try:
            notifier.send(f"👔 交易台经理｜{slot}｜{d['regime']}", body)
            notifier.flush(timeout=55)
        except Exception:                                          # noqa: BLE001
            pass
    return {"slot": slot, "regime": d["regime"], "buy": [sid for sid, t in d["traders"].items() if t["buy"]]}


def gather_scene(now: datetime, slot: str) -> dict:
    """真实现场：一号的 context（市场、梯队、高标、候选行情）+ 每位交易员的账户 + 今天的日志。"""
    ctx = ft.gather_context(now, slot, skip_src=())                 # 经理要看全市场（含异动票），交易员之间的分工不管经理
    quotes = {c: SimpleNamespace(price=q["price"]) for c, q in (ctx.get("quotes") or {}).items()}
    accounts = {sid: ft._account_state(ft.DB_PATH, quotes, now.date(), sid) for sid in ft.TRADERS}
    logs = {sid: _today_rows(t.log_path, now.date().isoformat()) for sid, t in ft.TRADERS.items()}
    return {"ctx": ctx, "accounts": accounts, "logs": logs}


# ---------- 交易员这一侧 ----------
def directive_for(sid: str, now: datetime, path: Path | None = None) -> dict | None:
    """今天经理对这位交易员的指令；不是今天的、没提到这位的都返回 None（照旧）。"""
    d = ft._read_state(path or DIRECTIVE_PATH)
    if d.get("date") != now.date().isoformat():
        return None
    t = (d.get("traders") or {}).get(sid)
    if not isinstance(t, dict):
        return None
    return {"buy": t.get("buy") is not False, "maxPct": t.get("maxPct"), "why": t.get("why", ""),
            "regime": d.get("regime"), "slot": d.get("slot"), "falsify": d.get("falsify")}


def apply_directive(result: dict, directive: dict | None) -> dict:
    """关了开仓：买单移进 dropped；开着：买单仓位夹到单笔上限。卖单不动。"""
    if not directive:
        return result
    kept = []
    for o in result.get("orders", []):
        if o.get("action") == "买入" and not directive.get("buy", True):
            result.setdefault("dropped", []).append({"action": "买入", "code": o.get("code"),
                                                     "why": f"经理今天关了开仓（{directive.get('regime')}）"})
            continue
        cap = directive.get("maxPct")
        if o.get("action") == "买入" and isinstance(o.get("pct"), (int, float)) and isinstance(cap, (int, float)):
            o["pct"] = min(o["pct"], cap)
        kept.append(o)
    result["orders"] = kept
    return result
