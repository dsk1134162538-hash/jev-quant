"""Kimi 盘中研究判断器。只返回结构化研究结论，不连接交易接口。"""
from __future__ import annotations

import json
import math
import os
import re

import requests

DEFAULT_KIMI_BASE_URL = "https://agent-gw.kimi.com/coding/"
DEFAULT_KIMI_MODEL = "kimi-for-coding"
SYSTEM_PROMPT = (
    "你是 A 股盘中研究与模拟策略判断器。只根据输入的实时证据回答，不使用扫首板、超跌、低吸等旧规则，"
    "不补造缺失数据，不给出真实买卖指令；只有问题明确要求时才输出隔离模拟账户的结构化意图。"
    "即使证据不足，也只能从给定选项中选择最保守的研究状态。只输出一个 JSON 对象，不要 Markdown 或其他说明。"
)


class KimiJudgeError(RuntimeError):
    """Kimi 未给出可验证的结构化回答。"""


def chat_completions_url(base_url: str) -> str:
    """接受 coding 根地址、v1 根地址或完整的 chat/completions 地址。"""
    base = (base_url or DEFAULT_KIMI_BASE_URL).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def extract_json_object(content: str) -> dict:
    """解析纯 JSON 或带代码围栏的 JSON；拒绝空回答和多余的非 JSON 内容。"""
    if not isinstance(content, str) or not content.strip():
        raise KimiJudgeError("Kimi 返回了空内容")
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise KimiJudgeError("Kimi 回答不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise KimiJudgeError("Kimi 回答的 JSON 顶层不是对象")
    return value


def _question_text(name: str, question: dict) -> str:
    if question["type"] == "score":
        levels = list(question["criteria"])
        return (f"- {name}：{question['instructions']}\n  给这些档位分别一个概率，总和为 1：{'、'.join(levels)}\n"
                f"  输出 \"{name}\": {{" + ", ".join(f'\"{level}\": 概率' for level in levels) + "}")
    if question["type"] == "portfolio":
        criteria = question["criteria"]
        postures = "；".join(f"{key}（{value}）" for key, value in criteria["posture"].items())
        actions = "、".join(criteria["actions"])
        codes = "、".join(f"{code}（{stock_name}）" for code, stock_name in criteria["codes"].items())
        return (f"- {name}：{question['instructions']}\n  策略姿态：{postures}\n"
                f"  总仓位上限只能从 {criteria['positionPct']} 中选择；0 表示不允许模拟买入。\n"
                f"  必须逐一评价这些代码，不能增加或遗漏：{codes}\n"
                f"  每个代码的动作只能是：{actions}。模拟卖出只针对当前持仓，模拟买入只针对非持仓。\n"
                f"  输出 \"{name}\": {{\"posture\": \"姿态\", \"positionPct\": 数值, "
                f"\"orders\": {{\"代码\": {{\"action\": \"动作\", \"confidence\": 0到1, \"reason\": \"简短证据\"}}}}}}")
    options = "；".join(f"{key}（{value}）" for key, value in question["criteria"].items())
    return (f"- {name}：{question['instructions']}\n  只能选一项：{options}\n"
            f"  输出 \"{name}\": {{\"choice\": \"选项名\", \"confidence\": 0到1}}")


def _confidence(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence 不是数字") from exc
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError("confidence 必须在 0 到 1 之间")
    return number


def _convert_portfolio(criteria: dict, raw: dict) -> dict:
    posture = raw.get("posture")
    if posture not in criteria["posture"]:
        raise ValueError(f"策略姿态不在允许范围：{posture!r}")
    try:
        position_pct = float(raw.get("positionPct"))
    except (TypeError, ValueError) as exc:
        raise ValueError("positionPct 不是数字") from exc
    if not math.isfinite(position_pct) or position_pct not in criteria["positionPct"]:
        raise ValueError("positionPct 不在允许范围")
    orders = raw.get("orders")
    codes = set(criteria["codes"])
    if not isinstance(orders, dict) or set(orders) != codes:
        raise ValueError("模拟策略必须逐一覆盖候选代码，且不能增加代码")
    converted = {}
    for code in criteria["codes"]:
        order = orders[code]
        if not isinstance(order, dict) or order.get("action") not in criteria["actions"]:
            raise ValueError(f"{code} 的模拟动作不在允许范围")
        confidence = _confidence(order.get("confidence"))
        if confidence is None:
            raise ValueError(f"{code} 缺少 confidence")
        reason = order.get("reason", "")
        if not isinstance(reason, str) or len(reason.strip()) > 200:
            raise ValueError(f"{code} 的 reason 无效或超过 200 字")
        if order["action"] != "观察" and not reason.strip():
            raise ValueError(f"{code} 的模拟动作缺少证据")
        converted[code] = {"action": order["action"], "confidence": confidence, "reason": reason.strip()}
    return {"type": "portfolio", "posture": posture, "positionPct": position_pct,
            "orders": converted, "confidence": min(order["confidence"] for order in converted.values())}


def _convert_answer(question: dict, raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("单题回答不是对象")
    if question["type"] == "portfolio":
        return _convert_portfolio(question["criteria"], raw)
    if question["type"] == "score":
        levels = list(question["criteria"])
        if not levels or set(raw) - set(levels):
            raise ValueError("概率档位与问题不一致")
        try:
            values = [max(0.0, float(raw.get(level, 0) or 0)) for level in levels]
        except (TypeError, ValueError) as exc:
            raise ValueError("概率不是数字") from exc
        if not all(math.isfinite(value) for value in values) or sum(values) <= 0:
            raise ValueError("概率必须是非负有限数字且总和大于 0")
        total = sum(values)
        probabilities = {str(index): round(value / total, 4) for index, value in enumerate(values)}
        top = max(probabilities.values())
        return {"type": "score", "legend": {str(index): level for index, level in enumerate(levels)},
                "probabilities": probabilities, "confidence": top, "score": top}
    choice = raw.get("choice")
    if choice not in question["criteria"]:
        raise ValueError(f"选项不在允许范围：{choice!r}")
    return {"type": "choice", "choice": choice, "confidence": _confidence(raw.get("confidence"))}


class KimiJudge:
    """与 DecisionJournal 的 judge.ask(context, questions) 协议兼容。"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, session=None, timeout: float = 90):
        self.api_key = api_key if api_key is not None else os.environ.get("KIMI_API_KEY", "")
        if not self.api_key:
            raise ValueError("未配置 KIMI_API_KEY")
        self.base_url = base_url or os.environ.get("KIMI_BASE_URL") or DEFAULT_KIMI_BASE_URL
        self.model_name = model or os.environ.get("KIMI_DECISION_MODEL") or DEFAULT_KIMI_MODEL
        self.url = chat_completions_url(self.base_url)
        self.session = session or requests.Session()
        self.timeout = timeout

    def ask(self, state: dict, questions: dict, timeout: float | None = None, deadline=None) -> dict:
        del deadline  # 当前面板不在本次调用内重试；失败由日志调度器退避。
        user = ("实时数据：\n" + json.dumps(state, ensure_ascii=False, indent=1)
                + "\n\n问题：\n" + "\n".join(_question_text(name, question)
                                             for name, question in questions.items())
                + "\n\n把每道题的答案合并成一个 JSON 对象，键名必须与题目名一致。")
        body = {
            "model": self.model_name,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        try:
            response = self.session.post(
                self.url,
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise KimiJudgeError(f"Kimi 请求失败：{exc}") from exc
        if response.status_code != 200:
            raise KimiJudgeError(f"Kimi HTTP {response.status_code}：{response.text[:300]}")
        try:
            message = response.json()["choices"][0]["message"]
            content = message["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            raw = extract_json_object(content)
            return {name: _convert_answer(question, raw.get(name)) for name, question in questions.items()}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            if isinstance(exc, KimiJudgeError):
                raise
            raise KimiJudgeError(f"Kimi 响应格式错误：{exc}") from exc
