from datetime import datetime

import pytest

from dashboard.decision_journal import DecisionJournal, build_questions
from dashboard.kimi_judge import KimiJudge, KimiJudgeError, chat_completions_url, extract_json_object


class Response:
    def __init__(self, payload, status=200, text=""):
        self.payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        return self.payload


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def answers_content():
    return {"regime": {"choice": "修复观察", "confidence": 0.72},
            "action": {"choice": "仅观察候选", "confidence": 0.81}}


def journal_context():
    return {
        "snapshot": {"phase": "盘中"},
        "marketBreadth": {"complete": True, "stale": False, "up": 3000, "down": 2000, "upRatio": 55.0,
                          "total": 5500, "universe": 5577, "limitUp": 50, "limitDown": 10},
        "sectorBreadth": {"complete": True, "stale": False, "up": 30, "down": 19,
                          "leaders": [{"name": "机器人", "change": 2.3}]},
    }


def test_kimi_endpoint_normalization():
    assert chat_completions_url("https://agent-gw.kimi.com/coding/") == \
        "https://agent-gw.kimi.com/coding/v1/chat/completions"
    assert chat_completions_url("https://example.test/v1") == "https://example.test/v1/chat/completions"
    assert chat_completions_url("https://example.test/v1/chat/completions") == \
        "https://example.test/v1/chat/completions"


def test_kimi_request_and_choice_answers():
    session = Session(Response({"choices": [{"message": {"content": __import__("json").dumps(answers_content())}}]}))
    judge = KimiJudge("secret", "https://example.test/coding", session=session)
    result = judge.ask(journal_context(), build_questions())
    assert result["regime"] == {"type": "choice", "choice": "修复观察", "confidence": 0.72}
    assert judge.model_name == "kimi-for-coding"
    url, request = session.calls[0]
    assert url == "https://example.test/coding/v1/chat/completions"
    assert request["json"]["model"] == "kimi-for-coding"
    assert request["json"]["response_format"] == {"type": "json_object"}
    assert request["headers"]["Authorization"] == "Bearer secret"
    assert "扫首板、超跌、低吸" in request["json"]["messages"][0]["content"]


def test_kimi_accepts_fenced_json_and_content_parts():
    raw = "```json\n" + __import__("json").dumps(answers_content(), ensure_ascii=False) + "\n```"
    assert extract_json_object(raw)["regime"]["choice"] == "修复观察"
    session = Session(Response({"choices": [{"message": {"content": [{"type": "text", "text": raw}]}}]}))
    result = KimiJudge("secret", session=session).ask(journal_context(), build_questions())
    assert result["action"]["choice"] == "仅观察候选"


@pytest.mark.parametrize("content", ["", "不是 JSON", "[]", '{"regime":{"choice":"追涨"},"action":{"choice":"仅观察候选"}}'])
def test_invalid_kimi_response_never_creates_a_decision(tmp_path, content):
    session = Session(Response({"choices": [{"message": {"content": content}}]}))
    journal = DecisionJournal(tmp_path / "ai.jsonl", judge=KimiJudge("secret", session=session))
    journal.update_now(journal_context())
    assert journal.records() == []
    assert journal.status()["error"]


def test_kimi_http_failure_is_explicit(tmp_path):
    session = Session(Response({}, status=451, text="region unavailable"))
    journal = DecisionJournal(tmp_path / "ai.jsonl", judge=KimiJudge("secret", session=session))
    journal.update_now(journal_context())
    assert journal.records() == []
    assert "HTTP 451" in journal.status()["error"]


def test_journal_persists_actual_kimi_model_name(tmp_path):
    session = Session(Response({"choices": [{"message": {"content": __import__("json").dumps(answers_content())}}]}))
    judge = KimiJudge("secret", model="kimi-test-model", session=session)
    journal = DecisionJournal(tmp_path / "ai.jsonl", judge=judge, clock=lambda: datetime(2026, 10, 27, 13, 30))
    journal.update_now(journal_context())
    assert journal.records()[0]["model"] == "kimi-test-model"
