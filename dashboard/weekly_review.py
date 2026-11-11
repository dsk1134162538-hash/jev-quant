"""每周复盘会（dj 2026-11-11）：周五收盘后，经理和交易员开一次会。

流程：每位 AI 交易员先看自己这一周的账（了结了几笔、赚亏、最好最差的一笔）各说三句——做对了什么、做错了什么、下周怎么改；
经理听完，结合公司和自己那个一万元账户这一周的成绩、这一周每天的行情定性，给总评、给每位交易员一句要求、定下周的三条做法，
并给自己定下周的风险档位：低 / 中 / 中高 / 高。老板的风格偏中高风险，所以默认中高，再按市场情况上下调。
档位写进 manager_stance.json，下周经理每次判断（开关交易员、操作自己的账户）的现场里都带着它。
一位交易员没交复盘（超时、格式错）不影响开会；经理没开成就不写档位、不记已开，下一轮再试。只读账本、只写自己的文件，不下单。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path

from jevq import config

from dashboard import flex_trader as ft
from dashboard import manager as mg

RUNTIME = ft.RUNTIME
STANCE_PATH = RUNTIME / "manager_stance.json"
LOG_PATH = RUNTIME / "weekly_review.jsonl"
STATE_PATH = RUNTIME / "weekly_review_state.json"
LEVELS = ("低", "中", "中高", "高")
DEFAULT_LEVEL = "中高"
OPEN_AT, CLOSE_AT = time(15, 35), time(18, 0)        # 周五收盘后这段时间里开；过了就等下周（周末的行情数据不新鲜）

TRADER_ASK = {sid: (f"你是{t.label}（{t.style}）。现在是周五收盘后的复盘会，经理和另一位交易员都在。"
                    "下面是你这一周的账：了结了几笔、几笔赚、合计盈亏、最好和最差的一笔、这周账户涨跌、现在的持仓。"
                    "照实说，不找借口、不说空话，每条都要落到具体的票或具体的做法上。"
                    "输出：snapshotId 原样返回；good（这周做对了什么）；bad（做错了什么、亏在哪）；next（下周具体怎么改）。")
              for sid, t in ft.TRADERS.items()}

MANAGER_SYSTEM = (
    "你是 A 股短线交易台的经理，现在主持周五收盘后的复盘会。你手下的交易员刚各自说完（statements），"
    "现场里还有：公司这一周的成绩和目标（company）、每位交易员的账（traders）、你自己那个一万元账户的账（mgr）、"
    "这一周你每天给行情的定性（regimes）、收盘时的市场概况（market）。\n\n"
    "【要做的事】\n"
    "1. summary：这一周赚在哪、亏在哪，两三句，引用数字；你自己哪天的定性错了也要说。\n"
    "2. traders：给每位交易员一句下周的具体要求。\n"
    "3. rules：下周的做法，最多三条，写成能直接照着做的话。\n"
    "4. risk：给你自己定下周的风险档位 level（低 / 中 / 中高 / 高），why 说依据，market 说你对下周市场的看法。\n\n"
    "【风险档位怎么定】老板的风格偏中高风险，所以默认是「中高」：票少、仓位重、敢在行情配合时把额度给足。"
    "市场明显配合（普涨放量、主线清楚、接力不吃面）可以上调到「高」；"
    "市场明显转差（跌停扩散、炸板率高、连续亏损、回撤扩大）下调到「中」，只有很差才到「低」。"
    "不要因为这周赚了就无脑调高，也不要因为亏了一笔就缩回去：档位跟的是市场，不是心情。"
)


def week_start(day):
    return day - timedelta(days=day.weekday())


def week_id(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# ---------- 这一周的账 ----------
def week_stats(conn, account: str, now: datetime) -> dict:
    """本周了结的每一笔（按卖出日期算在哪一周）、胜率、合计盈亏、最好最差的一笔；本周买了几笔；账户本周涨跌。"""
    monday, today = week_start(now.date()).isoformat(), now.date().isoformat()
    opens: dict[str, float] = {}
    closed, buys = [], 0
    for code, side, price, shares, fee, ts in conn.execute(
            "SELECT code, side, price, shares, fee, ts FROM fills WHERE account=? ORDER BY id", (account,)):
        day = str(ts)[:10]
        if side == "buy":
            opens[code] = price * shares + (fee or 0)
            buys += monday <= day <= today
        elif code in opens:
            cost = opens.pop(code)
            if monday <= day <= today:
                pnl = price * shares - (fee or 0) - cost
                closed.append({"code": code, "yuan": round(pnl, 2), "pct": round(pnl / cost * 100, 2)})
    capital = config.ACCOUNT_CAPITAL.get(account, config.FLEX_CAPITAL)
    last = conn.execute("SELECT value FROM equity WHERE account=? AND date<=? ORDER BY date DESC LIMIT 1", (account, today)).fetchone()
    base = conn.execute("SELECT value FROM equity WHERE account=? AND date<? ORDER BY date DESC LIMIT 1", (account, monday)).fetchone()
    equity = last[0] if last else capital
    start = base[0] if base and base[0] else capital
    return {"closed": len(closed), "wins": sum(1 for c in closed if c["yuan"] > 0), "buys": buys,
            "pnlYuan": round(sum(c["yuan"] for c in closed), 2),
            "best": max(closed, key=lambda c: c["pct"]) if closed else None, "worst": min(closed, key=lambda c: c["pct"]) if closed else None,
            "equity": equity, "weekPct": round((equity / start - 1) * 100, 2)}


# ---------- 档位 ----------
def read_stance(path: Path | None = None) -> dict:
    """经理这一周的风险档位。还没开过周会就用老板的风格（中高）。"""
    data = ft._read_json(Path(path or STANCE_PATH))
    if data.get("level") in LEVELS:
        return {k: data.get(k) for k in ("week", "level", "why", "market", "rules", "ts")}
    return {"week": None, "level": DEFAULT_LEVEL, "why": "老板的风格偏中高风险；还没开过周会，先按这个", "market": None, "rules": [], "ts": None}


def due(now: datetime, state_path: Path | None = None) -> bool:
    if now.weekday() != 4 or not OPEN_AT <= now.time() <= CLOSE_AT:
        return False
    return ft._read_state(Path(state_path or STATE_PATH)).get("week") != week_id(now)


# ---------- 开会 ----------
def _trader_schema(snap: str) -> dict:
    s = {"type": "string", "maxLength": 160}
    return {"type": "object", "additionalProperties": False, "required": ["snapshotId", "good", "bad", "next"],
            "properties": {"snapshotId": {"const": snap}, "good": s, "bad": s, "next": s}}


def _manager_schema(snap: str) -> dict:
    s = {"type": "string", "maxLength": 200}
    return {"type": "object", "additionalProperties": False, "required": ["snapshotId", "summary", "risk", "rules", "traders"],
            "properties": {"snapshotId": {"const": snap}, "summary": {"type": "string", "maxLength": 400},
                           "risk": {"type": "object", "additionalProperties": False, "required": ["level", "why"],
                                    "properties": {"level": {"enum": list(LEVELS)}, "why": s, "market": s}},
                           "rules": {"type": "array", "maxItems": 3, "items": s},
                           "traders": {"type": "object", "additionalProperties": False, "properties": {sid: s for sid in ft.TRADERS}}}}


def _prompt(snap: str, body: dict, keys: str) -> str:
    return (f"快照编号：{snap}（snapshotId 必须原样返回这个编号）。模拟账户，不下真单，不调用任何工具。\n现场：\n"
            + json.dumps(body, ensure_ascii=False) + f"\n\n请返回一个 JSON 对象：{keys}。")


def run(now: datetime, *, gather, ask, notifier=None, stance_path: Path | None = None, log_path: Path | None = None,
        state_path: Path | None = None) -> dict:
    """gather(now) -> 这一周的现场；ask(system, prompt, schema) -> 回答。经理那一步失败直接抛出：不写档位、不记已开。"""
    stance_path, log_path, state_path = Path(stance_path or STANCE_PATH), Path(log_path or LOG_PATH), Path(state_path or STATE_PATH)
    scene = gather(now)
    week = scene.get("week") or week_id(now)
    statements = {}
    for sid, t in ft.TRADERS.items():
        snap = f"WK-{week}-{sid}"
        try:
            a = ask(TRADER_ASK[sid], _prompt(snap, {"week": week, "account": (scene.get("traders") or {}).get(sid)}, "snapshotId、good、bad、next"),
                    _trader_schema(snap))
            if not isinstance(a, dict) or a.get("snapshotId") != snap:
                raise ValueError("回答与快照编号不一致")
            statements[sid] = {"label": t.label, **{k: ft._text(a.get(k), 160) for k in ("good", "bad", "next")}}
        except Exception as e:                                         # noqa: BLE001  一位没交不影响开会
            statements[sid] = {"label": t.label, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    snap = f"WK-{week}-mgr"
    a = ask(MANAGER_SYSTEM, _prompt(snap, {**scene, "statements": statements, "lastStance": read_stance(stance_path)},
                                    "snapshotId、summary、traders、rules、risk"), _manager_schema(snap))
    if not isinstance(a, dict) or a.get("snapshotId") != snap:
        raise ValueError("经理的周复盘回答与快照编号不一致")
    risk = a.get("risk") if isinstance(a.get("risk"), dict) else {}
    level = risk.get("level") if risk.get("level") in LEVELS else DEFAULT_LEVEL      # 写了别的词：按老板的风格
    rules = [ft._text(r, 200) for r in (a.get("rules") or []) if isinstance(r, str) and r.strip()][:3]
    asks = {sid: ft._text((a.get("traders") or {}).get(sid), 200) for sid in ft.TRADERS} if isinstance(a.get("traders"), dict) else {}
    stance = {"week": week, "level": level, "why": ft._text(risk.get("why"), 200), "market": ft._text(risk.get("market"), 200),
              "rules": rules, "ts": now.isoformat(timespec="seconds")}
    record = {**stance, "summary": ft._text(a.get("summary"), 400), "traders": asks, "statements": statements,
              "stats": {k: scene.get(k) for k in ("traders", "mgr", "company", "regimes")}}
    mg._write_json(stance_path, stance)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    mg._write_json(state_path, {"week": week_id(now), "ts": stance["ts"]})
    if notifier is not None:
        said = []
        for sid, s in statements.items():
            said.append(f"{s['label']}：没交复盘（{s['error']}）" if s.get("error") else
                        f"{s['label']}\n　做对：{s['good']}\n　做错：{s['bad']}\n　下周：{s['next']}" + (f"\n　经理要求：{asks[sid]}" if asks.get(sid) else ""))
        body = "\n\n".join(["【总评】" + record["summary"], "【交易员】\n" + "\n".join(said),
                            "【下周做法】\n" + ("\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)) or "—"),
                            f"【经理的风险档位】{level}｜{stance['why']}" + (f"\n对下周市场的看法：{stance['market']}" if stance["market"] else ""),
                            "档位会带进下周经理的每一次判断。模拟账户。"])
        try:
            notifier.send(f"📅 周复盘｜{week}｜下周风险档位：{level}", body)
            notifier.flush(timeout=55)
        except Exception:                                          # noqa: BLE001
            pass
    return {"week": week, "level": level}


# ---------- 真实现场 ----------
def gather_scene(now: datetime) -> dict:
    """只读账本：每位交易员和经理账户这一周的账、公司记分、这一周每天最后一次的行情定性、收盘时的宽度。"""
    monday = week_start(now.date()).isoformat()
    conn = sqlite3.connect(f"{Path(ft.DB_PATH).as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        traders = {sid: {"label": t.label, **week_stats(conn, t.account, now)} for sid, t in ft.TRADERS.items()}
        mgr = week_stats(conn, config.STRATEGIES["mgr"]["accounts"][0], now)
        company = mg.company_score(conn, [(t.account, traders[sid]["equity"]) for sid, t in ft.TRADERS.items()], now)
        row = conn.execute("SELECT up, down, limit_up, limit_down FROM market_breadth ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    regimes = {}
    try:
        for line in Path(mg.LOG_PATH).read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if str(r.get("date", "")) >= monday:
                regimes[r["date"]] = {"date": r["date"], "slot": r.get("slot"), "regime": r.get("regime"), "falsify": r.get("falsify")}
    except (OSError, ValueError, KeyError):
        pass
    return {"week": week_id(now), "days": [monday, now.date().isoformat()], "traders": traders, "mgr": mgr, "company": company,
            "regimes": list(regimes.values()), "market": dict(zip(("up", "down", "limitUp", "limitDown"), row)) if row else {}}
