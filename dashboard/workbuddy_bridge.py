"""WorkBuddy DeepSeek 与隔离模拟闭环的桥接；不使用付费 API，不连接券商。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from dashboard.kimi_judge import SYSTEM_PROMPT, _convert_answer, _question_text
from dashboard.subscription_bridge import (
    ANSWERS_PATH,
    SNAPSHOT_PATH,
    create_snapshot,
    submit_answers,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLI = (ROOT / "dashboard" / "runtime" / "workbuddy-cli" / "node_modules" /
               "@tencent-ai" / "codebuddy-code" / "bin" / "codebuddy")
DEFAULT_MODEL = "deepseek-v4.1-flash"


def _node_executable() -> str:
    """Windows 的 node.cmd 不能直接由 CreateProcess 启动，优先使用真实 node.exe。"""
    configured = os.environ.get("WORKBUDDY_NODE")
    if configured:
        return configured
    bundled = sorted((ROOT / ".test-bin").glob("node-*/node.exe"))
    return str(bundled[-1]) if bundled else "node.exe"


class WorkBuddyError(RuntimeError):
    """WorkBuddy 未返回可验证的模拟策略答案。"""


def answer_schema(snapshot: dict) -> dict:
    """把当前问题收紧为 JSON Schema，避免模型增加候选或自由发挥。"""
    properties = {"snapshotId": {"const": snapshot["snapshotId"]}}
    for name, question in snapshot["questions"].items():
        if question["type"] == "score":
            criteria = question["criteria"]
            properties[name] = {
                "type": "object",
                "properties": {level: {"type": "number", "minimum": 0, "maximum": 1}
                               for level in criteria},
                "required": list(criteria), "additionalProperties": False,
            }
        elif question["type"] == "text":
            properties[name] = {
                "type": "object",
                "properties": {key: {"type": "string", "minLength": 1, "maxLength": 600} for key in question["criteria"]},
                "required": list(question["criteria"]), "additionalProperties": False,
            }
        elif question["type"] == "portfolio":
            criteria = question["criteria"]
            order = {
                "type": "object",
                "properties": {
                    "action": {"enum": list(criteria["actions"])},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string", "maxLength": 200},
                },
                "required": ["action", "confidence", "reason"], "additionalProperties": False,
            }
            properties[name] = {
                "type": "object",
                "properties": {
                    "posture": {"enum": list(criteria["posture"])},
                    "positionPct": {"enum": criteria["positionPct"]},
                    "orders": {
                        "type": "object",
                        "properties": {code: order for code in criteria["codes"]},
                        "required": list(criteria["codes"]), "additionalProperties": False,
                    },
                },
                "required": ["posture", "positionPct", "orders"], "additionalProperties": False,
            }
        else:
            properties[name] = {
                "type": "object",
                "properties": {
                    "choice": {"enum": list(question["criteria"])},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["choice", "confidence"], "additionalProperties": False,
            }
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def build_prompt(snapshot: dict) -> str:
    questions = snapshot["questions"]
    state = {"snapshotId": snapshot["snapshotId"], "context": snapshot["context"],
             "portfolioContext": snapshot["portfolioContext"]}
    return ("这是隔离模拟研究，不是真实交易。快照编号必须原样返回。"
            "只使用本次输入证据，不使用旧规则，不调用任何工具；证据不足时选择观察。"
            "枚举字段只能输出给定的键名，严禁拼接括号或解释；例如只写\"风险收缩\"，"
            "不能写\"风险收缩（缺少有效证据，仅观察）\"。confidence 必须是数字。\n"
            "实时数据：\n" + json.dumps(state, ensure_ascii=False)
            + "\n\n问题：\n" + "\n".join(_question_text(name, question)
                                         for name, question in questions.items())
            + "\n\n将 snapshotId 和每道题的答案合并成一个 JSON 对象。")


def _parse_answer(stdout: str) -> dict:
    if not stdout or not stdout.strip():
        raise WorkBuddyError("WorkBuddy 返回了空内容")
    try:
        value = json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        raise WorkBuddyError("WorkBuddy 的回答不是纯 JSON") from exc
    if not isinstance(value, dict):
        raise WorkBuddyError("WorkBuddy 的回答顶层不是对象")
    return value


def judge_snapshot(snapshot_path: Path = SNAPSHOT_PATH, answers_path: Path = ANSWERS_PATH,
                   cli_path: Path | None = None, model: str = DEFAULT_MODEL,
                   timeout: float = 90, runner=subprocess.run) -> dict:
    """调用一次 WorkBuddy 并严格验证答案；此函数实际执行时会消耗其订阅积分。"""
    snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    cli = Path(cli_path or os.environ.get("WORKBUDDY_CLI", DEFAULT_CLI))
    command = [
        _node_executable(), str(cli), "--print", "--model", model,
        "--tools", "", "--max-turns", "1", "--no-session-persistence",
        "--output-format", "text", "--json-schema",
        json.dumps(answer_schema(snapshot), ensure_ascii=False, separators=(",", ":")),
        "--system-prompt", SYSTEM_PROMPT, build_prompt(snapshot),
    ]
    try:
        completed = runner(command, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise WorkBuddyError(f"WorkBuddy 判断超时（{timeout:g} 秒）") from exc
    except OSError as exc:
        raise WorkBuddyError(f"无法启动 WorkBuddy：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "未知错误").strip()[-500:]
        raise WorkBuddyError(f"WorkBuddy 执行失败（{completed.returncode}）：{detail}")
    answer = _parse_answer(completed.stdout)
    if answer.get("snapshotId") != snapshot.get("snapshotId"):
        raise WorkBuddyError("WorkBuddy 答案与快照编号不一致")
    questions = snapshot["questions"]
    if set(answer) != {"snapshotId", *questions}:
        raise WorkBuddyError("WorkBuddy 答案缺少题目或包含额外字段")
    try:
        for name, question in questions.items():
            _convert_answer(question, answer[name])
    except (TypeError, ValueError) as exc:
        raise WorkBuddyError(f"WorkBuddy 答案未通过协议校验：{exc}") from exc
    answers_path = Path(answers_path)
    answers_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = answers_path.with_suffix(answers_path.suffix + ".tmp")
    temporary.write_text(json.dumps(answer, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, answers_path)
    return {"ok": True, "snapshotId": snapshot["snapshotId"], "model": f"workbuddy:{model}",
            "answersPath": str(answers_path)}


def run_cycle(cli_path: Path | None = None, model: str = DEFAULT_MODEL,
              timeout: float = 90, runner=subprocess.run) -> dict:
    """真实只读行情 → WorkBuddy 判断 → 本地模拟、风控、对账和通知。"""
    snapshot = create_snapshot()
    if not snapshot.get("eligible"):
        return snapshot
    judged = judge_snapshot(cli_path=cli_path, model=model, timeout=timeout, runner=runner)
    return submit_answers(model_name=judged["model"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["judge", "cycle"])
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT_PATH)
    parser.add_argument("--answers", type=Path, default=ANSWERS_PATH)
    parser.add_argument("--cli", type=Path, default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    if args.action == "judge":
        result = judge_snapshot(args.snapshot, args.answers, args.cli, args.model, args.timeout)
    else:
        result = run_cycle(args.cli, args.model, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
