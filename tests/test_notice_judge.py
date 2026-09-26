"""公告两段式细判（2026-09-26 公告两段式细判：语义更新）。
旧语义「标题关键词/Jev 分类沾一类就否决」已换成「标题粗筛疑似 → 读正文让 Jev 打风险分 → 过线才否决」；
旧测试的意图逐条保留在下面（见各测试 docstring）。"""
import json
import threading
import time
from datetime import date

from jevq import db
from jevq.config import RISK_VERSION
from jevq.judge.jev import JevError
from jevq.judge.notices import NoticeJudge

TODAY = date(2026, 9, 26)


class FakeJev:
    """title_labels：标题 → 第一段分类；risk：标题 → 第二段四档概率。"""
    def __init__(self, title_labels=None, risk=None, fail_titles=(), fail_risk=()):
        self.title_labels, self.risk = title_labels or {}, risk or {}
        self.fail_titles, self.fail_risk = set(fail_titles), set(fail_risk)
        self.title_calls, self.risk_calls, self.states, self.timeouts = 0, 0, [], []
        self._lock = threading.Lock()

    def ask(self, state, questions, timeout=None):
        title = state["公告标题"]
        with self._lock:
            if "kind" in questions:
                self.title_calls += 1
            else:
                self.risk_calls += 1
                self.states.append(state)
                self.timeouts.append(timeout)
        if "kind" in questions:
            if title in self.fail_titles:
                raise JevError("down")
            label = self.title_labels.get(title, "其他")
            return {"kind": {"choice": label, "probabilities": {label: 0.9}, "confidence": 0.9}}
        if title in self.fail_risk:
            raise JevError("down")
        probs = self.risk[title]
        return {"risk": {"choice": max(probs, key=probs.get), "probabilities": probs,
                         "confidence": max(probs.values())}}


def setup(*rows):
    """rows: (title, date) 或 title（默认 2026-09-20）。"""
    conn = db.connect(":memory:")
    items = [r if isinstance(r, tuple) else (r, "2026-09-20") for r in rows]
    conn.executemany("INSERT INTO notices VALUES (?, ?, ?, ?, ?)",
                     [(f"AN{i}", "600001", t, "", d) for i, (t, d) in enumerate(items)])
    conn.commit()
    return conn


def judge(conn, jev, bodies=None, **kw):
    bodies = bodies if bodies is not None else {}
    return NoticeJudge(conn, jev, fetch_body=lambda aid: bodies.get(aid), **kw)


def test_no_notices_passes():
    conn = setup()
    j = judge(conn, FakeJev())
    assert j.check("600001", TODAY) == ("pass", "公告无雷")
    assert j.last_trace == {"suspects": [], "others": 0, "skipped": []}


def test_promise_not_to_reduce_is_read_and_passes():
    """旧 test_stale_keyword_cache_is_rejudged_when_negation_added 的意图：承诺不减持不能被当成减持否决。"""
    t = "先导基电:关于控股股东自愿承诺未来12个月不减持公司股份的公告"
    conn = setup(t)
    jev = FakeJev(risk={t: {"无": 0.9, "低": 0.1}})
    j = judge(conn, jev, {"AN0": "控股股东承诺自即日起12个月内不减持"})
    verdict, detail = j.check("600001", TODAY)
    assert verdict == "pass" and "均判低风险" in detail
    s = j.last_trace["suspects"][0]
    assert s["category"] == "减持" and s["source"] == "keyword" and s["body_read"] is True
    assert jev.states[0]["正文摘要"].startswith("控股股东承诺")


def test_keyword_suspect_high_risk_rejects_without_title_call():
    """旧 test_keyword_mine_rejects_without_jev：减持公告 + 高风险正文 → 否决，且标题命中关键词不再问第一段。"""
    t = "甲:关于股东减持计划的公告"
    conn = setup(t)
    jev = FakeJev(risk={t: {"高": 0.9, "中": 0.1}})
    verdict, why = judge(conn, jev, {"AN0": "控股股东拟减持不超过3%"}).check("600001", TODAY)
    assert verdict == "reject" and "减持" in why and jev.title_calls == 0
    assert jev.timeouts == [15.0]                    # 带正文的调用用 RISK_JEV_TIMEOUT


