"""DeepSeek（经 WorkBuddy CLI）的判断桥接：只收严格校验过的答案，不接券商（Kimi 2026-10-31 写，dj 2026-11-01 同意入库）。"""
import json
from types import SimpleNamespace

import pytest

from dashboard import workbuddy_bridge as wb

SNAPSHOT = {"snapshotId": "2026-11-02T10:00:00", "context": {"x": 1}, "portfolioContext": {"y": 2}, "questions": {
    "market": {"type": "score", "instructions": "x", "criteria": ["冰点", "偏弱", "正常", "火热"]}}}


def _snap(tmp_path):
    p = tmp_path / "snap.json"
    p.write_text(json.dumps(SNAPSHOT, ensure_ascii=False), encoding="utf-8")
    return p


def _runner(stdout, code=0, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="boom")
    return run


GOOD = {"snapshotId": SNAPSHOT["snapshotId"], "market": {"冰点": 0.1, "偏弱": 0.6, "正常": 0.25, "火热": 0.05}}


def test_valid_answer_is_written_and_tools_are_disabled(tmp_path):
    calls = []
    out = wb.judge_snapshot(_snap(tmp_path), tmp_path / "ans.json", cli_path=tmp_path / "cli",
                            runner=_runner(json.dumps(GOOD, ensure_ascii=False), calls=calls))
    assert out["ok"] and json.loads((tmp_path / "ans.json").read_text(encoding="utf-8")) == GOOD
    cmd = calls[0]
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--max-turns") + 1] == "1"
    assert "--no-session-persistence" in cmd and out["model"].startswith("workbuddy:")


@pytest.mark.parametrize("stdout,code", [("", 0), ("不是 json", 0), (json.dumps([1]), 0),
                                         (json.dumps({**GOOD, "snapshotId": "旧快照"}), 0),
                                         (json.dumps({**GOOD, "extra": 1}), 0),
                                         (json.dumps({"snapshotId": SNAPSHOT["snapshotId"]}), 0),
                                         (json.dumps(GOOD), 1)])
def test_bad_answers_are_rejected_and_nothing_is_written(tmp_path, stdout, code):
    with pytest.raises(wb.WorkBuddyError):
        wb.judge_snapshot(_snap(tmp_path), tmp_path / "ans.json", cli_path=tmp_path / "cli",
                          runner=_runner(stdout, code))
    assert not (tmp_path / "ans.json").exists()


def test_answer_wrapped_in_prose_is_still_accepted(tmp_path):
    """11-01 预检：DeepSeek 偶尔在 JSON 前后带一句话，之前整轮作废。"""
    stdout = "以下是判断：\n```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    out = wb.judge_snapshot(_snap(tmp_path), tmp_path / "ans.json", cli_path=tmp_path / "cli",
                            runner=_runner(stdout))
    assert out["ok"] and json.loads((tmp_path / "ans.json").read_text(encoding="utf-8")) == GOOD


def test_bridge_modules_have_no_broker_or_live_switch():
    import pathlib
    for name in ("dashboard/workbuddy_bridge.py", "research/candidate_paper_bridge.py",
                 "research/paper_validation.py", "research/strategy_lifecycle.py"):
        text = (pathlib.Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")
        for bad in ("GatewayBroker", "make_broker", "set_mode", "live_intents", "/orders", "XIADAN"):
            assert bad not in text, (name, bad)


def test_analysis_text_question_is_in_schema_validated_and_kept_verbatim():
    """dj 2026-11-01：面板要看 DeepSeek 自己写的分析，原话保存，不拼模板。"""
    from dashboard.decision_journal import ANALYSIS_FIELDS, build_questions
    from dashboard.kimi_judge import _convert_answer
    q = build_questions(analysis=True)["analysis"]
    schema = wb.answer_schema({"snapshotId": "s", "questions": {"analysis": q}})
    assert set(schema["properties"]["analysis"]["required"]) == set(ANALYSIS_FIELDS)
    raw = {k: f"{k} 的原话" for k in ANALYSIS_FIELDS}
    assert _convert_answer(q, raw) == {"type": "text", "text": raw}
    with pytest.raises(ValueError):
        _convert_answer(q, {**raw, "review": "  "})
    assert "analysis" not in build_questions()                          # 不要的地方不问，Kimi 老路径不受影响


def test_record_keeps_analysis_and_previous_judgment_feeds_review(tmp_path):
    from datetime import datetime
    from dashboard.decision_journal import ANALYSIS_FIELDS, make_record
    from dashboard.subscription_bridge import _previous_judgment
    ctx = {"marketBreadth": {"up": 1, "down": 2, "upRatio": 33, "total": 3, "universe": 3, "limitUp": 0, "limitDown": 0},
           "sectorBreadth": {"up": 1, "down": 1, "leaders": []}}
    text = {k: f"{k}…" for k in ANALYSIS_FIELDS}
    answers = {"regime": {"choice": "防守观察", "confidence": 0.7}, "action": {"choice": "空仓等待", "confidence": 0.8},
               "analysis": {"type": "text", "text": text}}
    rec = make_record(answers, ctx, datetime(2026, 11, 2, 9, 40))
    assert rec["analysis"] == text
    path = tmp_path / "d.jsonl"
    assert _previous_judgment(path) is None
    path.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    assert _previous_judgment(path)["analysis"]["forecast"] == "forecast…"


def test_rate_limit_message_on_stderr_is_recognised(tmp_path):
    """11-01 实测：额度用完时返回码 0、stdout 空，429 提示在 stderr。"""
    def run(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout="", stderr="429 您的使用量已超出频率限制，将在 2026-11-01 20:27:01 UTC+8 重置")
    with pytest.raises(wb.WorkBuddyRateLimited) as e:
        wb.judge_snapshot(_snap(tmp_path), tmp_path / "ans.json", cli_path=tmp_path / "cli", runner=run)
    assert e.value.reset_at.hour == 20 and e.value.reset_at.minute == 27


def _limited(cmd, **kw):
    return SimpleNamespace(returncode=0, stdout="", stderr="429 您的使用量已超出频率限制，将在 2026-11-01 20:27:01 UTC+8 重置")


def test_rate_limited_falls_back_to_official_api_with_same_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "_api_key", lambda: "sk-test")
    sent = {}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, body=json, auth=headers["Authorization"])
        return SimpleNamespace(status_code=200, text="",
                               json=lambda: {"choices": [{"message": {"content": __import__("json").dumps(GOOD)}}]})
    out = wb.judge_snapshot(_snap(tmp_path), tmp_path / "ans.json", cli_path=tmp_path / "cli", runner=_limited, post=post)
    assert out["model"] == "deepseek-api:deepseek-flash"
    assert sent["url"] == wb.OFFICIAL_URL and sent["auth"] == "Bearer sk-test"
    assert sent["body"]["messages"][0]["content"] == wb.SYSTEM_PROMPT


