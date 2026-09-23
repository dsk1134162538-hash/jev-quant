import sqlite3
from datetime import datetime, timedelta

from jevq.config import NEWS_LOOKBACK_HOURS
from jevq.data.akdata import fetch_news
from jevq.judge.jev import JevError, score_label

LEVELS = ["偏空", "中性", "偏多"]


class NewsJudge:
    def __init__(self, conn: sqlite3.Connection, jev, fetch=fetch_news):
        self.conn, self.jev, self.fetch = conn, jev, fetch

    def check(self, code: str, name: str, now: datetime) -> tuple[str, str]:
        day = now.date().isoformat()
        row = self.conn.execute("SELECT value FROM day_cache WHERE date=? AND kind='news' AND key=?",
                                (day, code)).fetchone()
        label = row[0] if row else self._judge(code, name, now)
        if label is None:
            return "error", "消息面判断失败"
        if not row:
            self.conn.execute("INSERT OR REPLACE INTO day_cache VALUES (?, 'news', ?, ?)", (day, code, label))
            self.conn.commit()
        return ("reject" if label == "偏空" else "pass"), f"消息面{label}"

    def _judge(self, code: str, name: str, now: datetime) -> str | None:
        try:
            items = [n for n in self.fetch(code)
                     if now - n.published <= timedelta(hours=NEWS_LOOKBACK_HOURS)]
        except Exception:
            return None
        if not items:
            return "中性"
        state = {"股票": f"{name}({code})",
                 "近24小时新闻": [f"{n.title}｜{n.content[:100]}" for n in items[:10]]}
        try:
            ans = self.jev.ask(state, {"sentiment": {
                "type": "score",
                "instructions": "这些新闻对该股票未来一两个交易日股价的影响偏向哪一边？",
                "criteria": LEVELS}})["sentiment"]
        except JevError:
            return None
        return score_label(ans)