def test_buyback_progress_passes_and_termination_rejects():
    ok, bad = "甲:关于回购公司股份的进展公告", "甲:关于终止回购公司股份的公告"
    conn = setup(ok, bad)
    jev = FakeJev(risk={ok: {"低": 0.9, "无": 0.1}, bad: {"高": 0.8, "中": 0.2}})
    verdict, detail = judge(conn, jev).check("600001", TODAY)
    assert verdict == "reject" and detail.startswith("回购·高风险 0.92：") and "终止回购" in detail


def test_restructuring_in_progress_rejects_as_high():
    t = "甲:关于筹划重大资产重组的进展公告"
    conn = setup(t)
    verdict, detail = judge(conn, FakeJev(risk={t: {"中": 1.0}})).check("600001", TODAY)
    assert verdict == "reject" and detail.startswith("重组·高风险 0.60")


def test_unsure_band_rejects_with_label():
    """旧 test_low_confidence_counts_as_mine 的意图：拿不准也否决，理由里写「拿不准」。"""
    t = "甲:关于股东减持计划的公告"
    conn = setup(t)
    verdict, detail = judge(conn, FakeJev(risk={t: {"中": 0.7, "无": 0.3}})).check("600001", TODAY)
    assert verdict == "reject" and detail.startswith("减持·拿不准 0.42：")


def test_boundaries():
    for probs, verdict in [({"低": 1.0, "中": 0.2475}, "pass"),     # 0.2 + 0.1485 = 0.3485
                           ({"中": 0.5, "低": 0.25}, "reject"),      # 0.35 拿不准
                           ({"中": 0.5, "低": 1.0}, "reject")]:      # 0.5 高风险
        t = "甲:关于股东减持计划的公告"
        conn = setup(t)
        assert judge(conn, FakeJev(risk={t: probs})).check("600001", TODAY)[0] == verdict


def test_body_missing_falls_back_to_title():
    t = "甲:关于股东减持计划的公告"
    conn = setup(t)
    jev = FakeJev(risk={t: {"低": 1.0}})
    j = judge(conn, jev, {})
    j.check("600001", TODAY)
    assert jev.states[0]["正文摘要"] == "未读到正文，仅凭标题判断"
    assert j.last_trace["suspects"][0]["body_read"] is False


def test_jev_title_classification_marks_suspect_and_caches_jev2():
    """旧 test_jev_classifies_ambiguous_and_caches 的意图：标题没命中关键词的交给 Jev 分类，结果缓存。"""
    t = "甲:关于控股股东协议转让部分股份的公告"
    conn = setup(t)
    jev = FakeJev(title_labels={t: "减持"}, risk={t: {"中": 0.2, "低": 0.8}})
    j = judge(conn, jev)
    assert j.check("600001", TODAY)[0] == "pass"
    assert j.last_trace["suspects"][0]["source"] == "jev_title"
    row = conn.execute("SELECT label, source FROM notice_verdicts WHERE id='AN0'").fetchone()
    assert row == ("减持", "jev2")
    j.check("600001", TODAY)
    assert jev.title_calls == 1                     # 标题分类走缓存


def test_old_verdict_cache_is_ignored():
    t = "甲:关于限售股份上市流通的提示性公告"      # 关键词命中「上市流通」
    t2 = "甲:董事会决议公告"
    conn = setup(t, t2)
    conn.execute("INSERT INTO notice_verdicts(id, label, confidence, source) VALUES ('AN1', '其他', 0.9, 'jev')")
    conn.commit()
    jev = FakeJev(risk={t: {"低": 1.0}})
    judge(conn, jev).check("600001", TODAY)
    assert jev.title_calls == 1                     # 旧 'jev' 缓存不算数，重新按新体系分类


def test_non_suspects_counted_in_others_without_risk_call():
    """旧 test_clean_and_loss_ignored 的意图：定期报告、亏损类公告不算雷。"""
    a, b = "甲:2026年半年度报告", "甲:2026年半年度业绩预亏公告"
    conn = setup(a, b, "甲:董事会决议公告")
    jev = FakeJev(title_labels={b: "亏损"})
    j = judge(conn, jev)
    assert j.check("600001", TODAY) == ("pass", "公告无雷")
    assert j.last_trace["others"] == 3 and jev.risk_calls == 0


