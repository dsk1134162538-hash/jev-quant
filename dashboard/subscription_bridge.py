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
from jevq.data import ztpool
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


def _stock_extras(db_path, quotes: dict, day) -> dict[str, dict]:
    """打板要看的个股数据（dj 2026-11-02 优化第 2 步）：封单（买一挂单 × 价，只在涨停价上算）、封单占成交、
    流通市值、5/20 日位置（到昨天收盘）、股东户数。读不到的字段就是 None。"""
    out = {}
    for code, q in quotes.items():
        sealed = bool(q.limit_up) and q.price >= q.limit_up and bool(getattr(q, "bid_vols", ()))
        seal = round(q.bid_vols[0] * 100 * q.price / 1e8, 2) if sealed else None
        out[code] = {"sealAmount亿": seal,
                     "sealRatio": round(seal * 1e8 / q.amount, 2) if seal and q.amount else None,
                     "floatMcap亿": round(q.float_mcap, 1) if getattr(q, "float_mcap", 0) else None,
                     "pos5%": None, "pos20%": None, "holders万": None}
    if not db_path or not Path(db_path).exists() or not quotes:
        return out
    try:
        conn = sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            for code in quotes:
                closes = [r[0] for r in conn.execute(
                    "SELECT close FROM closes WHERE code=? AND date<? ORDER BY date DESC LIMIT 21",
                    (code, day.isoformat()))]
                if len(closes) >= 6 and closes[5] > 0:
                    out[code]["pos5%"] = round((closes[0] / closes[5] - 1) * 100, 1)
                if len(closes) >= 21 and closes[20] > 0:
                    out[code]["pos20%"] = round((closes[0] / closes[20] - 1) * 100, 1)
                row = conn.execute("SELECT holders FROM holders WHERE code=?", (code,)).fetchone()
                if row and row[0]:
                    out[code]["holders万"] = round(row[0] / 1e4, 1)
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return out


TARGETS_PATH = Path(__file__).resolve().parent / "runtime" / "strategy_targets.json"
THEME_CACHE = Path(__file__).resolve().parent / "runtime" / "theme_cache.json"
THEME_CACHE_SECONDS = 120


def _theme_ladder(now, fetch_rows, prev_day, path: Path = THEME_CACHE, clock=None, with_members: bool = False) -> list[dict]:
    """今天的题材梯队（同花顺涨停揭秘按原因题材归好：涨停家数、最高板、龙头、阶段）。今天的每 120 秒刷新一次，
    前一天的每天只取一次，都缓存在文件里（AI 循环每 2~5 分钟一轮，不能每轮都联网）。取不到返回空，不拖垮快照。"""
    import time as _time
    from jevq.rules import themes
    clock = clock or _time.time
    try:
        try:
            cache = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        today = now.date().isoformat()
        rows = cache.get("rows") if cache.get("date") == today and clock() - cache.get("ts", 0) < THEME_CACHE_SECONDS else None
        if rows is None:
            rows = fetch_rows(now.date())
            cache.update(date=today, ts=clock(), rows=rows)
        prev_rows = []
        if prev_day is not None:
            prev_rows = cache.get("prev_rows") if cache.get("prev_date") == prev_day.isoformat() else None
            if prev_rows is None:
                prev_rows = fetch_rows(prev_day)
                cache.update(prev_date=prev_day.isoformat(), prev_rows=prev_rows)
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return themes.ladder(rows, prev_rows, with_members=with_members)
    except Exception:                                            # noqa: BLE001
        return []


def _prev_trading_day(db_path, today):
    if not db_path or not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute("SELECT MAX(date) FROM closes WHERE date < ?", (today.isoformat(),)).fetchone()
        finally:
            conn.close()
        return date.fromisoformat(row[0]) if row and row[0] else None
    except (sqlite3.Error, ValueError):
        return None


