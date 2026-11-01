"""Kimi Work 订阅判断与本地模拟闭环的桥接；不读取 API 密钥、不请求付费 API。"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime
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
    eligible = (context.get("snapshot", {}).get("phase") == "盘中" and
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
    quotes = service._paper_quotes(codes)
    if not codes or set(quotes) != set(codes) or not all(_quote_valid(quotes.get(code), now) for code in codes):
        return {"eligible": False, "reason": "候选代码为空或行情缺失、过期、价格无效"}

    conn = paper._connect()
    try:
        account = paper._account_snapshot(conn, quotes)
    finally:
        conn.close()
    snapshot_id = now.isoformat(timespec="seconds")
    constraints = {"account": AI_ACCOUNT, "liveTrading": False, "initialCapital": AI_TOTAL_BUDGET,
                   "maxPositions": 1, "maxDailyBuys": 2, "minConfidence": 0.65,
                   "tPlusOne": True, "lot": 100}
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
        "questions": {**build_questions(), **build_portfolio_questions(quotes)},
        "portfolioContext": {
            "snapshot": context.get("snapshot"), "marketBreadth": market, "sectorBreadth": sectors,
            "account": account, "quotes": paper._quote_payload(quotes),
            "constraints": constraints,
        },
    }
    _atomic_json(output, value)
    result = {"eligible": True, "snapshotId": snapshot_id, "codes": codes, "snapshotPath": str(output)}
    if selection is not None:
        result["candidateId"] = selection["candidateId"]
    return result


def notify_result(notifier, record: dict, latest: dict, reconciled: bool) -> None:
    """分析每轮一条、成交每笔一条；推送失败不影响已经完成的模拟账本。"""
    portfolio = latest["portfolio"]
    execution = latest["execution"]
    orders = portfolio["orders"]
    order_lines = [f"{code}｜{order['action']}｜{order['reason'] or '仅观察'}"
                   for code, order in orders.items()]
    skipped = execution.get("skipped", [])
    analysis = "\n".join([
        f"时间：{record['ts']}",
        f"状态：{record['regime']} · {record['action']}",
        record["market"], record["sector"],
        f"组合：{portfolio['posture']}，仓位上限 {portfolio['positionPct']:.0%}",
        "候选判断：\n" + "\n".join(order_lines),
        "风控／跳过：" + ("；".join(skipped) if skipped else "无"),
        f"模拟成交：{len(execution.get('fills', []))} 笔；对账：{'一致' if reconciled else '异常'}",
        "仅为隔离模拟，不是真实委托。",
    ])
    notifier.send(f"📊 模拟分析｜{record['regime']}", analysis)
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
                                quote_provider=lambda codes: {code: quotes[code] for code in codes}, clock=lambda: now)
    journal = DecisionJournal(decisions_path, judge=judge, clock=lambda: now,
                              on_record=paper.process_record)
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
        notify_result(notifier, record, latest, paper_status["reconciled"])
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
