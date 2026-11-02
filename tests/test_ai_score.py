import json
import sqlite3
import threading
from types import SimpleNamespace

from dashboard import ai_score

GATE = "允许模拟小仓评估"
SNAP = {"snapshotId": "2026-11-03T09:40:00", "questions": {}}


def _answer(action, orders):
    return {"snapshotId": SNAP["snapshotId"], "regime": {"choice": "修复观察"}, "action": {"choice": action},
            "portfolio": {"posture": "进攻", "orders": {c: {"action": a, "confidence": 0.7, "reason": "x"}
                                                       for c, a in orders.items()}},
            "analysis": {"plan": "盯涨停"}}


def _db(tmp_path, rows):
    path = tmp_path / "jevq.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE board_events (date TEXT, code TEXT, name TEXT, first_seal TEXT, bucket TEXT, "
                 "concept TEXT, next_open_pct REAL, next_close_pct REAL, next_board INTEGER)")
    conn.execute("CREATE TABLE decisions (id INTEGER, ts TEXT, code TEXT, verdict TEXT, detail TEXT, trace TEXT, "
                 "strategy TEXT, version TEXT)")
    conn.executemany("INSERT INTO board_events VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO decisions VALUES (1,'2026-11-03T10:15','', 'sentiment', ?, '', '', '')",
                 (json.dumps({"stats": {"涨停家数": 60, "炸板率%": 18}}, ensure_ascii=False),))
    conn.commit()
    conn.close()
    return path


ROWS = [("2026-11-03", "600001", "甲", "094000", "早盘", "", 3.0, 1.0, 0),
        ("2026-11-03", "600002", "乙", "100500", "早盘", "", 1.0, -2.0, 0),
        ("2026-11-03", "000003", "丙", "140000", "下午", "", -6.0, -8.0, 0)]     # 窗口外，不算闸门溢价


def test_judgment_row_records_gate_and_orders():
    row = ai_score.judgment_row(SNAP, _answer(GATE, {"600001": "模拟买入"}), "m", "shadow", 12.34, GATE)
    assert row["date"] == "2026-11-03" and row["gateOpen"] is True and row["seconds"] == 12.3
    assert row["orders"] == {"600001": {"action": "模拟买入", "confidence": 0.7}} and row["plan"] == "盯涨停"


def test_outcome_uses_scan_window_and_waits_for_next_day(tmp_path):
    db = _db(tmp_path, ROWS + [("2026-11-04", "600009", "丁", "093500", "早盘", "", None, None, None)])
    out = ai_score.outcomes_for(["2026-11-03", "2026-11-04"], db)
    assert list(out) == ["2026-11-03"]                     # 11-04 次日还没回填，不记分
    assert out["2026-11-03"]["premium"] == 2.0 and out["2026-11-03"]["boards"] == 2


def test_scoreboard_counts_gate_and_stock_hits():
    outcome = {"premium": 2.0, "boards": 2, "codes": {"600001": 3.0, "600002": -1.0}}
    good = ai_score.judgment_row(SNAP, _answer(GATE, {"600001": "模拟买入", "600002": "观察", "600777": "模拟买入"}),
                                 "good", "main", 1, GATE)
    bad = ai_score.judgment_row(SNAP, _answer("空仓等待", {"600001": "观察", "600002": "模拟买入"}), "bad", "shadow", 1, GATE)
    failed = {"date": "2026-11-03", "model": "bad", "role": "shadow", "ok": False}
    board = ai_score.scoreboard([good, bad, failed], {"2026-11-03": outcome})
    assert board["good"] == {"role": "main", "rounds": 1, "days": 1, "failed": 0, "gateAcc": 1.0, "stockAcc": 1.0,
                             "stocks": 2}
    assert board["bad"]["gateAcc"] == 0.0 and board["bad"]["stockAcc"] == 0.0 and board["bad"]["failed"] == 1
    assert "good" in ai_score.scoreboard_text(board, {"2026-11-03": outcome})


def test_shadows_run_in_background_and_do_not_pile_up(tmp_path):
    path = tmp_path / "j.jsonl"
    release = threading.Event()

    def ask(snapshot, model, timeout):
        release.wait(5)
        if model == "broken":
            raise RuntimeError("bad json")
        return _answer("空仓等待", {})
    assert ai_score.start_shadows(SNAP, GATE, ("a", "broken"), ask, path) == ["a", "broken"]
    assert ai_score.start_shadows(SNAP, GATE, ("a", "broken"), ask, path) == []     # 上一轮还没答完
    release.set()
    for t in [t for t in threading.enumerate() if t.name.startswith("shadow-")]:
        t.join(5)
    rows = {r["model"]: r for r in ai_score.read_judgments(path)}
    assert rows["a"]["ok"] is True and rows["a"]["role"] == "shadow"
    assert rows["broken"]["ok"] is False and "bad json" in rows["broken"]["error"]


def test_lessons_that_keep_failing_are_switched_off():
    out = ai_score.validate_lessons({"lessons": [
        {"id": "L1", "text": "炸板率 > 30% → 空仓", "evidence": "3 天", "verified": 3, "failed": 0, "active": True},
        {"id": "L2", "text": "高开就打", "evidence": "", "verified": 1, "failed": 4, "active": True}]})
    assert [x["active"] for x in out] == [True, False]


def test_daily_job_scores_reviews_once_and_pushes(tmp_path):
    db = _db(tmp_path, ROWS)
    jpath = tmp_path / "j.jsonl"
    ai_score.append_judgment(ai_score.judgment_row(SNAP, _answer(GATE, {"600001": "模拟买入"}), "flash", "main", 30, GATE),
                             jpath)
    prompts = []

    def runner(cmd, **kw):
        prompts.append(kw["input"])
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"lessons": [
            {"id": "L1", "text": "涨停 60 家且炸板率 < 20% → 开闸", "evidence": "11-03", "verified": 1, "failed": 0,
             "active": True}]}, ensure_ascii=False))
    pushes = []
    notifier = SimpleNamespace(send=lambda t, c: pushes.append((t, c)), flush=lambda timeout: None)
    kw = dict(runner=runner, judgments_path=jpath, db_path=db, lessons_path=tmp_path / "lessons.json",
              scores_path=tmp_path / "scores.json")
    out = ai_score.daily_job(notifier=notifier, **kw)
    assert out["newDays"] == ["2026-11-03"] and out["lessons"] == 1
    assert "炸板率%" in prompts[0] and "firstBoardNextOpenAvg%" in prompts[0]
    assert json.loads((tmp_path / "lessons.json").read_text(encoding="utf-8"))["lessons"][0]["id"] == "L1"
    assert "赛马" in pushes[0][0] and "flash" in pushes[0][1]
    again = ai_score.daily_job(notifier=None, **kw)
    assert again["newDays"] == [] and len(prompts) == 1            # 复盘过的日子不重复复盘