def _strategy_targets(now, fetch_quotes, path: Path = TARGETS_PATH) -> list[dict]:
    """高标接力、弱转强今天的标的和买入计划（dj 2026-11-03：「出了首板还有其他策略的标的吗」）。
    标的清单是 daemon 盘前算好写进文件的（只认今天的）；这里补实时行情，按竞价开盘幅度说明买不买。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if data.get("date") != now.date().isoformat():
        return []
    items = [("高标接力", c, n) for c, n in sorted((data.get("relay") or {}).items(), key=lambda kv: -kv[1])]
    items += [("弱转强", c, n) for c, n in sorted((data.get("wts") or {}).items(), key=lambda kv: -kv[1])]
    if not items:
        return []
    try:
        quotes = fetch_quotes([c for _, c, _ in items]) or {}
    except Exception:                                            # noqa: BLE001
        quotes = {}
    out = []
    for strategy, code, n in items:
        lo, hi = jevq_config.RELAY_GAP_PCT if strategy == "高标接力" else jevq_config.WTS_GAP_PCT
        label = f"{n} 板" if strategy == "高标接力" else f"炸板前 {n} 连板"
        q = quotes.get(code)
        if q is None or q.ts.date() != now.date() or q.open <= 0:
            plan = f"{label}；等 09:25 竞价结果（竞价高开 {lo:g}~{hi:g}% 才买）"
        else:
            gap = q.open_pct
            if lo <= gap < hi:
                verdict = f"在 {lo:g}~{hi:g}% 区间：09:30 按开盘价买入（主账户先过 Jev 排雷，每天最多 3 只），下一交易日 14:55 后卖"
            elif strategy == "高标接力" and gap >= hi:
                verdict = f"超过 {hi:g}%，预期已打满，不买"
            elif strategy == "高标接力":
                verdict = f"不到 {lo:g}%，资金不接力，不买"
            else:
                verdict = f"不在 {lo:g}~{hi:g}% 区间，不买"
            plan = f"{label}；竞价高开 {gap:+.2f}%，{verdict}"
        out.append({"strategy": strategy, "code": code, "name": getattr(q, "name", "") or code, "plan": plan})
    return out


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
    # 行情无效（取不到 / 时间戳超过有效期 / 价格无效）的候选只丢掉那一只，不让一只票拖垮整轮：冷门票、临停票的时间戳
    # 本来就可能很旧，2026-11-03 一天因此跳过 4 轮（含所有推送）。持仓票行情无效才放弃整轮——不能对着看不到价格的持仓做判断。
    bad = {c for c in codes if not _quote_valid(quotes.get(c), now)}
    if bad:
        conn = paper._connect()
        try:
            held_codes = set(paper._candidate_codes(conn, {"sectorBreadth": {}, "observations": []}))
        finally:
            conn.close()
        if bad & held_codes:
            return {"eligible": False, "reason": "持仓票行情缺失、过期或价格无效"}
    codes = [c for c in codes if c not in bad]
    quotes = {c: q for c, q in quotes.items() if c in codes}
    board["watch"] = [w for w in board["watch"] if w["code"] not in bad]
    if not codes:
        return {"eligible": False, "reason": "候选代码为空或行情缺失、过期、价格无效"}

    conn = paper._connect()
    try:
        account = paper._account_snapshot(conn, quotes)
    finally:
        conn.close()
    snapshot_id = now.isoformat(timespec="seconds")
    industries = _industries(getattr(service, "db_path", None), codes)
    extras = _stock_extras(getattr(service, "db_path", None), quotes, now.date())
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
            "quotes": {code: {**q, "industry": industries.get(code), **extras.get(code, {})}
                       for code, q in paper._quote_payload(quotes).items()},
            "constraints": constraints,
            "previous": _previous_judgment(),
            "boardWatch": board["watch"], "boardAccount": board["account"],
            "strategyTargets": _strategy_targets(now, service._paper_quotes),
            "themeLadder": _theme_ladder(now, ztpool.fetch_reasons,
                                         _prev_trading_day(getattr(service, "db_path", None), now.date())),
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


def _ladder_section(ladder) -> list[str]:
    """【题材梯队】：同花顺涨停揭秘按题材归好。None = 旧快照没有这一项，不写；空列表 = 今天还没有成型的梯队。"""
    if ladder is None:
        return []
    if not ladder:
        return ["【题材梯队】今天还没有成型的题材梯队（涨停揭秘里没有涨停 ≥2 家或最高板 ≥3 的题材）。"]
    lines = [f"▶ {t['theme']}｜涨停 {t['n']} 家（前一日 {t['prev_n']}）｜最高 {t['maxb']} 板（{'、'.join(t['leaders'])}）"
             f"｜{t['levels']} 层梯队" + (f"｜{t['phase']}" if t.get("phase") else "") for t in ladder]
    return ["【题材梯队】盘中涨停家数还在增加，「分歧/退潮」要收盘后才准\n" + "\n".join(lines)]


def notify_result(notifier, record: dict, latest: dict, reconciled: bool, quotes: dict | None = None,
                  targets: list[dict] | None = None, ladder: list[dict] | None = None) -> None:
    """分析每轮一条、成交每笔一条；推送失败不影响已经完成的模拟账本。
    dj 2026-11-02：推送要写细——股票名、板块、每只票后续怎么做和触发条件、现在什么行情、什么情况做什么。"""
    quotes = quotes or {}
    portfolio = latest["portfolio"]
    execution = latest["execution"]
    orders = portfolio["orders"]
    skipped = execution.get("skipped", [])
    text = record.get("analysis") or {}
    gate = ("闸门由规则定：上一交易日跌停多于涨停、且前日涨停股红盘率 < 40% 才空仓，其余日子 09:30-10:30 扫板；"
            "DeepSeek 对个股给「观察」就否决这只。买进后次日一开盘就卖。"
            f"DeepSeek 本轮看法：{'该打' if record['action'] == jevq_config.AI_BOARD_GATE_ACTION else '不该打'}")
    sections = [
        f"{record['ts'][11:16]}｜{record['regime']} · {record['action']}｜置信 {record.get('confidence', 0):.0%}",
        "【动作】" + gate,
        "【大盘】" + (text.get("market") or record["market"]),
        "【板块】" + (text.get("sectors") or record["sector"]),
        *_ladder_section(ladder),
        f"【个股】组合{portfolio['posture']}，仓位上限 {portfolio['positionPct']:.0%}\n"
        + "\n".join(_order_block(code, order, quotes.get(code)) for code, order in orders.items()),
    ]
    if targets is not None:             # None = 旧快照没有这一项，不写；空列表 = 今天确实没有
        lines = [f"▶ {t['strategy']} {t['code']} {t.get('name', '')}：{t['plan']}" for t in targets]
        body = "\n" + "\n".join(lines) if lines else "今天没有高标接力 / 弱转强的标的（昨日没有 3 板以上的高标，也没有炸板的高标）。"
        sections.append("【其他策略标的】" + body)
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


def notify_stale(notifier, snapshot: dict, answers: dict, age: float, now: datetime) -> None:
    """答案回来得太晚（行情已过期）：模拟账户不处理，分析照样推给 dj，标题和正文都写明延迟，价格不再显示（已过期）。"""
    regime = (answers.get("regime") or {}).get("choice") or "—"
    action = answers.get("action") or {}
    text = answers.get("analysis") or {}
    portfolio = answers.get("portfolio") or {}
    names = {}
    for code, raw in (snapshot.get("quotes") or {}).items():
        names[code] = {"name": raw.get("name", "")} if isinstance(raw, dict) else {}
    lines = []
    for code, order in (portfolio.get("orders") or {}).items():
        quote = type("Q", (), names.get(code, {}))()
        lines.append(_order_block(code, order, quote))
    sections = [f"{now:%H:%M}｜{regime} · {action.get('choice', '—')}｜置信 {action.get('confidence', 0):.0%}",
                f"⚠️ 这一轮分析回来得太晚（行情是 {age / 60:.1f} 分钟前的，已过期），只作参考：没有处理模拟账户。"]
    for label, key in (("大盘", "market"), ("板块", "sectors")):
        if text.get(key):
            sections.append(f"【{label}】" + text[key])
    if lines:
        sections.append("【个股】\n" + "\n".join(lines))
    for label, key in (("选股思路", "picks"), ("预判", "forecast"), ("计划·触发条件", "plan"), ("复盘上一轮", "review")):
        if text.get(key):
            sections.append(f"【{label}】" + text[key])
    notifier.send(f"📊 {regime}｜{action.get('choice', '—')}（延迟 {age / 60:.0f} 分钟）", "\n\n".join(sections))
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
    if age > MAX_QUOTE_AGE_SECONDS:
        try:        # 2026-11-03：机器被占满、一轮 6~8 分钟，答案回来行情已过期 → 整个下午一条推送都没有。
            notify_stale(notifier, snapshot, answers, age, now)    # 不处理模拟账户（不能按旧价成交），但分析照推
        except Exception:                                          # noqa: BLE001
            pass
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
        notify_result(notifier, record, latest, paper_status["reconciled"], quotes=quotes,
                      targets=snapshot.get("portfolioContext", {}).get("strategyTargets"),
                      ladder=snapshot.get("portfolioContext", {}).get("themeLadder"))
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
