"""AI 判断记分、多模型赛马和收盘复盘（dj 2026-11-02「WorkBuddy 11-27 到期，做吧」）。

- 每一轮：主模型（控制闸门）之外，陪跑模型对同一份快照各答一次，只记录不碰账户。
- 记分口径（全部从 board_events 来，第二天开盘后才知道答案）：
    闸门：当天 09:30-10:30 封的首板，次日平均开盘溢价 >= PREMIUM_OK 算「该开闸」，否则「该空仓」；
    个股：对当天真封了首板的票，「模拟买入」且次日高开 / 「观察」且次日不高开，算对。
- 收盘后：给还没记分的交易日记分，推送排行榜；再让模型复盘对错、更新经验条（ai_lessons.json）。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "dashboard" / "runtime"
JUDGMENTS_PATH = RUNTIME / "ai_judgments.jsonl"
SCORES_PATH = RUNTIME / "ai_scores.json"
LESSONS_PATH = RUNTIME / "ai_lessons.json"
DB_PATH = ROOT / "data" / "jevq.sqlite"

# 11-02 实测同一份快照：flash 36s、v4-pro 126s、kimi-k3.1-pro 141s、hy4-preview 215s、glm-5.3 超时
# dj 2026-11-02：kimi-k3.1-pro 太烧积分、hy4 太慢老超时，陪跑只留 v4-pro，每个模型至少隔 10 分钟才答一次
SHADOW_MODELS = ("deepseek-v4-pro",)
SHADOW_TIMEOUT = 280
SHADOW_MIN_GAP = 600
REVIEW_MODEL = "deepseek-v4-pro"
PREMIUM_OK = 1.0          # 首板次日平均开盘溢价 >= 1% 才算有赚钱效应、该开闸
SCAN_END = "103000"       # 扫板窗口 09:30-10:30：只看这之前封的首板
MAX_LESSONS = 20

_running: set[str] = set()
_last_start: dict[str, float] = {}
_lock = threading.Lock()


# ---------- 记录 ----------

def judgment_row(snapshot: dict, answer: dict, model: str, role: str, seconds: float, gate_action: str) -> dict:
    action = (answer.get("action") or {}).get("choice")
    portfolio = answer.get("portfolio") or {}
    return {
        "ts": snapshot["snapshotId"], "date": snapshot["snapshotId"][:10], "model": model, "role": role,
        "seconds": round(seconds, 1), "ok": True,
        "regime": (answer.get("regime") or {}).get("choice"), "action": action, "gateOpen": action == gate_action,
        "posture": portfolio.get("posture"),
        "orders": {code: {"action": o.get("action"), "confidence": o.get("confidence")}
                   for code, o in (portfolio.get("orders") or {}).items()},
        "plan": ((answer.get("analysis") or {}).get("plan") or "")[:300],
    }


def append_judgment(row: dict, path: Path = None) -> None:
    path = Path(path or JUDGMENTS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_judgments(path: Path = None) -> list[dict]:
    path = Path(path or JUDGMENTS_PATH)
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def ask_validated(snapshot: dict, model: str, timeout: float, runner=subprocess.run) -> dict:
    """问一个模型并按主模型同一套协议校验；不合格抛 WorkBuddyError。"""
    from dashboard import workbuddy_bridge as wb
    from dashboard.kimi_judge import _convert_answer
    answer = wb._parse_answer(wb._ask_workbuddy(snapshot, None, model, timeout, runner))
    if answer.get("snapshotId") != snapshot.get("snapshotId") or set(answer) != {"snapshotId", *snapshot["questions"]}:
        raise wb.WorkBuddyError("陪跑答案的快照编号或题目不对")
    try:
        for name, question in snapshot["questions"].items():
            _convert_answer(question, answer[name])
    except (TypeError, ValueError) as exc:
        raise wb.WorkBuddyError(f"陪跑答案未通过协议校验：{exc}") from exc
    return answer


def _shadow(snapshot: dict, model: str, gate_action: str, ask, path) -> None:
    t0 = time.monotonic()
    try:
        answer = ask(snapshot, model, SHADOW_TIMEOUT)
        append_judgment(judgment_row(snapshot, answer, model, "shadow", time.monotonic() - t0, gate_action), path)
    except Exception as exc:                                  # noqa: BLE001 —— 陪跑失败只记一笔，不影响主流程
        append_judgment({"ts": snapshot["snapshotId"], "date": snapshot["snapshotId"][:10], "model": model,
                         "role": "shadow", "seconds": round(time.monotonic() - t0, 1), "ok": False,
                         "error": f"{type(exc).__name__}: {str(exc)[:200]}"}, path)
    finally:
        with _lock:
            _running.discard(model)


def start_shadows(snapshot: dict, gate_action: str, models=SHADOW_MODELS, ask=None, path=None,
                  min_gap: float = SHADOW_MIN_GAP, clock=time.monotonic) -> list[str]:
    """后台线程里让陪跑模型各答一次；上一轮还没答完、或离上次开答不到 min_gap 秒的模型这轮跳过。返回这轮启动了哪些。"""
    ask = ask or ask_validated
    started = []
    for model in models:
        with _lock:
            now = clock()
            if model in _running or now - _last_start.get(model, -1e18) < min_gap:
                continue
            _running.add(model)
            _last_start[model] = now
        threading.Thread(target=_shadow, args=(snapshot, model, gate_action, ask, path),
                         name=f"shadow-{model}", daemon=True).start()
        started.append(model)
    return started


# ---------- 记分 ----------

def day_outcome(conn: sqlite3.Connection, day: str) -> dict | None:
    """当天扫板窗口内封的首板，次日开盘溢价；次日还没回填就返回 None（不能记分）。"""
    rows = conn.execute("SELECT code, first_seal, next_open_pct FROM board_events WHERE date=?", (day,)).fetchall()
    if not rows or any(r[2] is None for r in rows):
        return None
    window = [r for r in rows if str(r[1] or "999999") <= SCAN_END]
    premium = round(sum(r[2] for r in window) / len(window), 2) if window else None
    return {"premium": premium, "boards": len(window), "codes": {r[0]: r[2] for r in rows}}


def score_judgment(row: dict, outcome: dict) -> dict:
    gate = None
    if outcome["premium"] is not None:
        should_open = outcome["premium"] >= PREMIUM_OK
        gate = row["gateOpen"] == should_open
    hits = misses = 0
    for code, order in (row.get("orders") or {}).items():
        if code not in outcome["codes"]:
            continue
        up = outcome["codes"][code] > 0
        if (order["action"] == "模拟买入") == up:
            hits += 1
        else:
            misses += 1
    return {"gate": gate, "stockHits": hits, "stockMisses": misses}


def scoreboard(rows: list[dict], outcomes: dict[str, dict]) -> dict:
    """按模型汇总：闸门判对率、个股判对率；只算有答案的交易日。"""
    agg = defaultdict(lambda: {"rounds": 0, "gateRight": 0, "gateScored": 0, "stockHits": 0, "stockMisses": 0,
                               "failed": 0, "days": set(), "role": "shadow"})
    for row in rows:
        a = agg[row["model"]]
        if row.get("role") == "main":
            a["role"] = "main"
        if not row.get("ok"):
            a["failed"] += 1
            continue
        outcome = outcomes.get(row["date"])
        if outcome is None:
            continue
        s = score_judgment(row, outcome)
        a["rounds"] += 1
        a["days"].add(row["date"])
        if s["gate"] is not None:
            a["gateScored"] += 1
            a["gateRight"] += int(s["gate"])
        a["stockHits"] += s["stockHits"]
        a["stockMisses"] += s["stockMisses"]
    out = {}
    for model, a in agg.items():
        stocks = a["stockHits"] + a["stockMisses"]
        out[model] = {"role": a["role"], "rounds": a["rounds"], "days": len(a["days"]), "failed": a["failed"],
                      "gateAcc": round(a["gateRight"] / a["gateScored"], 3) if a["gateScored"] else None,
                      "stockAcc": round(a["stockHits"] / stocks, 3) if stocks else None, "stocks": stocks}
    return out


def outcomes_for(days, db_path: Path = None) -> dict[str, dict]:
    conn = sqlite3.connect(f"{Path(db_path or DB_PATH).as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        return {d: o for d in sorted(set(days)) if (o := day_outcome(conn, d)) is not None}
    finally:
        conn.close()


def scoreboard_text(board: dict, outcomes: dict) -> str:
    def pct(x):
        return "—" if x is None else f"{x:.0%}"
    lines = []
    for model, s in sorted(board.items(), key=lambda kv: (-(kv[1]["gateAcc"] or 0), kv[0])):
        tag = "主" if s["role"] == "main" else "陪"
        lines.append(f"[{tag}] {model}：闸门 {pct(s['gateAcc'])}（{s['rounds']} 轮/{s['days']} 天）"
                     f"｜个股 {pct(s['stockAcc'])}（{s['stocks']} 次）｜失败 {s['failed']}")
    days = "、".join(f"{d[5:]} 溢价 {o['premium']}%（{o['boards']} 只）" for d, o in sorted(outcomes.items())[-5:]
                     if o["premium"] is not None)
    return "\n".join(lines) + (f"\n\n最近几天首板次日溢价：{days}" if days else "") + \
        f"\n\n口径：扫板窗口首板次日平均高开 ≥ {PREMIUM_OK}% 算该开闸；个股以次日是否高开对答案。"


# ---------- 复盘 → 经验条 ----------

LESSON_SCHEMA = {
    "type": "object", "required": ["lessons"], "additionalProperties": False,
    "properties": {"lessons": {"type": "array", "maxItems": MAX_LESSONS, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "text", "evidence", "verified", "failed", "active"],
        "properties": {"id": {"type": "string", "maxLength": 20}, "text": {"type": "string", "maxLength": 160},
                       "evidence": {"type": "string", "maxLength": 200},
                       "verified": {"type": "integer", "minimum": 0}, "failed": {"type": "integer", "minimum": 0},
                       "active": {"type": "boolean"}}}}},
}

REVIEW_SYSTEM = ("你是 A 股超短线打首板交易员，现在收盘复盘。只用给的数据，结论具体、带数字。"
                 "只输出一个 JSON 对象，不要 Markdown 或其他说明。")


def read_lessons(path: Path = None) -> list[dict]:
    try:
        return json.loads(Path(path or LESSONS_PATH).read_text(encoding="utf-8")).get("lessons") or []
    except (OSError, ValueError):
        return []


def validate_lessons(raw: dict) -> list[dict]:
    lessons = raw.get("lessons") if isinstance(raw, dict) else None
    if not isinstance(lessons, list) or len(lessons) > MAX_LESSONS:
        raise ValueError("lessons 不是列表或超过上限")
    out = []
    for x in lessons:
        if not isinstance(x, dict) or not str(x.get("text", "")).strip():
            raise ValueError("经验条缺 text")
        verified, failed = int(x.get("verified", 0)), int(x.get("failed", 0))
        if verified < 0 or failed < 0:
            raise ValueError("验证次数不能为负")
        active = bool(x.get("active", True)) and not (failed >= 3 and failed > verified)   # 屡验不准的强制停用
        out.append({"id": str(x.get("id") or f"L{len(out) + 1}")[:20], "text": str(x["text"]).strip()[:160],
                    "evidence": str(x.get("evidence", ""))[:200], "verified": verified, "failed": failed,
                    "active": active})
    return out


def review_prompt(cases: list[dict], lessons: list[dict], source: str) -> str:
    return ("下面是打首板判断的对答案记录（" + source + "）。每条有：当时的市场情绪数据、你的判断（开闸/空仓、对个股打不打）、"
            "以及第二天的真实结果（首板次日开盘溢价）。\n"
            "任务：对照对错，更新经验条。规则：\n"
            "1. 经验条要能直接用来盘中判断，写成「条件 → 动作」，带具体阈值（如「炸板率 > 30% 且昨日涨停均涨 < 1% → 空仓」）。\n"
            "2. 已有经验条：这批记录里被验证对了 verified+1，被证伪 failed+1；证伪多于验证的 active=false。\n"
            "3. 新规律只有在至少 2 个交易日出现时才新增；总数不超过 " + str(MAX_LESSONS) + " 条，留最有用的。\n"
            "4. evidence 写依据（哪几天、多少比多少）。\n"
            "已有经验条：" + json.dumps(lessons, ensure_ascii=False) + "\n"
            "对答案记录：" + json.dumps(cases, ensure_ascii=False) + "\n"
            "输出 {\"lessons\": [{\"id\": \"L1\", \"text\": \"条件 → 动作\", \"evidence\": \"依据\", "
            "\"verified\": 整数, \"failed\": 整数, \"active\": true}]}")


def ask_lessons(prompt: str, model: str = REVIEW_MODEL, timeout: float = 600, runner=subprocess.run) -> list[dict]:
    from dashboard import workbuddy_bridge as wb
    command = [wb._node_executable(), str(Path(wb.os.environ.get("WORKBUDDY_CLI", wb.DEFAULT_CLI))), "--print",
               "--model", model, "--tools", "", "--max-turns", "1", "--no-session-persistence",
               "--output-format", "text", "--json-schema",
               json.dumps(LESSON_SCHEMA, ensure_ascii=False, separators=(",", ":")),
               "--system-prompt", REVIEW_SYSTEM]
    completed = runner(command, input=prompt, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, check=False)
    if completed.returncode != 0:
        raise wb.WorkBuddyError(f"复盘调用失败（{completed.returncode}）：{(completed.stderr or '')[-300:]}")
    return validate_lessons(wb._parse_answer(completed.stdout))


def save_lessons(lessons: list[dict], source: str, path: Path = None) -> None:
    path = Path(path or LESSONS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated": datetime.now().isoformat(timespec="seconds"), "source": source,
                               "lessons": lessons}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _cases_from_judgments(rows: list[dict], outcomes: dict, db_path: Path = None) -> list[dict]:
    """每个交易日取主模型最后一轮扫板窗口内的判断 + 当时情绪 + 次日结果，压成复盘用的案例。"""
    conn = sqlite3.connect(f"{Path(db_path or DB_PATH).as_uri()}?mode=ro", uri=True, timeout=5)
    cases = []
    try:
        for day, outcome in sorted(outcomes.items()):
            mine = [r for r in rows if r["date"] == day and r.get("role") == "main" and r.get("ok")
                    and r["ts"][11:16] <= "10:30"]
            if not mine:
                continue
            last = mine[-1]
            sent = conn.execute("SELECT detail FROM decisions WHERE verdict='sentiment' AND ts LIKE ? AND ts <= ? "
                                "ORDER BY ts DESC LIMIT 1", (f"{day}%", f"{day}T10:30")).fetchone()
            cases.append({"day": day, "sentiment": (json.loads(sent[0]).get("stats") if sent else None),
                          "judgment": {"action": last["action"], "gateOpen": last["gateOpen"], "plan": last.get("plan"),
                                       "orders": last["orders"]},
                          "result": {"firstBoardNextOpenAvg%": outcome["premium"], "boards": outcome["boards"],
                                     "orderOutcomes": {c: outcome["codes"][c] for c in last["orders"]
                                                       if c in outcome["codes"]}},
                          "score": score_judgment(last, outcome)})
    finally:
        conn.close()
    return cases


def daily_job(notifier=None, today: date | None = None, runner=subprocess.run,
              judgments_path: Path = None, db_path: Path = None, lessons_path: Path = None,
              scores_path: Path = None) -> dict:
    """收盘后跑一次：给有答案的交易日记分 → 推送排行榜 → 新记分的日子拿去复盘更新经验条。"""
    rows = read_judgments(judgments_path)
    outcomes = outcomes_for([r["date"] for r in rows], db_path)
    board = scoreboard(rows, outcomes)
    scores_path = Path(scores_path or SCORES_PATH)
    try:
        prev = json.loads(scores_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    reviewed = set(prev.get("reviewedDays", []))
    new_days = {d: o for d, o in outcomes.items() if d not in reviewed}
    result = {"scoredDays": sorted(outcomes), "newDays": sorted(new_days), "board": board, "lessons": None}
    if new_days:
        cases = _cases_from_judgments(rows, new_days, db_path)
        if cases:
            try:
                lessons = ask_lessons(review_prompt(cases, read_lessons(lessons_path), "盘中实盘判断"), runner=runner)
                save_lessons(lessons, "daily", lessons_path)
                result["lessons"] = len(lessons)
            except Exception as exc:                          # noqa: BLE001
                result["lessonError"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        reviewed |= set(new_days)
    tmp = scores_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated": datetime.now().isoformat(timespec="seconds"), "board": board,
                               "reviewedDays": sorted(reviewed)}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(scores_path)
    if notifier is not None and board:
        extra = ""
        if result.get("lessons") is not None:
            extra = f"\n\n复盘：新记分 {len(new_days)} 天，经验条更新为 {result['lessons']} 条。"
        elif result.get("lessonError"):
            extra = f"\n\n复盘失败：{result['lessonError']}"
        notifier.send("🏁 AI 赛马记分", scoreboard_text(board, outcomes) + extra)
        notifier.flush(timeout=30)
    return result
