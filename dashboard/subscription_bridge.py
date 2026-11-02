"""Kimi Work 订阅判断与本地模拟闭环的桥接；不读取 API 密钥、不请求付费 API。"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, time as dtime
from pathlib import Path

from dashboard.adaptive_paper import (
    AdaptivePaperEngine,
    MAX_QUOTE_AGE_SECONDS,
    _quote_valid,
    build_portfolio_questions,
)
from dashboard.decision_journal import DecisionJournal, build_questions
from dashboard.kimi_judge import _convert_answer
from dashboard.server import DashboardService
from jevq import config as jevq_config
from jevq.config import AI_ACCOUNT, AI_TOTAL_BUDGET
from jevq.models import Quote
from jevq.notify import notifier_from_env
from research.candidate_paper_bridge import DEFAULT_REGISTRY, load_candidate_selection
from research.paper_validation import record_run, review as review_paper_run

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "dashboard" / "runtime"
SNAPSHOT_PATH = RUNTIME / "subscription_snapshot.json"
ANSWERS_PATH = RUNTIME / "subscription_answers.json"
DECISIONS_PATH = RUNTIME / "ai_decisions.jsonl"
PAPER_PATH = RUNTIME / "ai_paper.sqlite"


class SubscriptionJudge:
    """把一次订阅任务的结构化答案适配到现有两阶段判断协议。"""

    def __init__(self, answers: dict, model_name: str = "kimi-work-subscription"):
        self.answers = answers
        self.model_name = model_name

    def ask(self, state: dict, questions: dict, timeout=None, deadline=None) -> dict:
        del state, timeout, deadline
        return {name: _convert_answer(question, self.answers.get(name))
                for name, question in questions.items()}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _json_quote(quote: Quote) -> dict:
    value = asdict(quote)
    value["ts"] = quote.ts.isoformat()
    return value


def _load_quote(value: dict) -> Quote:
    return Quote(**{**value, "ts": datetime.fromisoformat(value["ts"])})


def _previous_judgment(path: Path = None) -> dict | None:
    """上一次 DeepSeek 自己写的分析（给它做复盘用）；读不到就是首次。"""
    path = Path(path or DECISIONS_PATH)
    if not path.exists():
        return None
    last = None
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    if row.get("analysis"):
                        last = row
    except (OSError, ValueError):
        return None
    if last is None:
        return None
    return {"time": last.get("ts"), "regime": last.get("regime"), "action": last.get("action"),
            "analysis": last["analysis"]}


MAIN_BOARD = ("60", "00")        # 同 jevq.data.akdata.MAIN_BOARD；那边要 import pandas/akshare，这里不引


def _main_board_only(codes) -> list[str]:
    """dj 2026-11-02：只看沪深主板（60/00 开头），创业板、科创板、北交所买不了，不给 DeepSeek 当候选。"""
    return [c for c in codes if str(c).startswith(MAIN_BOARD)]


def _industries(db_path, codes) -> dict[str, str]:
    """新浪行业归属（jevq.sqlite.industries），给 DeepSeek 写「所属板块」用；读不到就空。"""
    if not db_path or not Path(db_path).exists() or not codes:
        return {}
    try:
        conn = sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            marks = ",".join("?" * len(codes))
            return {c: i for c, i in conn.execute(f"SELECT code, industry FROM industries WHERE code IN ({marks})",
                                                  list(codes)) if i}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def _board_context(db_path, day) -> dict:
    """后台扫首板今天的观察池 + AI 打首板主账户（持仓、成本、回本价），给 DeepSeek 表态用。读不到就是空。"""
    out = {"watch": [], "account": None, "fills": [], "sentiment": None}
    if not db_path or not Path(db_path).exists():
        return out
    from jevq.data import candidates
    from jevq.models import Position
    from jevq.rules.ai_board import breakeven_price
    try:
        conn = sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            snap = candidates.latest(conn)
            if snap.get("date") == day.isoformat():
                out["watch"] = [{k: w.get(k) for k in ("code", "name", "concept", "heat", "pct", "verdict", "why")}
                                for w in snap["board"] if len(str(w.get("code", ""))) == 6]
            account = jevq_config.STRATEGIES["aiboard"]["accounts"][0]
            cash = conn.execute("SELECT amount FROM cash WHERE account=?", (account,)).fetchone()
            positions = []
            for code, shares, cost, buy_price, buy_date in conn.execute(
                    "SELECT code, shares, cost, buy_price, buy_date FROM positions WHERE account=?", (account,)):
                pos = Position(account, code, shares, cost, buy_price, date.fromisoformat(buy_date))
                positions.append({"code": code, "shares": shares, "cost": round(cost, 2), "buyPrice": buy_price,
                                  "buyDate": buy_date, "breakeven": breakeven_price(pos)})
            out["account"] = {"cash": None if cash is None else round(cash[0], 2), "positions": positions}
            # 交易记忆：最近 20 笔 AI 打首板成交（主账户），做 T、回本都要看着它
            out["fills"] = [{"ts": ts[:16], "side": side, "code": code, "price": price, "shares": shares,
                             "reason": (reason or "")[:60]}
                            for ts, side, code, price, shares, reason in conn.execute(
                                "SELECT ts, side, code, price, shares, reason FROM fills WHERE account=? "
                                "ORDER BY ts DESC LIMIT 20", (account,))]
            # 情绪统计（后台每隔几分钟记一次：涨停、跌停、炸板率、连板…），取最近一条
            row = conn.execute("SELECT ts, detail FROM decisions WHERE verdict='sentiment' "
                               "ORDER BY ts DESC LIMIT 1").fetchone()
            if row:
                detail = json.loads(row[1] or "{}")
                out["sentiment"] = {"ts": row[0][:16], "label": detail.get("label"), "stats": detail.get("stats")}
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    return out


LESSONS_PATH = RUNTIME / "ai_lessons.json"


def _today_notes(day, path: Path = None, limit: int = 12) -> list[dict]:
    """当日盘中笔记：今天每一轮 DeepSeek 自己的结论和计划（压缩成一行），让它记得自己前面干了什么。"""
    path = Path(path or DECISIONS_PATH)
    if not path.exists():
        return []
    notes = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                if not str(row.get("ts", "")).startswith(day.isoformat()):
                    continue
                analysis = row.get("analysis") or {}
                notes.append({"time": row.get("time"), "regime": row.get("regime"), "action": row.get("action"),
                              "market": (analysis.get("market") or "")[:80], "plan": (analysis.get("plan") or "")[:160]})
    except (OSError, ValueError):
        return notes[-limit:]
    return notes[-limit:]


def _lessons(path: Path = None) -> list:
    """收盘复盘攒下来的经验条（有依据次数和验证结果）；还没有就是空。"""
    try:
        data = json.loads(Path(path or LESSONS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [x for x in (data.get("lessons") or []) if x.get("active", True)][:20]


def create_snapshot(output: Path = SNAPSHOT_PATH, clock=datetime.now,
                    registry: Path = DEFAULT_REGISTRY) -> dict:
    """抓取一次真实只读行情和模拟账户，供订阅任务语义判断。"""
    os.environ["DASHBOARD_DECISION_SOURCE"] = "subscription"
    now = clock()
    selection = load_candidate_selection(now.date(), registry)
    service = DashboardService()
    service.market.payload(blocking=True)
    context = service.payload()
    market = context.get("marketBreadth", {})
    sectors = context.get("sectorBreadth", {})
    # 09:25 集合竞价出结果后到 09:30 开盘前算「竞价」轮（dj 2026-11-01：要分析竞价），让 DeepSeek 开盘前就定开不开闸
    phase_now = "竞价" if dtime(9, 25) <= now.time() < dtime(9, 30) and now.weekday() < 5 \
        else context.get("snapshot", {}).get("phase")
    eligible = (phase_now in ("盘中", "竞价") and
                all((market.get("complete"), sectors.get("complete"),
                     not market.get("stale"), not sectors.get("stale"))))
    if not eligible:
        return {"eligible": False, "reason": "非盘中或全市场、行业行情不完整或已过期"}

    paper = service.paper
    conn = paper._connect()
    try:
        paper._reconcile(conn, now)
        if selection is None:
            codes = paper._candidate_codes(conn, context)
        else:
            # 训练候选在场时股票池只由模拟持仓 + 当日候选组成；
            # 传空行业/观察的 context 复用同一查询，取到的就是持仓。
            held = paper._candidate_codes(conn, {"sectorBreadth": {}, "observations": []})
            codes = list(dict.fromkeys([*held, *selection["codes"]]))
    finally:
        conn.close()
    board = _board_context(getattr(service, "db_path", None), now.date())
    codes = _main_board_only(dict.fromkeys([*codes, *(w["code"] for w in board["watch"])]))
    quotes = service._paper_quotes(codes)
    # 首板候选取不到行情就不给 DeepSeek 看，不让一只票拖垮整轮
    dropped = {w["code"] for w in board["watch"]} - {c for c in codes if _quote_valid(quotes.get(c), now)}
    codes = [c for c in codes if c not in dropped]
    quotes = {c: q for c, q in quotes.items() if c in codes}
    board["watch"] = [w for w in board["watch"] if w["code"] not in dropped]
    if not codes or set(quotes) != set(codes) or not all(_quote_valid(quotes.get(code), now) for code in codes):
        return {"eligible": False, "reason": "候选代码为空或行情缺失、过期、价格无效"}

    conn = paper._connect()
    try:
        account = paper._account_snapshot(conn, quotes)
    finally:
        conn.close()
    snapshot_id = now.isoformat(timespec="seconds")
    industries = _industries(getattr(service, "db_path", None), codes)
    constraints = {"account": AI_ACCOUNT, "liveTrading": False, "initialCapital": AI_TOTAL_BUDGET,
                   "maxPositions": 1, "maxDailyBuys": 2, "minConfidence": 0.65,
                   "tPlusOne": True, "lot": 100,
                   "boardExecutor": {"account": "aiboard_main", "capital": jevq_config.INITIAL_CASH,
                                     "firstBuyPct": jevq_config.AI_BOARD_FIRST_PCT, "maxPositions": 1,
                                     "scanWindow": "09:30-10:30 涨到 9% 且没摸过涨停时扫板",
                                     "noLossSell": True}}
    if selection is not None:
        constraints["candidateId"] = selection["candidateId"]
        constraints["candidateSelection"] = {
            "candidateId": selection["candidateId"], "codes": list(selection["codes"]),
            "entryDate": selection["entryDate"], "scoreCutoff": selection["scoreCutoff"],
            "artifactSha256": selection["artifactSha256"]}
    value = {
        "snapshotId": snapshot_id,
        "createdAt": snapshot_id,
        "context": context,
        "quotes": {code: _json_quote(quote) for code, quote in quotes.items()},
        "questions": {**build_questions(analysis=True), **build_portfolio_questions(quotes)},
        "portfolioContext": {
            "snapshot": context.get("snapshot"), "marketBreadth": market, "sectorBreadth": sectors,
            "account": account,
            "quotes": {code: {**q, "industry": industries.get(code)} for code, q in paper._quote_payload(quotes).items()},
            "constraints": constraints,
            "previous": _previous_judgment(),
            "boardWatch": board["watch"], "boardAccount": board["account"],
            "memory": {"todayNotes": _today_notes(now.date()), "recentFills": board["fills"],
                       "sentiment": board["sentiment"], "lessons": _lessons()},
            "auction": phase_now == "竞价",
        },
    }
    _atomic_json(output, value)
    result = {"eligible": True, "snapshotId": snapshot_id, "codes": codes, "snapshotPath": str(output)}
    if selection is not None:
        result["candidateId"] = selection["candidateId"]
    return result


def _order_block(code: str, order: dict, quote) -> str:
    name = getattr(quote, "name", "") or ""
    head = f"▶ {code} {name}".rstrip()
    if order.get("sector"):
        head += f"（{order['sector']}）"
    if quote is not None and getattr(quote, "pre_close", 0):
        head += f" {quote.price:.2f} {quote.pct:+.2f}%"
        if quote.limit_up and quote.price >= quote.limit_up:
            head += " 涨停"
    head += f"｜{order['action']} {order.get('confidence', 0):.0%}"
    lines = [head, f"  理由：{order.get('reason') or '仅观察'}"]
    if order.get("next"):
        lines.append(f"  后续：{order['next']}")
    return "\n".join(lines)


def notify_result(notifier, record: dict, latest: dict, reconciled: bool, quotes: dict | None = None) -> None:
    """分析每轮一条、成交每笔一条；推送失败不影响已经完成的模拟账本。
    dj 2026-11-02：推送要写细——股票名、板块、每只票后续怎么做和触发条件、现在什么行情、什么情况做什么。"""
    quotes = quotes or {}
    portfolio = latest["portfolio"]
    execution = latest["execution"]
    orders = portfolio["orders"]
    skipped = execution.get("skipped", [])
    text = record.get("analysis") or {}
    gate_open = record["action"] == jevq_config.AI_BOARD_GATE_ACTION
    gate = ("闸门开：09:30-10:30 观察池里 DeepSeek 表态「模拟买入」的票涨到 9% 且没摸过涨停时扫板，首仓一半"
            if gate_open else
            "闸门关：今天不打板；09:30-10:30 扫板窗口里只有 DeepSeek 改判「允许模拟小仓评估」并对个股表态「模拟买入」才会买")
    sections = [
        f"{record['ts'][11:16]}｜{record['regime']} · {record['action']}｜置信 {record.get('confidence', 0):.0%}",
        "【动作】" + gate,
        "【大盘】" + (text.get("market") or record["market"]),
        "【板块】" + (text.get("sectors") or record["sector"]),
        f"【个股】组合{portfolio['posture']}，仓位上限 {portfolio['positionPct']:.0%}\n"
        + "\n".join(_order_block(code, order, quotes.get(code)) for code, order in orders.items()),
    ]
    if text.get("picks"):
        sections.append("【选股思路】" + text["picks"])
    if text.get("forecast"):
        sections.append("【预判】" + text["forecast"])
    if text.get("plan"):
        sections.append("【计划·触发条件】" + text["plan"])
    if text.get("review"):
        sections.append("【复盘上一轮】" + text["review"])
    sections += [
        "【执行】" + ("；".join(skipped) if skipped else "无跳过")
        + f"｜模拟成交 {len(execution.get('fills', []))} 笔｜对账{'一致' if reconciled else '异常'}",
        "仅为隔离模拟，不是真实委托。",
    ]
    notifier.send(f"📊 {record['regime']}｜{record['action']}", "\n\n".join(sections))
    for fill in execution.get("fills", []):
        side = "买入" if fill["side"] == "buy" else "卖出"
        reason = orders.get(fill["code"], {}).get("reason", "")
        notifier.send(f"💰 模拟{side}｜{fill['code']}",
                      f"{fill['code']} {fill['shares']} 股 @{fill['price']:.2f}\n依据：{reason}\n"
                      f"决策：{record['id']}\n仅为隔离模拟，不是真实委托。")
    notifier.flush(timeout=55)


def submit_answers(snapshot_path: Path = SNAPSHOT_PATH, answers_path: Path = ANSWERS_PATH,
                   decisions_path: Path = DECISIONS_PATH, paper_path: Path = PAPER_PATH,
                   clock=datetime.now, notifier=None,
                   model_name: str = "kimi-work-subscription",
                   registry: Path = DEFAULT_REGISTRY) -> dict:
    """校验订阅答案后复用本地账本；任何异常都不会生成真实委托。"""
    notifier = notifier or notifier_from_env(max_per_day=150)
    now = clock()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    if answers.get("snapshotId") != snapshot.get("snapshotId"):
        raise ValueError("订阅答案与行情快照编号不一致")
    created = datetime.fromisoformat(snapshot["createdAt"])
    age = (now - created).total_seconds()
    if not -30 <= age <= MAX_QUOTE_AGE_SECONDS:
        raise ValueError("订阅答案已超过行情有效期，拒绝处理模拟策略")

    quotes = {code: _load_quote(value) for code, value in snapshot["quotes"].items()}
    if not all(_quote_valid(quote, now) for quote in quotes.values()):
        raise ValueError("快照内候选行情已过期，拒绝处理模拟策略")
    judge = SubscriptionJudge(answers, model_name=model_name)
    paper = AdaptivePaperEngine(paper_path, judge=judge,
                                quote_provider=lambda codes: {code: quotes[code] for code in codes}, clock=lambda: now,
                                candidate_codes=list(quotes))
    journal = DecisionJournal(decisions_path, judge=judge, clock=lambda: now,
                              on_record=paper.process_record, analysis="analysis" in snapshot.get("questions", {}))
    journal.update_now(snapshot["context"])
    status = journal.status()
    if status["error"]:
        raise RuntimeError(status["error"])
    record = journal.records(1)[0]
    paper_status = paper.status()
    latest = paper_status["latestStrategy"]
    if status["listenerError"] or latest is None or latest["decisionId"] != record["id"] or latest["status"] != "done":
        raise RuntimeError(status["listenerError"] or (latest or {}).get("error") or "模拟策略未完成")
    notification_error = ""
    try:
        notify_result(notifier, record, latest, paper_status["reconciled"], quotes=quotes)
    except Exception as exc:
        notification_error = str(exc)
    result = {"ok": True, "decisionId": record["id"], "strategyId": latest["id"],
              "execution": latest["execution"], "reconciled": paper_status["reconciled"],
              "liveTrading": False, "model": record["model"],
              "notificationEnabled": notifier.enabled, "notificationError": notification_error}
    constraints = snapshot.get("portfolioContext", {}).get("constraints", {})
    candidate_id = constraints.get("candidateId") if isinstance(constraints, dict) else None
    if candidate_id:
        run = {"ts": now.isoformat(), "status": "completed",
               "reconciled": paper_status["reconciled"], "liveTrading": False,
               "equity": paper_status["account"]["total"], "execution": latest["execution"]}
        record_run(candidate_id, run, registry)
        review = review_paper_run(candidate_id, registry)
        result["candidateId"] = candidate_id
        result["paperReview"] = {
            "qualified": review.get("qualified"),
            "championAction": review.get("championAction"),
            "runCount": review.get("runCount"), "completedRuns": review.get("completedRuns"),
            "distinctDays": review.get("distinctDays"), "fills": review.get("fills"),
            "totalReturn": review.get("totalReturn"), "maxDrawdown": review.get("maxDrawdown"),
            "reasons": review.get("reasons"), "reviewPath": review.get("review_path"),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["snapshot", "submit"])
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT_PATH)
    parser.add_argument("--answers", type=Path, default=ANSWERS_PATH)
    args = parser.parse_args()
    result = (create_snapshot(args.snapshot) if args.action == "snapshot" else
              submit_answers(args.snapshot, args.answers))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
