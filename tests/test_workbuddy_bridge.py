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


def test_bridge_modules_have_no_broker_or_live_switch():
    import pathlib
    for name in ("dashboard/workbuddy_bridge.py", "research/candidate_paper_bridge.py",
                 "research/paper_validation.py", "research/strategy_lifecycle.py"):
        text = (pathlib.Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")
        for bad in ("GatewayBroker", "make_broker", "set_mode", "live_intents", "/orders", "XIADAN"):
            assert bad not in text, (name, bad)
