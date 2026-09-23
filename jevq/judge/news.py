import json
import sqlite3
from datetime import datetime, timedelta

from jevq.config import NEWS_LOOKBACK_HOURS
from jevq.data.akdata import fetch_news
from jevq.judge.jev import JevError, score_label, score_probs

LEVELS = ["偏空", "中性", "偏多"]


class NewsJudge:
    def __init__(self, conn: sqlite3.Connection, jev, fetch=fetch_news):
        self.conn, self.jev, self.fetch = conn, jev, fetch
        self.last_trace: dict | None = None

    def check(self, code: str, name: str, now: datetime) -> tuple[str, str]:
        day = now.date().isoformat()
        row = self.conn.execute("SELECT value FROM day_cache WHERE date=? AND kind='news' AND key=?",
                                (day, code)).fetchone()
        if row:
            v = row[0]
            trace = json.loads(v) if v.startswith("{") else {"label": v, "news": [], "probabilities": None,
                                                              "confidence": None}
        else:
            trace = self._judge(code, name, now)
            if trace is None:
                self.last_trace = None
                return "error", "消息面判断失败"
            self.conn.execute("INSERT OR REPLACE INTO day_cache VALUES (?, 'news', ?, ?)",
                              (day, code, json.dumps(trace, ensure_ascii=False)))
            self.conn.commit()
        self.last_trace = trace
        label = trace["label"]
        return ("reject" if label == "偏空" else "pass"), f"消息面{label}"

    def _judge(self, code: str, name: str, now: datetime) -> dict | None:
        try:
            items = [n for n in self.fetch(code)
                     if now - n.published <= timedelta(hours=NEWS_LOOKBACK_HOURS)]
        except Exception:
            return None
        if not items:
            return {"label": "中性", "news": [], "probabilities": None, "confidence": None}
        lines = [f"{n.title}｜{n.content[:100]}" for n in items[:10]]
        try:
            ans = self.jev.ask({"股票": f"{name}({code})", "近24小时新闻": lines}, {"sentiment": {
                "type": "score",
                "instructions": "这些新闻对该股票未来一两个交易日股价的影响偏向哪一边？",
                "criteria": LEVELS}})["sentiment"]
        except JevError:
            return None
        return {"label": score_label(ans), "news": lines, "probabilities": score_probs(ans),
                "confidence": ans.get("confidence")}
