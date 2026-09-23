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
