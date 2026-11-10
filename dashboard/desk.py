"""交易台面板的数据（dj 2026-11-09，仿「横行 SIDEWAYS」）：一屏看清每个岗位在干什么、经理怎么判断、每位交易员卡在哪一层。

只读：jevq.sqlite 用 mode=ro 打开，其余读 dashboard/runtime 下经理和交易员写的文件。
- roles：汇报员的岗位卡（经理最近一次跑时写的 desk_reports.json；宽度换成秒级的）
- manager：今天的指令（不是今天的不显示）+ 今天每次判断的历史
- traders：每位交易员的账户（按秒级行情估值）、净值走势、今天每一轮、漏斗（候选 → AI 提名 → 经理拦下 → 通过 → 成交）
- log：经理、交易员、丢弃、成交混排的事件流；params：护栏参数
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from jevq import config

from dashboard import flex_trader as ft
from dashboard import manager as mg

CURVE_DAYS = 30


def _read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _today_rows(path: Path, day: str) -> list[dict]:
    rows = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines()[-200:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get("ts", ""))[:10] == day:
                rows.append(row)
    except OSError:
        pass
    return rows


def _live_breadth(report: dict, mb: dict, now: datetime) -> dict:
    if not (mb.get("available") and mb.get("complete") and not mb.get("stale")):
        return report
    up, down = mb.get("up") or 0, mb.get("down") or 0
    if not up + down:
        return report
    share = up / (up + down)
    line = f"涨 {up} / 跌 {down}"
    if mb.get("amount"):
        line += f" · 成交 {mb['amount'] / 1e8:,.0f} 亿"
    return {**report, "line": line, "level": "ok" if share >= 0.6 else "bad" if share <= 0.35 else "warn",
            "ts": f"{now:%H:%M}", "live": True}


def _account(conn, account: str, quotes: dict, day: str) -> tuple[dict, list]:
    cash_row = conn.execute("SELECT amount FROM cash WHERE account=?", (account,)).fetchone()
    capital = config.ACCOUNT_CAPITAL.get(account, config.FLEX_CAPITAL)
    if cash_row is None:
        return {"equity": capital, "cash": capital, "today": None, "total": 0.0, "positions": []}, []
    positions, mv = [], 0.0
    for code, shares, buy_price, buy_date in conn.execute(
            "SELECT code, shares, buy_price, buy_date FROM positions WHERE account=?", (account,)):
        q = quotes.get(code)
        if q is not None:
            price = q.price
        else:
            last = conn.execute("SELECT close FROM closes WHERE code=? ORDER BY date DESC LIMIT 1", (code,)).fetchone()
            price = last[0] if last else buy_price
        mv += price * shares
        positions.append({"code": code, "name": getattr(q, "name", None) or code, "shares": shares, "price": round(price, 2),
                          "pct": round((q.price / q.pre_close - 1) * 100, 2) if q is not None and q.pre_close else None,
                          "pnl": round((price / buy_price - 1) * 100, 2) if buy_price else None, "buyDate": buy_date})
    equity = cash_row[0] + mv
    prev = conn.execute("SELECT value FROM equity WHERE account=? AND date < ? ORDER BY date DESC LIMIT 1", (account, day)).fetchone()
    curve = [{"d": d, "v": round(v, 2)} for d, v in reversed(conn.execute(
        "SELECT date, value FROM equity WHERE account=? AND date < ? ORDER BY date DESC LIMIT ?", (account, day, CURVE_DAYS)).fetchall())]
    curve.append({"d": day, "v": round(equity, 2)})
    return {"equity": round(equity, 2), "cash": round(cash_row[0], 2),
            "today": round((equity / prev[0] - 1) * 100, 2) if prev and prev[0] else None,
            "total": round((equity / capital - 1) * 100, 2), "positions": positions}, curve


def _fills(conn, account: str, day: str) -> list[dict]:
    return [{"t": ts[11:16], "code": code, "side": side, "price": price, "shares": shares, "reason": (reason or "").split("｜")[0][:30]}
            for code, side, price, shares, ts, reason in conn.execute(
                "SELECT code, side, price, shares, ts, reason FROM fills WHERE account=? AND substr(ts, 1, 10)=? ORDER BY ts",
                (account, day))]


def _funnel(rounds: list, fills: list) -> dict:
    dropped = [d for r in rounds for d in r.get("dropped") or []]
    return {"candidates": max([r.get("candidates") or 0 for r in rounds] or [0]),
            "named": sum(r.get("rawOrders") or 0 for r in rounds),
            "manager": sum(1 for d in dropped if "经理" in str(d.get("why"))),
            "dropped": sum(1 for d in dropped if "经理" not in str(d.get("why"))),
            "passed": sum(1 for r in rounds for o in r.get("orders") or [] if o.get("action") == "买入"),
            "filled": sum(1 for f in fills if f["side"] == "buy")}


def _params() -> list[dict]:
    return [{"name": "单票上限", "value": f"{config.FLEX_MAX_PCT:.0%} 净值"},
            {"name": "最多持仓", "value": f"{config.ACCOUNT_MAX_POSITIONS.get('aiflex_main', 5)} 只"},
            {"name": "止损最远", "value": "12%"}, {"name": "追价偏离", "value": "≤2%"},
            {"name": "订单有效", "value": "10 分钟"},
            {"name": "日亏停手", "value": f"-{config.FLEX_DAY_LOSS:.0%}"},
            {"name": "连亏停手", "value": f"{config.FLEX_LOSS_STREAK} 笔"},
            {"name": "可交易", "value": "沪深主板 60/00 · T+1"},
            {"name": "经理时点", "value": " ".join(mg.SLOTS)}, {"name": "交易员时点", "value": " ".join(ft.SLOTS)}]


def desk_payload(db_path: Path | None, runtime_dir: Path, quotes: dict, market_breadth: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    day = now.date().isoformat()
    rt = Path(runtime_dir)
    reports = _read_json(rt / mg.REPORTS_PATH.name)
    roles = [r for r in reports.get("reports") or [] if not str(r.get("role", "")).startswith("trader:")] \
        if str(reports.get("ts", ""))[:10] == day else []
    roles = [_live_breadth(r, market_breadth or {}, now) if r.get("role") == "breadth" else r for r in roles]

    directive = _read_json(rt / mg.DIRECTIVE_PATH.name)
    history = _today_rows(rt / mg.LOG_PATH.name, day)
    manager = None
    if directive.get("date") == day:
        manager = {k: directive.get(k) for k in ("slot", "ts", "regime", "read", "traders", "falsify", "watch")}
        manager["history"] = [{k: h.get(k) for k in ("slot", "regime", "falsify", "read")} for h in history]

    log: list[dict] = []
    for h in history:
        on = [ft.TRADERS[s].label for s, t in (h.get("traders") or {}).items() if s in ft.TRADERS and t.get("buy")]
        log.append({"t": str(h.get("ts", ""))[11:19], "who": "经理", "tag": "经理",
                    "text": f"{h.get('regime')}｜开：{'、'.join(on) or '无'}｜否证：{h.get('falsify') or '—'}"})

    traders = []
    conn = None
    if db_path and Path(db_path).exists():
        try:
            conn = sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            conn = None
    try:
        for sid, t in ft.TRADERS.items():
            rounds = _today_rows(rt / t.log_path.name, day)
            account, curve, fills = {"equity": config.FLEX_CAPITAL, "cash": config.FLEX_CAPITAL, "today": None, "total": 0.0,
                                     "positions": []}, [], []
            if conn is not None:
                try:
                    account, curve = _account(conn, t.account, quotes, day)
                    fills = _fills(conn, t.account, day)
                except sqlite3.Error:
                    pass
            names = {p["code"]: p["name"] for p in account["positions"]}
            for r in rounds:
                a = r.get("analysis") or {}
                ts = str(r.get("ts", ""))[11:19]
                if r.get("skipped"):
                    log.append({"t": ts, "who": t.label, "tag": "跳过", "text": r["skipped"]})
                    continue
                log.append({"t": ts, "who": t.label, "tag": "判断", "text": f"{r.get('slot')} {a.get('stage') or '—'}｜{a.get('plan') or ''}"})
                for o in r.get("orders") or []:
                    log.append({"t": ts, "who": t.label, "tag": o.get("action", "订单"),
                                "text": f"{o.get('code')} {format(o['pct'], '.0%') + ' 仓 ' if isinstance(o.get('pct'), (int, float)) else ''}{o.get('reason') or ''}"})
                for d in r.get("dropped") or []:
                    log.append({"t": ts, "who": t.label, "tag": "丢弃", "text": f"{d.get('action') if d.get('action') not in (None, 'None') else ''}{d.get('code')}｜{d.get('why')}"})
            for f in fills:
                q = quotes.get(f["code"])
                f["name"] = getattr(q, "name", None) or names.get(f["code"], f["code"])
                log.append({"t": f["t"] + ":00", "who": t.label, "tag": "成交",
                            "text": f"{'买' if f['side'] == 'buy' else '卖'} {f['name']} {f['price']} × {f['shares']}｜{f['reason']}"})
            traders.append({"sid": sid, "label": t.label, "style": t.style, "account": account, "curve": curve, "fills": fills,
                            "rounds": [{"slot": r.get("slot"), "t": str(r.get("ts", ""))[11:16], "stage": (r.get("analysis") or {}).get("stage"),
                                        "plan": (r.get("analysis") or {}).get("plan"), "market": (r.get("analysis") or {}).get("market"),
                                        "orders": r.get("orders") or [], "dropped": r.get("dropped") or [], "skipped": r.get("skipped")}
                                       for r in rounds],
                            "directive": ((manager or {}).get("traders") or {}).get(sid), "funnel": _funnel(rounds, fills)})
    finally:
        if conn is not None:
            conn.close()
    log.sort(key=lambda e: e["t"], reverse=True)
    calls = len(history) + sum(1 for t in traders for r in t["rounds"] if not r.get("skipped"))
    strategy = _strategy_desk(rt / "desk_submissions.json", day)
    return {"now": now.isoformat(timespec="seconds"), "roles": roles, "rolesAt": reports.get("ts") if roles else None,
            "manager": manager, "traders": traders, "strategyDesk": strategy, "log": log[:120], "params": _params(), "calls": calls}


def _strategy_desk(path: Path, day: str) -> dict | None:
    """策略交易员 → 经理（dashboard/strategy_desk）最近一轮的提交，只认今天的；证据太长，面板不要。"""
    try:
        rec = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if rec.get("date") != day:
        return None
    slim = lambda rows: [{k: v for k, v in r.items() if k != "evidence"} for r in rows or []]      # noqa: E731
    return {"slot": rec.get("slot"), "ts": rec.get("ts"), "final": rec.get("final"),
            "traders": [{**{k: t.get(k) for k in ("key", "label", "candidates", "passed", "rejected", "error")},
                         "picks": slim(t.get("picks")), "vetoed": slim(t.get("vetoed"))} for t in rec.get("traders") or []]}
