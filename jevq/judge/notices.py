import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from jevq.config import LOW_CONFIDENCE
from jevq.data.notice_store import notices_for
from jevq.judge.jev import JevError

MINE_LABELS = {"新增减持", "新增质押", "回购", "诉讼仲裁"}

CRITERIA = {
    "新增减持": "股东、董监高的减持计划、减持进展、被动减持、权益变动导致持股减少",
    "新增质押": "股东新增股份质押、补充质押、质押展期",
    "回购": "公司股份回购方案、回购进展、回购结果",
    "诉讼仲裁": "公司涉及诉讼、仲裁、被起诉、判决、立案调查",
    "亏损": "业绩预亏、净利润亏损、业绩预告下修",
    "解除质押或减持完成": "解除质押、减持计划实施完毕、减持期限届满、终止减持",
    "其他": "与以上各类都无关的公告",
}

MINE_KEYWORDS = {"新增减持": ["减持"], "新增质押": ["质押"], "回购": ["回购"],
                 "诉讼仲裁": ["诉讼", "仲裁", "起诉"]}
SAFE_KEYWORDS = ["解除", "解押", "实施完毕", "期限届满", "终止", "完成"]
NEGATION_KEYWORDS = ["不减持", "不存在减持", "承诺不", "不质押", "暂不", "无减持", "不再减持"]


def classify_by_keyword(title: str) -> str | None:
    if any(k in title for k in SAFE_KEYWORDS) or any(k in title for k in NEGATION_KEYWORDS):
        return None
    for label, words in MINE_KEYWORDS.items():
        if any(w in title for w in words):
            return label
    return None


class NoticeJudge:
    def __init__(self, conn: sqlite3.Connection, jev, workers: int = 8):
        self.conn, self.jev, self.workers = conn, jev, workers
        self.last_trace: list[dict] = []

    def check(self, code: str, today: date) -> tuple[str, str]:
        notices = notices_for(self.conn, code, today)
        verdicts: dict[str, tuple[str, float, str, dict | None]] = {}
        pending = []
        for n in notices:
            cached = self.conn.execute(
                "SELECT label, confidence, source, probs FROM notice_verdicts WHERE id=?", (n.id,)).fetchone()
            if cached:
                label, conf, source, probs = cached
                if source == "keyword":
                    fresh = classify_by_keyword(n.title)
                    if fresh is None:
                        pending.append(n)
                        continue
                    if fresh != label:
                        label = fresh
                        self._save(n.id, label, 1.0, "keyword", None)
                verdicts[n.id] = (label, conf, source, json.loads(probs) if probs else None)
            elif (label := classify_by_keyword(n.title)) is not None:
                verdicts[n.id] = (label, 1.0, "keyword", None)
                self._save(n.id, label, 1.0, "keyword", None)
            else:
                pending.append(n)

        failed = False
        if pending:
            with ThreadPoolExecutor(self.workers) as pool:
                results = list(pool.map(self._ask, pending))
            for n, result in zip(pending, results):
                if result is None:
                    failed = True
                    continue
                label, conf, probs = result
                verdicts[n.id] = (label, conf, "jev", probs)
                self._save(n.id, label, conf, "jev", probs)

        self.last_trace = []
        first_bad = None
        for n in notices:
            if n.id not in verdicts:
                continue
            label, conf, source, probs = verdicts[n.id]
            mine, unsure = label in MINE_LABELS, conf < LOW_CONFIDENCE
            self.last_trace.append({"date": n.date, "title": n.title, "label": label, "confidence": conf,
                                    "source": source, "probabilities": probs, "mine": mine, "unsure": unsure})
            if first_bad is None and (mine or unsure):
                first_bad = (n, label, conf, mine)
        if failed:
            return "error", "Jev 不可用"
        if first_bad:
            n, label, conf, mine = first_bad
            if mine:
                return "reject", f"{label}：{n.date} {n.title}"
            return "reject", f"拿不准({conf:.2f})：{n.date} {n.title}"
        return "pass", "公告无雷"

    def _ask(self, n) -> tuple[str, float, dict] | None:
        try:
            ans = self.jev.ask({"公告标题": n.title, "公告类型": n.type},
                               {"kind": {"type": "choice",
                                         "instructions": "这条A股上市公司公告属于哪一类？",
                                         "criteria": CRITERIA}})["kind"]
        except JevError:
            return None
        return ans["choice"], ans["confidence"], ans["probabilities"]

    def _save(self, nid: str, label: str, conf: float, source: str, probs: dict | None):
        self.conn.execute(
            "INSERT OR REPLACE INTO notice_verdicts(id, label, confidence, source, probs) VALUES (?, ?, ?, ?, ?)",
            (nid, label, conf, source, None if probs is None else json.dumps(probs, ensure_ascii=False)))
        self.conn.commit()
