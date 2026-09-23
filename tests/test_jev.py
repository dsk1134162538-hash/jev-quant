import json

import pytest
import requests

from jevq import db
from jevq.judge.jev import JevClient, JevError, score_label


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


OK = Resp(200, {"model": "jev-1.13.0", "answers": {"q": {"type": "noul", "noul": 0.9}}})
Q = {"q": {"type": "noul", "instructions": "?"}}


def test_ask_sends_body_and_auth():
    s = FakeSession(OK)
    assert JevClient("k", session=s).ask("state", Q) == {"q": {"type": "noul", "noul": 0.9}}
    url, body, headers = s.calls[0]
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert body == {"state": "state", "model": "jev-latest", "questions": Q}
    assert headers["Authorization"] == "Bearer k"


@pytest.mark.parametrize("first", [Resp(529, {}), Resp(429, {}), requests.Timeout("t")])
def test_retries_once(first):
    s = FakeSession(first, OK)
    assert JevClient("k", session=s).ask("s", Q)["q"]["noul"] == 0.9
    assert len(s.calls) == 2


def test_gives_up_after_two_failures():
    with pytest.raises(JevError):
        JevClient("k", session=FakeSession(Resp(529, {}), Resp(529, {}))).ask("s", Q)


def test_no_retry_on_auth_error():
    s = FakeSession(Resp(401, {"error": "bad key"}), OK)
    with pytest.raises(JevError):
        JevClient("k", session=s).ask("s", Q)
    assert len(s.calls) == 1


def test_logs_calls():
    conn = db.connect(":memory:")
    JevClient("k", session=FakeSession(OK), conn=conn).ask("s", Q)
    req, resp, err = conn.execute("SELECT request, response, error FROM jev_calls").fetchone()
    assert json.loads(req)["state"] == "s" and "noul" in resp and err is None


def test_score_label():
    ans = {"type": "score", "score": 1.05, "legend": {"0": "偏空", "1": "中性", "2": "偏多"},
           "probabilities": {"0": 0.0, "1": 0.95, "2": 0.05}, "confidence": 0.92}
    assert score_label(ans) == "中性"


class BadJson(Resp):
    def json(self):
        raise ValueError("not json")


@pytest.mark.parametrize("bad", [Resp(200, {}), Resp(200, None), BadJson(200, {})])
def test_malformed_200_retried_then_error(bad):
    s = FakeSession(bad, bad)
    with pytest.raises(JevError, match="响应格式错误"):
        JevClient("k", session=s).ask("s", Q)
    assert len(s.calls) == 2


def test_malformed_200_then_ok():
    s = FakeSession(Resp(200, {}), OK)
    assert JevClient("k", session=s).ask("s", Q)["q"]["noul"] == 0.9
    assert len(s.calls) == 2


def test_malformed_200_logged_as_error():
    conn = db.connect(":memory:")
    with pytest.raises(JevError):
        JevClient("k", session=FakeSession(Resp(200, {}), Resp(200, {})), conn=conn).ask("s", Q)
    (err,) = conn.execute("SELECT error FROM jev_calls").fetchone()
    assert "响应格式错误" in err
