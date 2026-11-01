"""WorkBuddy DeepSeek 与隔离模拟闭环的桥接；不使用付费 API，不连接券商。"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from dashboard.kimi_judge import _convert_answer, _question_text
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

# 交易员角色（dj 2026-11-01）：只做龙头、打首板、超短线快进快出，市场不好空仓；单票不亏损。
# Kimi 那份「研究判断器」不动，只给 DeepSeek 用这一份。
SYSTEM_PROMPT = (
    "你是 A 股超短线交易员，做了 8 年情绪周期和板块轮动，只做主线题材的龙头，打首板。"
    "现在管一个 3 万元的模拟账户，盘中每 5 分钟看一次盘；扫板下单由程序在 3 秒级完成，你负责两件事："
    "一是判断现在该不该打板（开闸还是空仓），二是对观察池里的首板候选逐只表态。\n"
    "风格：中高风险，快进快出。情绪好、主线清楚、首板有赚钱效应时果断开闸；"
    "大盘普跌、跌停多、炸板率高、没有主线时空仓避险，空仓也是仓位。"
    "白酒等冷门、没题材的板块不做，股东户数 8 万以上的大盘股不做，跟风股不做，只做板块里最强的那只。\n"
    "铁律：单票不能亏着卖。买进后不及预期就进回本模式，靠做 T、低吸把成本做下来，回本就走，不长期拿；"
    "被套期间不开新票。所以开闸前要想清楚：这只板封不住、明天低开，账户能不能扛。\n"
    "写分析像盘中笔记：结论放前面，带具体数字，敢下判断，写清楚判断错了的信号。"
    "只用本次给的数据，缺的数据直接说缺，不编造。只输出一个 JSON 对象，不要 Markdown 或其他说明。"
)


def _node_executable() -> str:
    """Windows 的 node.cmd 不能直接由 CreateProcess 启动，优先使用真实 node.exe。"""
    configured = os.environ.get("WORKBUDDY_NODE")
    if configured:
        return configured
    bundled = sorted((ROOT / ".test-bin").glob("node-*/node.exe"))
    return str(bundled[-1]) if bundled else "node.exe"


class WorkBuddyError(RuntimeError):
    """WorkBuddy 未返回可验证的模拟策略答案。"""


class WorkBuddyRateLimited(WorkBuddyError):
    """WorkBuddy 额度/频率限制（429）；reset_at 是它说的恢复时间（北京时间，可能为 None）。"""

    def __init__(self, message: str, reset_at=None):
        super().__init__(message)
        self.reset_at = reset_at


_RESET = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*UTC\+8")


def _rate_limit(text: str):
    """11-01 实测：额度用完时返回码是 0、标准输出为空，提示「429 您的使用量已超出频率限制，将在 … UTC+8 重置」在错误输出里。"""
    if "429" not in text and "频率限制" not in text and "使用量" not in text:
        return None
    m = _RESET.search(text)
    from datetime import datetime as _dt
    reset = _dt.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None
    return WorkBuddyRateLimited(f"WorkBuddy 额度限流：{text.strip()[:200]}", reset)


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
    return ("模拟账户，不下真单。快照编号必须原样返回。不调用任何工具。\n"
            "动作的含义：action 选「允许模拟小仓评估」= 打开打首板闸门，程序会在候选冲板时买入；"
            "选「空仓等待」或「仅观察候选」= 关闸空仓。portfolio.orders 里对 portfolioContext.boardWatch 的首板候选："
            "「模拟买入」= 同意打这只，「观察」= 否决这只；对持仓：「模拟卖出」只在不亏的前提下才会执行。\n"
            "枚举字段只能输出给定的键名，严禁拼接括号或解释；例如只写\"风险收缩\"，"
            "不能写\"风险收缩（缺少有效证据，仅观察）\"。confidence 必须是数字。\n"
            "实时数据：\n" + json.dumps(state, ensure_ascii=False)
            + "\n\n问题：\n" + "\n".join(_question_text(name, question)
                                         for name, question in questions.items())
            + "\n\n将 snapshotId 和每道题的答案合并成一个 JSON 对象。")


def _parse_answer(stdout: str) -> dict:
    if not stdout or not stdout.strip():
        raise WorkBuddyError("WorkBuddy 返回了空内容")
    text = stdout.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        # 11-01 预检：同一份快照偶尔在 JSON 前后夹带说明文字，取最外层的大括号再试一次
        start, end = text.find("{"), text.rfind("}")
        try:
            value = json.loads(text[start:end + 1]) if 0 <= start < end else None
        except json.JSONDecodeError:
            value = None
        if value is None:
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
    out_text, err_text = completed.stdout or "", completed.stderr or ""
    limited = None if out_text.strip().startswith("{") else _rate_limit(out_text + "\n" + err_text)
    if limited is not None:
        raise limited
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "未知错误").strip()[-500:]
        raise WorkBuddyError(f"WorkBuddy 执行失败（{completed.returncode}）：{detail}")
    if not (completed.stdout or "").strip() and (completed.stderr or "").strip():
        raise WorkBuddyError(f"WorkBuddy 没有输出答案：{completed.stderr.strip()[-300:]}")
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
