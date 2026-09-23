import json
import sqlite3
import threading
from datetime import datetime

import requests

from jevq.config import JEV_MODEL, JEV_TIMEOUT, JEV_URL

RETRYABLE = {429, 529}
_LOG_LOCK = threading.Lock()


class JevError(Exception):
    pass


class JevClient:
    def __init__(self, api_key: str, session=None, conn: sqlite3.Connection | None = None):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.conn = conn

    def ask(self, state, questions: dict) -> dict:
        body = {"state": state, "model": JEV_MODEL, "questions": questions}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        error = "unknown"
        for _ in range(2):
            try:
                r = self.session.post(JEV_URL, json=body, headers=headers, timeout=JEV_TIMEOUT)
            except requests.RequestException as e:
                error = repr(e)
                continue
            if r.status_code == 200:
                answers = r.json()["answers"]
                self._log(body, answers, None)
                return answers
            error = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in RETRYABLE and r.status_code < 500:
                break
        self._log(body, None, error)
        raise JevError(error)

    def _log(self, body, answers, error):
        if self.conn is None:
            return
        with _LOG_LOCK:
            self.conn.execute("INSERT INTO jev_calls(ts, request, response, error) VALUES (?, ?, ?, ?)",
                              (datetime.now().isoformat(timespec="seconds"),
                               json.dumps(body, ensure_ascii=False),
                               None if answers is None else json.dumps(answers, ensure_ascii=False),
                               error))
            self.conn.commit()


def score_label(answer: dict) -> str:
    probs = answer["probabilities"]
    return answer["legend"][max(probs, key=probs.get)]