def test_risk_cache_and_version_bump(monkeypatch):
    t = "甲:关于股东减持计划的公告"
    conn = setup(t)
    jev = FakeJev(risk={t: {"低": 1.0}})
    judge(conn, jev).check("600001", TODAY)
    judge(conn, jev).check("600001", TODAY)
    assert jev.risk_calls == 1
    conn.execute("UPDATE notice_risk SET version=?", (RISK_VERSION - 1,))
    conn.commit()
    judge(conn, jev).check("600001", TODAY)
    assert jev.risk_calls == 2


def test_cached_risk_trace_keeps_body_and_probs():
    """旧 test_trace_records_each_notice_with_source_and_probs 后半段：走缓存时 trace 里的概率、正文摘录不丢。"""
    t = "甲:关于股东减持计划的公告"
    conn = setup(t)
    judge(conn, FakeJev(risk={t: {"低": 0.8, "无": 0.2}}), {"AN0": "减持已实施完毕"}).check("600001", TODAY)
    j2 = judge(conn, FakeJev())                      # 不给正文、不给风险答案：只能走缓存
    assert j2.check("600001", TODAY)[0] == "pass"
    s = j2.last_trace["suspects"][0]
    assert s["probs"] == {"低": 0.8, "无": 0.2} and s["body_read"] is True and s["excerpt"] == "减持已实施完毕"


def test_risk_failure_is_error_and_not_cached():
    t = "甲:关于股东减持计划的公告"
    conn = setup(t)
    j = judge(conn, FakeJev(fail_risk=[t]))
    assert j.check("600001", TODAY) == ("error", "Jev 不可用")
    assert conn.execute("SELECT COUNT(*) FROM notice_risk").fetchone()[0] == 0


def test_title_failure_is_error():
    """旧 test_jev_failure_is_error_and_not_cached：第一段失败 → error，且不写标题缓存。"""
    t = "甲:董事会决议公告"
    conn = setup(t)
    assert judge(conn, FakeJev(fail_titles=[t])).check("600001", TODAY) == ("error", "Jev 不可用")
    assert conn.execute("SELECT COUNT(*) FROM notice_verdicts").fetchone()[0] == 0


def test_partial_failure_caches_successes():
    a, b = "甲:关于股东减持计划的公告", "甲:关于回购进展的公告"
    conn = setup(a, b)
    jev = FakeJev(risk={a: {"低": 1.0}}, fail_risk=[b])
    assert judge(conn, jev).check("600001", TODAY)[0] == "error"
    assert conn.execute("SELECT id FROM notice_risk").fetchall() == [("AN0",)]


def test_partial_title_failure_caches_successes():
    """旧 test_partial_batch_failure_caches_successes：第一段一成一败，成功的缓存，下次只问失败的那条。"""
    a, b = "甲:关于重大合同的公告", "甲:关于对外投资的公告"
    conn = setup(a, b)
    assert judge(conn, FakeJev(fail_titles=[b])).check("600001", TODAY)[0] == "error"
    assert conn.execute("SELECT COUNT(*) FROM notice_verdicts").fetchone()[0] == 1
    jev2 = FakeJev()
    assert judge(conn, jev2).check("600001", TODAY) == ("pass", "公告无雷")
    assert jev2.title_calls == 1


def test_per_check_cap_and_skipped():
    conn = setup(*[(f"甲:减持公告{i}", f"2026-09-{i + 1:02d}") for i in range(25)])
    jev = FakeJev(risk={f"甲:减持公告{i}": {"低": 1.0} for i in range(25)})
    j = judge(conn, jev)
    j.check("600001", TODAY)
    assert jev.risk_calls == 20 and len(j.last_trace["skipped"]) == 5
    assert j.last_trace["skipped"][0]["category"] == "减持"


def test_trace_sorted_by_score_and_fields():
    a, b = "甲:关于股东减持计划的公告", "甲:关于股份质押的公告"
    conn = setup(a, b)
    j = judge(conn, FakeJev(risk={a: {"低": 1.0}, b: {"中": 1.0}}), {"AN1": "质押" * 200})
    j.check("600001", TODAY)
    s = j.last_trace["suspects"]
    assert [x["title"] for x in s] == [b, a]
    assert s[0]["verdict"] == "reject" and s[1]["verdict"] == "pass"
    assert s[0]["days_ago"] == 6 and len(s[0]["excerpt"]) == 200 and s[1]["excerpt"] == ""
    assert set(s[0]) >= {"id", "date", "title", "days_ago", "category", "source", "level", "score",
                         "probs", "body_read", "excerpt", "verdict"}
    json.dumps(j.last_trace, ensure_ascii=False)      # 能写进 decisions.trace