def test_official_api_bad_answer_is_still_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "_api_key", lambda: "sk-test")
    post = lambda *a, **k: SimpleNamespace(status_code=200, text="",
                                           json=lambda: {"choices": [{"message": {"content": "{\"snapshotId\": \"旧\"}"}}]})
    with pytest.raises(wb.WorkBuddyError):
        wb.judge_snapshot(_snap(tmp_path), tmp_path / "ans.json", cli_path=tmp_path / "cli", runner=_limited, post=post)
    assert not (tmp_path / "ans.json").exists()


def test_official_api_failure_keeps_rate_limit_so_loop_waits(tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "_api_key", lambda: "sk-test")
    post = lambda *a, **k: SimpleNamespace(status_code=402, text="余额不足", json=lambda: {})
    with pytest.raises(wb.WorkBuddyRateLimited) as e:
        wb.judge_snapshot(_snap(tmp_path), tmp_path / "ans.json", cli_path=tmp_path / "cli", runner=_limited, post=post)
    assert "402" in str(e.value) and e.value.reset_at is not None


def test_portfolio_schema_requires_sector_and_next_step():
    from dashboard.adaptive_paper import build_portfolio_questions
    from dashboard.workbuddy_bridge import answer_schema
    q = type("Q", (), {"name": "奥飞娱乐"})()
    schema = answer_schema({"snapshotId": "s", "questions": build_portfolio_questions({"002292": q})})
    order = schema["properties"]["portfolio"]["properties"]["orders"]["properties"]["002292"]
    assert {"sector", "next"} <= set(order["required"])


def test_long_prompt_goes_through_stdin_not_the_command_line(tmp_path):
    # 2026-11-02 11:07：当日记忆越积越长，提示词放命令行超过 Windows 32K 上限（WinError 206），整轮失败
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw.get("input")
        return SimpleNamespace(returncode=0, stdout=json.dumps(GOOD, ensure_ascii=False), stderr="")
    big = {**SNAPSHOT, "portfolioContext": {"memory": "很长的记忆" * 20000}}
    p = tmp_path / "snap.json"
    p.write_text(json.dumps(big, ensure_ascii=False), encoding="utf-8")
    wb.judge_snapshot(p, tmp_path / "ans.json", cli_path=tmp_path / "cli", runner=run)
    assert "很长的记忆" in seen["input"]
    assert sum(len(x) for x in seen["cmd"]) < 30000


def test_portfolio_output_template_asks_for_sector_and_next():
    from dashboard.adaptive_paper import build_portfolio_questions
    from dashboard.kimi_judge import _question_text
    q = type("Q", (), {"name": "奥飞娱乐"})()
    text = _question_text("portfolio", build_portfolio_questions({"002292": q})["portfolio"])
    assert '"sector"' in text and '"next"' in text


def test_liquor_sector_is_open_for_first_board():
    # dj 2026-11-02：打首板了，酿酒板块放开
    assert "白酒等冷门" not in wb.SYSTEM_PROMPT and "酿酒" in wb.SYSTEM_PROMPT


def test_run_cycle_starts_shadows_and_records_main_judgment(tmp_path, monkeypatch):
    from dashboard import ai_score
    snap = {"snapshotId": "2026-11-03T09:40:00", "questions": {}}
    (tmp_path / "s.json").write_text(json.dumps(snap), encoding="utf-8")
    (tmp_path / "a.json").write_text(json.dumps({"snapshotId": snap["snapshotId"],
                                                 "action": {"choice": "空仓等待"}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(wb, "create_snapshot", lambda: {"eligible": True, "snapshotPath": str(tmp_path / "s.json")})
    monkeypatch.setattr(wb, "judge_snapshot", lambda **kw: {"model": "deepseek-api:deepseek-flash",
                                                             "answersPath": str(tmp_path / "a.json")})
    monkeypatch.setattr(wb, "submit_answers", lambda model_name: {"ok": True, "decisionId": "D1"})
    started = []
    monkeypatch.setattr(ai_score, "start_shadows", lambda s, gate: started.append(s["snapshotId"]) or ["x"])
    rows = []
    monkeypatch.setattr(ai_score, "append_judgment", rows.append)
    out = wb.run_cycle(model="deepseek-v4.1-flash")
    assert out["shadows"] == ["x"] and started == [snap["snapshotId"]]
    assert rows[0]["model"] == "deepseek-v4.1-flash" and rows[0]["role"] == "main" and rows[0]["gateOpen"] is False
