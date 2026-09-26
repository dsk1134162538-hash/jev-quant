"""公告排雷（两段式）：标题粗筛疑似 → 读正文让 Jev 按轻重打风险分 → 过线才否决。
设计：docs/specs/2026-09-26-notice-risk-design.md"""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from time import monotonic

from jevq.config import RISK_BODY_BUDGET, RISK_JEV_BUDGET, RISK_JEV_TIMEOUT, RISK_MAX_PER_CHECK, RISK_VERSION
from jevq.data.notice_body import fetch_body as default_fetch_body
from jevq.data.notice_store import notices_for
from jevq.judge.jev import JevError
from jevq.judge.notice_risk import (LEVELS, RISK_CATEGORIES, classify_score, keyword_category, risk_question,
                                    risk_score, risk_state, title_criteria)

TITLE_SOURCE = "jev2"      # 新分类体系的标题分类缓存；旧的 keyword/jev 行不再使用


class NoticeJudge:
    def __init__(self, conn: sqlite3.Connection, jev, workers: int = 8, fetch_body=default_fetch_body,
                 body_budget: float = RISK_BODY_BUDGET, jev_budget: float = RISK_JEV_BUDGET):
        self.conn, self.jev, self.workers, self.fetch_body = conn, jev, workers, fetch_body
        self.body_budget, self.jev_budget = body_budget, jev_budget
        self.last_trace: dict = self._empty_trace()
        # 超过时限才回来的正文：后台线程只往这里放，下次 check 在调用线程里落库（sqlite 连接不跨线程）
        self._late_bodies: dict[str, str] = {}
        self._late_lock = threading.Lock()

    @staticmethod
    def _empty_trace() -> dict:
        return {"suspects": [], "others": 0, "skipped": []}

    def check(self, code: str, today: date) -> tuple[str, str]:
        # 中途抛异常时，调用方读到的也不会是上一只票的；并标上 incomplete，面板不会把它显示成「无疑似雷」
        self.last_trace = {**self._empty_trace(), "incomplete": True}
        notices = notices_for(self.conn, code, today)
        failed = False

        # 第一段：挑疑似
        suspects, others, need_title = [], 0, []
        for n in notices:
            cat = keyword_category(n.title)
            if cat:
                suspects.append((n, cat, "keyword"))
                continue
            row = self.conn.execute("SELECT label FROM notice_verdicts WHERE id=? AND source=?",
                                    (n.id, TITLE_SOURCE)).fetchone()
            if row:
                if row[0] in RISK_CATEGORIES:
                    suspects.append((n, row[0], "jev_title"))
                else:
                    others += 1
            else:
                need_title.append(n)
        if need_title:
            # 标题分类同样受 jev_budget 总时限管：到点还没问的直接算失败（→ error，当日不买），
            # 问到的照常缓存。与第二段一样用 deadline 压住单次请求超时，线程不会活过 check。
            deadline = monotonic() + self.jev_budget
            with ThreadPoolExecutor(self.workers) as pool:
                results = list(pool.map(lambda n: self._ask_title(n, deadline), need_title))
            for n, res in zip(need_title, results):
                if res is None:
                    failed = True
                    continue
                label, conf, probs = res
                self._save_title(n.id, label, conf, probs)
                if label in RISK_CATEGORIES:
                    suspects.append((n, label, "jev_title"))
                else:
                    others += 1
        suspects.sort(key=lambda s: (s[0].date, s[0].id), reverse=True)
        judged, skipped = suspects[:RISK_MAX_PER_CHECK], suspects[RISK_MAX_PER_CHECK:]

        # 第二段：细判（缓存 → 正文 → Jev）
        self._flush_late_bodies()
        items, pending = [], []
        for n, cat, source in judged:
            row = self.conn.execute("SELECT level, score, probs, body_read FROM notice_risk "
                                    "WHERE id=? AND version=?", (n.id, RISK_VERSION)).fetchone()
            if row:
                level, score, probs, body_read = row
                items.append(self._item(n, cat, source, level, score, json.loads(probs or "{}"),
                                        bool(body_read), self._cached_body(n.id) if body_read else None,
                                        today))
            else:
                pending.append((n, cat, source))
        # 第一段已失败时结论必是 error：不再抓正文、不再细判，别在交易 tick 里白等
        if pending and not failed:
            bodies, transient = self._bodies([n.id for n, _, _ in pending])
            # 细判 Jev 总时限：慢 Jev 下一次 check 最多再等 jev_budget 秒；到点还没问的直接算失败。
            # 用 deadline 压住每次请求的超时（而不是丢下线程先走），工作线程不会在 check 返回后
            # 还往共享的 sqlite 连接里写 jev_calls 日志。
            deadline = monotonic() + self.jev_budget
            with ThreadPoolExecutor(self.workers) as pool:
                answers = list(pool.map(
                    lambda p: self._ask_risk(p[0], p[1], bodies.get(p[0].id), today, deadline), pending))
            for (n, cat, source), ans in zip(pending, answers):
                if ans is None:
                    failed = True
                    continue
                level, probs = ans
                score = risk_score(probs)
                body = bodies.get(n.id)
                if n.id not in transient:     # 正文超时/抓取异常只按标题判这一次，不落缓存，下次带正文重判
                    self._save_risk(n.id, cat, level, score, probs, body is not None)
                items.append(self._item(n, cat, source, level, score, probs, body is not None, body, today))

        items.sort(key=lambda it: it["score"], reverse=True)
        self.last_trace = {"suspects": items, "others": others,
                           "skipped": [{"date": n.date, "title": n.title, "category": cat} for n, cat, _ in skipped]}
        if failed:
            self.last_trace["incomplete"] = True      # Jev 中途失败：疑似/其余只是已判完的那部分
            return "error", "Jev 不可用"
        if not items:
            return "pass", "公告无雷"
        top = items[0]
        if top["verdict"] == "reject":
            return "reject", f"{top['category']}·高风险 {top['score']:.2f}：{top['date']} {top['title']}"
        if top["verdict"] == "unsure":
            return "reject", f"{top['category']}·拿不准 {top['score']:.2f}：{top['date']} {top['title']}"
        return "pass", f"疑似 {len(items)} 条，均判低风险"

    # ---- 正文：缓存在调用线程查；缺的并发抓，总共最多等 body_budget 秒 ----

    def _bodies(self, ids: list[str]) -> tuple[dict[str, str | None], set[str]]:
        """返回 (正文, 暂时没拿到的 id)。抓回 None 视为「确实没有正文」；超时或异常视为暂时没拿到。"""
        bodies: dict[str, str | None] = {}
        todo = []
        for nid in ids:
            text = self._cached_body(nid)
            if text:
                bodies[nid] = text
            else:
                todo.append(nid)
        if not todo:
            return bodies, set()
        transient = set(todo)
        pool = ThreadPoolExecutor(self.workers)
        futures = {pool.submit(self.fetch_body, nid): nid for nid in todo}
        try:
            for fut in as_completed(futures, timeout=self.body_budget):
                nid = futures[fut]
                try:
                    text = fut.result() or None
                except Exception:
                    continue
                transient.discard(nid)
                bodies[nid] = text
                if text:
                    self._save_body(nid, text)
        except TimeoutError:
            pass
        finally:
            for fut, nid in futures.items():
                if nid in transient:          # 已完成的回调会立即执行，不会漏掉刚好卡在时限上回来的
                    fut.add_done_callback(lambda f, nid=nid: self._stash_late(nid, f))
            pool.shutdown(wait=False, cancel_futures=True)
        return bodies, transient

    def _stash_late(self, nid: str, fut):
        if fut.cancelled() or fut.exception() is not None:
            return
        text = fut.result()
        if text:
            with self._late_lock:
                self._late_bodies[nid] = text

    def _flush_late_bodies(self):
        with self._late_lock:
            late, self._late_bodies = self._late_bodies, {}
        for nid, text in late.items():
            self._save_body(nid, text)

    def _cached_body(self, nid: str) -> str | None:
        row = self.conn.execute("SELECT text FROM notice_bodies WHERE id=?", (nid,)).fetchone()
        return row[0] if row else None

    def _save_body(self, nid: str, text: str):
        self.conn.execute("INSERT OR REPLACE INTO notice_bodies(id, text, fetched) VALUES (?, ?, ?)",
                          (nid, text, datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    @staticmethod
    def _item(n, cat, source, level, score, probs, body_read, body, today) -> dict:
        return {"id": n.id, "date": n.date, "title": n.title,
                "days_ago": (today - date.fromisoformat(n.date)).days, "category": cat, "source": source,
                "level": level, "score": round(score, 4), "probs": probs, "body_read": body_read,
                "excerpt": (body or "")[:200], "verdict": classify_score(score)}

    def _ask_title(self, n, deadline):
        if monotonic() >= deadline:
            return None
        try:
            ans = self.jev.ask({"公告标题": n.title, "公告类型": n.type},
                               {"kind": {"type": "choice", "instructions": "这条A股上市公司公告属于哪一类？",
                                         "criteria": title_criteria()}}, deadline=deadline)["kind"]
            label = ans["choice"]
        except (JevError, KeyError, TypeError):
            return None
        return label, ans.get("confidence"), ans.get("probabilities")

    def _ask_risk(self, n, cat, body, today, deadline):
        if monotonic() >= deadline:
            return None
        try:
            ans = self.jev.ask(risk_state(n, cat, body, today), risk_question(cat),
                               timeout=RISK_JEV_TIMEOUT, deadline=deadline)["risk"]
            choice = ans["choice"]
        except (JevError, KeyError, TypeError):
            return None
        probs = ans.get("probabilities") or {}
        if not any(k in LEVELS for k in probs):     # 没给概率：按所选档位算满分，不能按 0 分放行
            if choice not in LEVELS:
                return None
            probs = {choice: 1.0}
        return choice, probs

    def _save_title(self, nid, label, conf, probs):
        self.conn.execute("INSERT OR REPLACE INTO notice_verdicts(id, label, confidence, source, probs) "
                          "VALUES (?, ?, ?, ?, ?)",
                          (nid, label, conf, TITLE_SOURCE,
                           None if probs is None else json.dumps(probs, ensure_ascii=False)))
        self.conn.commit()

    def _save_risk(self, nid, cat, level, score, probs, body_read):
        self.conn.execute("INSERT OR REPLACE INTO notice_risk(id, version, category, level, score, probs, "
                          "body_read, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (nid, RISK_VERSION, cat, level, score, json.dumps(probs, ensure_ascii=False),
                           int(body_read), datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()