# ---- 正文抓取不能堵交易：并发 + 总时限 ----

def test_cached_body_used_without_fetch():
    t = "甲:关于股东减持计划的公告"
    conn = setup(t)
    conn.execute("INSERT INTO notice_bodies(id, text, fetched) VALUES ('AN0', '缓存里的正文', '2026-09-26T08:00:00')")
    conn.commit()
    calls = []
    jev = FakeJev(risk={t: {"低": 1.0}})
    j = NoticeJudge(conn, jev, fetch_body=lambda aid: calls.append(aid) or "网上的正文")
    j.check("600001", TODAY)
    assert calls == [] and jev.states[0]["正文摘要"] == "缓存里的正文"


def test_fetched_bodies_are_cached():
    a, b = "甲:关于股东减持计划的公告", "甲:关于股份质押的公告"
    conn = setup(a, b)
    jev = FakeJev(risk={a: {"低": 1.0}, b: {"低": 1.0}})
    judge(conn, jev, {"AN0": "正文甲", "AN1": "正文乙"}).check("600001", TODAY)
    rows = dict(conn.execute("SELECT id, text FROM notice_bodies").fetchall())
    assert rows == {"AN0": "正文甲", "AN1": "正文乙"}


def test_slow_fetch_beyond_budget_falls_back_to_title_and_returns_in_time():
    fast, slow = "甲:关于股东减持计划的公告", "甲:关于股份质押的公告"
    conn = setup(fast, slow)
    release = threading.Event()

    def fetch(aid):
        if aid == "AN1":
            release.wait(5)                          # 卡住，远超时限
            return "迟到的正文"
        return "及时的正文"

    jev = FakeJev(risk={fast: {"低": 1.0}, slow: {"低": 1.0}})
    j = NoticeJudge(conn, jev, fetch_body=fetch, body_budget=0.3)
    t0 = time.monotonic()
    try:
        assert j.check("600001", TODAY)[0] == "pass"
        elapsed = time.monotonic() - t0
    finally:
        release.set()
    assert elapsed < 2.0
    by_id = {s["id"]: s for s in j.last_trace["suspects"]}
    assert by_id["AN0"]["body_read"] is True and by_id["AN1"]["body_read"] is False
    states = {s["公告标题"]: s["正文摘要"] for s in jev.states}
    assert states[slow] == "未读到正文，仅凭标题判断"
    assert dict(conn.execute("SELECT id, text FROM notice_bodies").fetchall()) == {"AN0": "及时的正文"}
    # 超时那条只按标题判了这一次，不写进 notice_risk：下次拿到正文再判
    assert conn.execute("SELECT id FROM notice_risk").fetchall() == [("AN0",)]


def test_late_body_is_picked_up_on_next_check():
    t = "甲:关于股份质押的公告"
    conn = setup(t)
    release, calls = threading.Event(), []

    def fetch(aid):
        calls.append(aid)
        release.wait(5)
        return "迟到的正文"

    jev = FakeJev(risk={t: {"低": 1.0}})
    j = NoticeJudge(conn, jev, fetch_body=fetch, body_budget=0.2)
    j.check("600001", TODAY)
    release.set()
    for _ in range(50):                              # 等后台那次抓取落地
        if j._late_bodies:
            break
        time.sleep(0.02)
    j.check("600001", TODAY)
    assert calls == ["AN0"]                          # 没有重抓
    assert jev.states[-1]["正文摘要"] == "迟到的正文"
    assert conn.execute("SELECT text FROM notice_bodies WHERE id='AN0'").fetchone() == ("迟到的正文",)
    assert conn.execute("SELECT body_read FROM notice_risk WHERE id='AN0'").fetchone() == (1,)


def test_fetch_exception_falls_back_to_title():
    t = "甲:关于股东减持计划的公告"
    conn = setup(t)

    def boom(aid):
        raise RuntimeError("network down")

    jev = FakeJev(risk={t: {"低": 1.0}})
    j = NoticeJudge(conn, jev, fetch_body=boom)
    assert j.check("600001", TODAY)[0] == "pass"
    assert jev.states[0]["正文摘要"] == "未读到正文，仅凭标题判断"
    assert j.last_trace["suspects"][0]["body_read"] is False
    assert conn.execute("SELECT COUNT(*) FROM notice_bodies").fetchone()[0] == 0
