import json
import sqlite3

from dashboard import ai_replay


def _db(tmp_path):
    path = tmp_path / "jevq.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE board_events (date TEXT, code TEXT, name TEXT, first_seal TEXT, bucket TEXT, "
                 "concept TEXT, next_open_pct REAL, next_close_pct REAL, next_board INTEGER)")
    conn.execute("CREATE TABLE decisions (id INTEGER, ts TEXT, code TEXT, verdict TEXT, detail TEXT, trace TEXT, "
                 "strategy TEXT, version TEXT)")
    conn.execute("CREATE TABLE market_breadth (ts TEXT, up INT, down INT, total INT, limit_up INT, limit_down INT, "
                 "amount_ratio REAL)")
    conn.execute("CREATE TABLE industries (code TEXT, industry TEXT, updated TEXT)")
    conn.executemany("INSERT INTO board_events VALUES (?,?,?,?,?,?,?,?,?)", [
        ("2026-10-20", "600001", "甲公司", "093500", "早盘", "机器人", 2.0, 1.0, 0),
        ("2026-10-21", "600002", "乙公司", "094000", "早盘", "机器人", 4.0, 3.0, 1),
        ("2026-10-21", "000003", "丙公司", "101500", "早盘", "", -2.0, -5.0, 0),
        ("2026-10-21", "000004", "丁公司", "133000", "下午", "", 9.0, 9.0, 1),     # 窗口外
        ("2026-10-22", "600005", "戊公司", "093100", "早盘", "", None, None, None)])  # 次日没回填
    conn.executemany("INSERT INTO industries VALUES (?,?,?)", [("600002", "电子信息", ""), ("000003", "电子信息", "")])
    conn.execute("INSERT INTO decisions VALUES (1,'2026-10-21T09:55','', 'sentiment', ?, '', '', '')",
                 (json.dumps({"stats": {"涨停家数": 40}}, ensure_ascii=False),))
    conn.execute("INSERT INTO decisions VALUES (2,'2026-10-21T10:20','', 'sentiment', ?, '', '', '')",
                 (json.dumps({"stats": {"涨停家数": 99}}, ensure_ascii=False),))      # 10:00 之后，不该看到
    conn.execute("INSERT INTO market_breadth VALUES ('2026-10-21 09:58', 3000, 2000, 5000, 40, 3, 5.0)")
    conn.commit()
    conn.close()
    return path


def test_case_is_anonymous_and_uses_only_morning_data(tmp_path):
    conn = sqlite3.connect(_db(tmp_path))
    case = ai_replay.build_case(conn, "2026-10-21", "2026-10-20")
    text = ai_replay.prompt_for(case)
    for leak in ("600002", "乙公司", "2026-10-21", "000004"):
        assert leak not in text, leak
    ctx = case["context"]
    assert ctx["情绪统计"] == {"涨停家数": 40} and ctx["全市场"]["涨停"] == 40
    assert ctx["昨日首板今日表现"]["今日平均高开%"] == 2.0
    assert list(ctx["扫板窗口首板"]) == ["S1", "S2"] and ctx["扫板窗口首板"]["S1"]["同行业今日首板数"] == 2
    assert case["answers"] == {"S1": 4.0, "S2": -2.0} and case["premium"] == 1.0
    assert set(ai_replay.schema_for(case)["properties"]["picks"]["required"]) == {"S1", "S2"}


def test_run_scores_models_and_skips_days_without_answers(tmp_path):
    def asker(case, model):
        if model == "bad":
            raise RuntimeError("timeout")
        return {"gate": "开闸", "confidence": 0.7, "reason": "x",
                "picks": {k: {"action": "打" if k == "S1" else "不打", "reason": ""} for k in case["answers"]}}
    rows, cases = ai_replay.run(("good", "bad"), days=5, workers=2, db_path=_db(tmp_path),
                                out_path=tmp_path / "r.jsonl", asker=asker)
    assert [c["day"] for c in cases] == ["2026-10-20", "2026-10-21"]
    s = ai_replay.summarize(rows)
    assert s["good"]["days"] == 2 and s["good"]["gateAcc"] == 1.0 and s["good"]["stockAcc"] == 1.0
    assert s["good"]["avgNextOpen%"] == 3.0 and s["bad"]["failed"] == 2


def test_failed_answers_are_retried_and_reruns_skip_done_days(tmp_path):
    calls = []

    def asker(case, model):
        calls.append((case["day"], model))
        if len(calls) == 1:
            raise RuntimeError("不是纯 JSON")
        return {"gate": "空仓", "confidence": 0.6, "reason": "x",
                "picks": {k: {"action": "不打", "reason": ""} for k in case["answers"]}}
    db, out = _db(tmp_path), tmp_path / "r.jsonl"
    rows, _ = ai_replay.run(("m",), days=5, workers=1, db_path=db, out_path=out, asker=asker)
    assert len(calls) == 3 and all(r["ok"] for r in rows) and len(rows) == 2
    rows, _ = ai_replay.run(("m",), days=5, workers=1, db_path=db, out_path=out, asker=asker)
    assert len(calls) == 3 and len(rows) == 2                 # 第二次全部跳过
