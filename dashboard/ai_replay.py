"""历史复盘（dj 2026-11-02：WorkBuddy 额度 11-27 到期，拿来攒经验）。

board_events 里每个交易日的首板 + 当时的情绪统计 → 让几个模型逐日判断「开闸还是空仓、每只打不打」，
再用次日真实开盘溢价对答案：既是历史上的赛马，也给复盘出经验条（ai_lessons.json）。
防作弊：股票代码、名称、日期全部抹掉（模型可能记得具体哪天哪只票涨了），只留行业、概念、封板时间和统计数字。
只读 jevq.sqlite；不碰任何账户。用法：python -m dashboard.ai_replay [--models a,b] [--days 30] [--no-review]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import jevq  # noqa: F401  —— 进程时区固定成北京时间
from dashboard import ai_score

REPLAY_PATH = ai_score.RUNTIME / "ai_replay.jsonl"
SUMMARY_PATH = ai_score.RUNTIME / "ai_replay_summary.json"
MODELS = ("deepseek-v4.1-flash", "deepseek-v4-pro", "kimi-k3.1-pro", "hy4-preview")
DECIDE_AT = "10:00"          # 情绪统计取这个时刻之前最近的一条

SCHEMA_TEMPLATE = {
    "type": "object", "additionalProperties": False, "required": ["gate", "confidence", "reason", "picks"],
    "properties": {
        "gate": {"enum": ["开闸", "空仓"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "maxLength": 300},
        "picks": {"type": "object"},
    },
}

SYSTEM = ("你是 A 股超短线交易员，只做沪深主板首板。下面是某个历史交易日上午的盘面（日期和个股已匿名）。"
          "判断两件事：一是当天该不该打首板（开闸/空仓），二是窗口里封首板的票逐只打不打。"
          "只用给的数据，不要猜是哪一天、哪只票。只输出一个 JSON 对象，不要 Markdown 或其他说明。")


def _days(conn, limit: int) -> list[str]:
    rows = conn.execute("SELECT date, SUM(next_open_pct IS NULL) FROM board_events GROUP BY date ORDER BY date").fetchall()
    return [d for d, missing in rows if not missing][-limit:]


def build_case(conn, day: str, prev_day: str | None) -> dict:
    """一天的匿名盘面 + 答案（答案不进提示词）。"""
    sent = conn.execute("SELECT detail FROM decisions WHERE verdict='sentiment' AND ts LIKE ? AND substr(ts, 12, 5) <= ? "
                        "ORDER BY ts DESC LIMIT 1", (f"{day}%", DECIDE_AT)).fetchone()
    breadth = conn.execute("SELECT up, down, limit_up, limit_down, amount_ratio FROM market_breadth "
                           "WHERE ts LIKE ? AND substr(ts, 12, 5) <= ? ORDER BY ts DESC LIMIT 1",
                           (f"{day}%", DECIDE_AT)).fetchone()
    prev = None
    if prev_day:
        r = conn.execute("SELECT COUNT(*), AVG(next_open_pct), AVG(next_close_pct), SUM(next_board), "
                         "AVG(next_open_pct > 0) FROM board_events WHERE date=?", (prev_day,)).fetchone()
        prev = {"昨日首板数": r[0], "今日平均高开%": round(r[1], 2), "今日平均收盘涨%": round(r[2], 2),
                "今日再涨停": r[3], "今日高开占比": round(r[4], 2)}
    boards = conn.execute("SELECT b.code, b.first_seal, b.concept, i.industry, b.next_open_pct FROM board_events b "
                          "LEFT JOIN industries i ON i.code = b.code WHERE b.date=? AND b.first_seal <= ? "
                          "ORDER BY b.first_seal", (day, ai_score.SCAN_END)).fetchall()
    sector_count: dict[str, int] = {}
    for _c, _s, _con, ind, _p in boards:
        sector_count[ind or "未知"] = sector_count.get(ind or "未知", 0) + 1
    candidates, answers = {}, {}
    for n, (code, seal, concept, industry, nxt) in enumerate(boards, 1):
        key = f"S{n}"
        candidates[key] = {"封板时间": f"{seal[:2]}:{seal[2:4]}", "行业": industry or "未知", "概念": concept or "",
                           "同行业今日首板数": sector_count[industry or "未知"]}
        answers[key] = nxt
    premium = round(sum(answers.values()) / len(answers), 2) if answers else None
    return {"day": day, "context": {"情绪统计": json.loads(sent[0]).get("stats") if sent else None,
                                    "全市场": (dict(zip(("上涨", "下跌", "涨停", "跌停", "成交较昨日%"), breadth))
                                            if breadth else None),
                                    "昨日首板今日表现": prev, "扫板窗口首板": candidates},
            "answers": answers, "premium": premium}


def prompt_for(case: dict) -> str:
    return ("盘面（" + DECIDE_AT + " 左右）：\n" + json.dumps(case["context"], ensure_ascii=False) + "\n\n"
            "输出：{\"gate\": \"开闸\"或\"空仓\", \"confidence\": 0到1, \"reason\": \"带数字的理由\", "
            "\"picks\": {\"S1\": {\"action\": \"打\"或\"不打\", \"reason\": \"简短理由\"}, ...}}，"
            "picks 必须逐一覆盖扫板窗口首板里的每个编号，不能增加。")


def schema_for(case: dict) -> dict:
    keys = list(case["answers"])
    pick = {"type": "object", "additionalProperties": False, "required": ["action", "reason"],
            "properties": {"action": {"enum": ["打", "不打"]}, "reason": {"type": "string", "maxLength": 120}}}
    schema = json.loads(json.dumps(SCHEMA_TEMPLATE))
    schema["properties"]["picks"] = {"type": "object", "additionalProperties": False, "required": keys,
                                     "properties": {k: pick for k in keys}}
    return schema


def ask(case: dict, model: str, timeout: float = 400, runner=subprocess.run) -> dict:
    from dashboard import workbuddy_bridge as wb
    command = [wb._node_executable(), str(Path(wb.os.environ.get("WORKBUDDY_CLI", wb.DEFAULT_CLI))), "--print",
               "--model", model, "--tools", "", "--max-turns", "1", "--no-session-persistence",
               "--output-format", "text", "--json-schema",
               json.dumps(schema_for(case), ensure_ascii=False, separators=(",", ":")), "--system-prompt", SYSTEM]
    completed = runner(command, input=prompt_for(case), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, check=False)
    if completed.returncode != 0:
        raise wb.WorkBuddyError(f"返回码 {completed.returncode}：{(completed.stderr or '')[-200:]}")
    answer = wb._parse_answer(completed.stdout)
    if answer.get("gate") not in ("开闸", "空仓") or set(answer.get("picks") or {}) != set(case["answers"]) \
            or any((p or {}).get("action") not in ("打", "不打") for p in answer["picks"].values()):
        raise wb.WorkBuddyError("答案不合格式")
    return answer


def score(case: dict, answer: dict) -> dict:
    gate = None if case["premium"] is None else (answer["gate"] == "开闸") == (case["premium"] >= ai_score.PREMIUM_OK)
    hits = sum((p["action"] == "打") == (case["answers"][k] > 0) for k, p in answer["picks"].items())
    taken = [case["answers"][k] for k, p in answer["picks"].items() if p["action"] == "打"]
    return {"gate": gate, "stockHits": hits, "stocks": len(case["answers"]),
            "pickedAvg%": round(sum(taken) / len(taken), 2) if taken else None, "picked": len(taken)}


def summarize(rows: list[dict]) -> dict:
    out = {}
    for model in sorted({r["model"] for r in rows}):
        mine = [r for r in rows if r["model"] == model]
        ok = [r for r in mine if r.get("ok")]
        gates = [r["score"]["gate"] for r in ok if r["score"]["gate"] is not None]
        stocks = sum(r["score"]["stocks"] for r in ok)
        picked = [r for r in ok if r["gate"] == "开闸" and r["score"]["picked"]]
        # 模拟收益：只在开闸那天、按它点名「打」的票次日开盘卖，等权平均
        pnl = [r["score"]["pickedAvg%"] for r in picked]
        out[model] = {"days": len(ok), "failed": len(mine) - len(ok),
                      "gateAcc": round(sum(gates) / len(gates), 3) if gates else None,
                      "stockAcc": round(sum(r["score"]["stockHits"] for r in ok) / stocks, 3) if stocks else None,
                      "openDays": sum(r["gate"] == "开闸" for r in ok), "tradeDays": len(pnl),
                      "avgNextOpen%": round(sum(pnl) / len(pnl), 2) if pnl else None,
                      "avgSeconds": round(sum(r["seconds"] for r in ok) / len(ok), 1) if ok else None}
    return out


def run(models=MODELS, days: int = 30, workers: int = 8, db_path: Path = None, out_path: Path = REPLAY_PATH,
        asker=ask) -> list[dict]:
    conn = sqlite3.connect(f"{Path(db_path or ai_score.DB_PATH).as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        all_days = _days(conn, days + 1)
        cases = [build_case(conn, d, all_days[i - 1] if i else None) for i, d in enumerate(all_days)][-days:]
    finally:
        conn.close()
    cases = [c for c in cases if c["answers"]]
    done = {(r["day"], r["model"]) for r in read_rows(out_path) if r.get("ok")}     # 答过的不重复花额度
    jobs = [(c, m) for c in cases for m in models if (c["day"], m) not in done]

    write_lock = threading.Lock()

    def one(job):
        case, model = job
        t0 = time.monotonic()
        row = {"day": case["day"], "model": model, "premium": case["premium"]}
        for attempt in range(2):                              # 偶尔 reason 里夹了引号成不了 JSON，再问一次
            try:
                answer = asker(case, model)
                row.update(ok=True, gate=answer["gate"], confidence=answer.get("confidence"),
                           reason=answer.get("reason"), picks={k: v["action"] for k, v in answer["picks"].items()},
                           score=score(case, answer))
                row.pop("error", None)
                break
            except Exception as exc:                          # noqa: BLE001
                row.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")
        row["seconds"] = round(time.monotonic() - t0, 1)
        with write_lock, out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{row['day']} {model} {'ok' if row.get('ok') else row.get('error')}", flush=True)
        return row

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, jobs))
    wanted = {c["day"] for c in cases}
    return [r for r in latest_rows(read_rows(out_path)) if r["day"] in wanted and r["model"] in models], cases


def read_rows(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def latest_rows(rows: list[dict]) -> list[dict]:
    """同一天同一模型可能答过多次（失败后重跑）：有成功的取最后一次成功，否则取最后一次失败。"""
    best: dict = {}
    for r in rows:
        key = (r["day"], r["model"])
        if r.get("ok") or not (best.get(key) or {}).get("ok"):
            best[key] = r
    return list(best.values())


def review(rows: list[dict], cases: list[dict], runner=subprocess.run) -> list[dict]:
    """把每天的盘面、各模型的判断和真实结果交给复盘模型，写经验条（在已有经验条上更新）。"""
    by_day = {c["day"]: c for c in cases}
    review_cases = []
    for n, day in enumerate(sorted(by_day), 1):
        case = by_day[day]
        mine = [r for r in rows if r["day"] == day and r.get("ok")]
        review_cases.append({
            "day": f"D{n}", "盘面": {k: v for k, v in case["context"].items() if k != "扫板窗口首板"},
            "首板": {k: {**case["context"]["扫板窗口首板"][k], "次日开盘%": case["answers"][k]} for k in case["answers"]},
            "首板次日平均高开%": case["premium"],
            "各模型判断": {r["model"]: {"gate": r["gate"], "打": [k for k, a in r["picks"].items() if a == "打"],
                                   "对错": r["score"]} for r in mine}})
    lessons = ai_score.ask_lessons(ai_score.review_prompt(review_cases, ai_score.read_lessons(), "历史匿名复盘"),
                                   runner=runner)
    ai_score.save_lessons(lessons, "replay")
    return lessons


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=",".join(MODELS))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--no-review", action="store_true")
    args = parser.parse_args(argv)
    started = datetime.now()
    rows, cases = run(tuple(m for m in args.models.split(",") if m), args.days)
    summary = {"started": started.isoformat(timespec="seconds"), "days": len(cases),
               "baseline": {"首板次日平均高开%": round(sum(c["premium"] for c in cases) / len(cases), 2) if cases else None,
                            "该开闸天数": sum(c["premium"] >= ai_score.PREMIUM_OK for c in cases)},
               "models": summarize(rows)}
    if not args.no_review:
        try:
            summary["lessons"] = len(review(rows, cases))
        except Exception as exc:                              # noqa: BLE001
            summary["lessonError"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    try:
        from jevq.notify import notifier_from_env
        n = notifier_from_env()
        lines = [f"{m}：闸门 {s['gateAcc']}｜个股 {s['stockAcc']}｜开闸 {s['openDays']} 天，点名票次日均开 {s['avgNextOpen%']}%"
                 f"｜失败 {s['failed']}" for m, s in summary["models"].items()]
        n.send("📚 历史复盘赛马", f"{summary['days']} 个交易日（匿名）\n" + "\n".join(lines)
               + f"\n\n基准：首板次日平均高开 {summary['baseline']['首板次日平均高开%']}%，"
                 f"该开闸 {summary['baseline']['该开闸天数']} 天"
               + (f"\n经验条：{summary['lessons']} 条" if "lessons" in summary else f"\n复盘失败：{summary.get('lessonError')}"))
        n.flush(timeout=30)
    except Exception:                                         # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
