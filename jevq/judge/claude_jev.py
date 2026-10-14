"""和 JevClient 一样的接口（ask(state, questions) → answers），后面接的是本机 claude 命令行（订阅登录）。

用户 2026-10-14 定：大盘情绪、板块情绪直接改由 Claude 判。现有判断器（MarketJudge、OwnSectorJudge.strength）
只换客户端，不用改。支持 Jev 的两种题型：score（各档概率）、choice（选一项）；答案转成 Jev 的格式
（legend 下标 → 档位名，probabilities 下标 → 概率），score_label / score_probs 照用。
Claude 没答上（超时、额度用完、答得不对）→ 有 fallback（Jev）就退回问 Jev，交易不断；last_source 记是谁答的。
"""
import json
from datetime import datetime

from jevq import config
from jevq.judge.jev import _LOG_LOCK, JevError

SYSTEM = ("你是 A 股短线交易的分析员，只根据给你的数据回答问题。"
          "只输出一个 JSON 对象，不要任何别的文字。")


def _question_text(name: str, q: dict) -> str:
    if q["type"] == "score":
        levels = "、".join(q["criteria"])
        return (f"- {name}：{q['instructions']}\n  在这几档上给出概率（加起来为 1）：{levels}\n"
                f"  输出 \"{name}\": {{" + ", ".join(f'"{lv}": 概率' for lv in q["criteria"]) + "}")
    opts = "；".join(f"{k}（{v}）" for k, v in q["criteria"].items())
    return (f"- {name}：{q['instructions']}\n  从下面选一个：{opts}\n"
            f"  输出 \"{name}\": {{\"choice\": \"选项名\", \"confidence\": 0到1}}")


def _convert(q: dict, raw) -> dict:
    """Claude 的回答 → Jev 的答案格式；不合格抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("回答不是对象")
    if q["type"] == "score":
        levels = list(q["criteria"])
        extra = set(raw) - set(levels)
        if extra or not raw:
            raise ValueError(f"档位不对：{sorted(extra) or '空'}")
        vals = [max(0.0, float(raw.get(lv, 0) or 0)) for lv in levels]
        total = sum(vals)
        if total <= 0:
            raise ValueError("概率全为 0")
        probs = {str(i): round(v / total, 4) for i, v in enumerate(vals)}
        top = max(probs.values())
        return {"type": "score", "legend": {str(i): lv for i, lv in enumerate(levels)}, "probabilities": probs,
                "confidence": top, "score": top}
    choice = raw.get("choice")
    if choice not in q["criteria"]:
        raise ValueError(f"选项不对：{choice!r}")
    conf = raw.get("confidence")
    return {"type": "choice", "choice": choice, "confidence": None if conf is None else float(conf)}


class ClaudeJev:
    def __init__(self, cli, conn=None, fallback=None, timeout: float = config.CLAUDE_JUDGE_TIMEOUT):
        self.cli, self.conn, self.fallback, self.timeout = cli, conn, fallback, timeout
        self.last_source: str | None = None

    def ask(self, state, questions: dict, timeout: float | None = None, deadline: float | None = None) -> dict:
        user = ("数据：\n" + json.dumps(state, ensure_ascii=False, indent=1) + "\n\n问题：\n"
                + "\n".join(_question_text(n, q) for n, q in questions.items())
                + "\n\n把上面各题的输出合成一个 JSON 对象。")
        got = self.cli.ask_json(SYSTEM, user, timeout=min(self.timeout, timeout or self.timeout))
        error = None
        answers = None
        if got is None:
            error = "Claude 没有答上"
        else:
            try:
                answers = {n: _convert(q, got.get(n)) for n, q in questions.items()}
            except (ValueError, TypeError) as e:
                error = f"Claude 回答格式不对：{e}"
        self._log(state, questions, answers, error)
        if answers is not None:
            self.last_source = "claude"
            return answers
        if self.fallback is not None:
            print(f"[claude] {error}，这次退回 Jev")
            self.last_source = "jev"
            return self.fallback.ask(state, questions, timeout=timeout, deadline=deadline)
        self.last_source = None
        raise JevError(error)

    def _log(self, state, questions, answers, error):
        if self.conn is None:
            return
        body = {"state": state, "model": f"claude-{config.CLAUDE_MODEL}", "questions": questions,
                "cli": getattr(self.cli, "last_trace", None)}
        with _LOG_LOCK:
            self.conn.execute("INSERT INTO jev_calls(ts, request, response, error) VALUES (?, ?, ?, ?)",
                              (datetime.now().isoformat(timespec="seconds"), json.dumps(body, ensure_ascii=False),
                               None if answers is None else json.dumps(answers, ensure_ascii=False), error))
            self.conn.commit()
