from datetime import date

import pytest

from jevq import db
from jevq.data.akdata import Notice
from jevq.judge.jev import JevError
from jevq.judge.notices import NoticeJudge, classify_by_keyword

TODAY = date(2026, 9, 23)


@pytest.mark.parametrize("title,label", [
    ("甲:关于持股5%以上股东减持股份计划的预披露公告", "新增减持"),
    ("甲:关于控股股东部分股份质押的公告", "新增质押"),
    ("甲:关于回购公司股份的进展公告", "回购"),
    ("甲:关于公司涉及诉讼的公告", "诉讼仲裁"),
    ("甲:关于股东部分股份解除质押的公告", None),
    ("甲:关于股东减持计划实施完毕的公告", None),
    ("甲:关于控股股东权益变动的提示性公告", None),
    ("甲:2026年半年度报告", None),
])
def test_keyword(title, label):
    assert classify_by_keyword(title) == label


class FakeJev:
    def __init__(self, labels, confidence=0.9, fail=False):
        self.labels, self.confidence, self.fail, self.calls = labels, confidence, fail, 0

    def ask(self, state, questions):
        self.calls += 1
        if self.fail:
            raise JevError("down")
        label = self.labels[state["公告标题"]]
        return {"kind": {"type": "choice", "choice": label,
                         "probabilities": {label: self.confidence}, "confidence": self.confidence}}


class PartialFailJev:
    """Succeeds for title1, fails for title2."""
    def __init__(self):
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        title = state["公告标题"]
        if "title1" in title:
            return {"kind": {"type": "choice", "choice": "其他",
                           "probabilities": {"其他": 0.9}, "confidence": 0.9}}
        else:  # title2
            raise JevError("down")


def setup(*titles):
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO notices VALUES (?, ?, ?, ?, ?)",
                     [(f"AN{i}", "600001", t, "", "2026-09-01") for i, t in enumerate(titles)])
    return conn


def test_keyword_mine_rejects_without_jev():
    conn = setup("甲:关于股东减持计划的公告")
    jev = FakeJev({})
    verdict, why = NoticeJudge(conn, jev).check("600001", TODAY)
    assert verdict == "reject" and "减持" in why and jev.calls == 0


def test_jev_classifies_ambiguous_and_caches():
    title = "甲:关于控股股东权益变动的提示性公告"
    conn = setup(title)
    jev = FakeJev({title: "新增减持"})
    judge = NoticeJudge(conn, jev)
    assert judge.check("600001", TODAY)[0] == "reject"
    assert judge.check("600001", TODAY)[0] == "reject"
    assert jev.calls == 1                                  # 第二次走缓存


def test_clean_and_loss_ignored():
    titles = ["甲:2026年半年度报告", "甲:2026年半年度业绩预亏公告"]
    conn = setup(*titles)
    jev = FakeJev({titles[0]: "其他", titles[1]: "亏损"})
    assert NoticeJudge(conn, jev).check("600001", TODAY) == ("pass", "公告无雷")


def test_low_confidence_counts_as_mine():
    title = "甲:关于重大事项的公告"
    conn = setup(title)
    verdict, why = NoticeJudge(conn, FakeJev({title: "其他"}, confidence=0.5)).check("600001", TODAY)
    assert verdict == "reject" and "拿不准" in why


def test_jev_failure_is_error_and_not_cached():
    title = "甲:关于重大事项的公告"
    conn = setup(title)
    assert NoticeJudge(conn, FakeJev({}, fail=True)).check("600001", TODAY)[0] == "error"
    assert conn.execute("SELECT COUNT(*) FROM notice_verdicts").fetchone()[0] == 0


def test_no_notices_passes():
    assert NoticeJudge(db.connect(":memory:"), FakeJev({})).check("600001", TODAY)[0] == "pass"


def test_trace_records_each_notice_with_source_and_probs():
    titles = ["甲:关于回购公司股份的进展公告", "甲:关于重大合同的公告"]
    conn = setup(*titles)
    judge = NoticeJudge(conn, FakeJev({titles[1]: "其他"}, confidence=0.8))
    assert judge.check("600001", TODAY)[0] == "reject"
    by_title = {t["title"]: t for t in judge.last_trace}
    assert by_title[titles[0]]["source"] == "keyword" and by_title[titles[0]]["mine"] is True
    assert by_title[titles[0]]["probabilities"] is None
    assert by_title[titles[1]]["source"] == "jev" and by_title[titles[1]]["probabilities"] == {"其他": 0.8}
    assert by_title[titles[1]]["unsure"] is False
    judge2 = NoticeJudge(conn, FakeJev({}))                 # 全部走缓存
    judge2.check("600001", TODAY)
    assert {t["title"]: t["probabilities"] for t in judge2.last_trace}[titles[1]] == {"其他": 0.8}


def test_partial_batch_failure_caches_successes():
    """Two ambiguous notices, one succeeds and is cached, one fails.
    First check returns error but one notice is cached.
    Second check: previously-failing one succeeds, Jev called only once."""
    titles = ["ambiguous title1", "ambiguous title2"]
    conn = setup(*titles)

    # First check: title1 succeeds, title2 fails
    jev1 = PartialFailJev()
    verdict1, why1 = NoticeJudge(conn, jev1).check("600001", TODAY)
    assert verdict1 == "error"
    # Exactly one notice cached (the successful one)
    cached_count = conn.execute("SELECT COUNT(*) FROM notice_verdicts").fetchone()[0]
    assert cached_count == 1

    # Second check: provide a Jev that succeeds for all
    jev2 = FakeJev({"ambiguous title1": "其他", "ambiguous title2": "其他"})
    verdict2, _ = NoticeJudge(conn, jev2).check("600001", TODAY)
    assert verdict2 == "pass"
    # Should have called Jev only once (for title2, since title1 is cached)
    assert jev2.calls == 1
