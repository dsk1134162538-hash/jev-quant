from datetime import date

import pytest

from jevq.data.akdata import Notice
from jevq.judge import notice_risk as nr


@pytest.mark.parametrize("title,cat", [
    ("甲:关于持股5%以上股东减持股份计划的预披露公告", "减持"),
    ("先导基电:关于控股股东自愿承诺未来12个月不减持公司股份的公告", "减持"),
    ("甲:关于股东部分股份解除质押的公告", "质押"),
    ("甲:关于回购公司股份的进展公告", "回购"),
    ("甲:关于公司涉及诉讼的公告", "诉讼仲裁"),
    ("甲:关于筹划重大资产重组的停牌公告", "重组"),
    ("甲:股票交易严重异常波动公告", "风险提示"),
    ("甲:关于收到中国证监会立案告知书的公告", "监管处罚"),
    ("甲:关于公司股票可能被实施退市风险警示的提示性公告", "退市风险"),
    ("甲:关于首发限售股上市流通的提示性公告", "限售解禁"),
    ("甲:关于控股股东所持股份被司法冻结的公告", "冻结和债务"),
    ("甲:关于变更会计师事务所的公告", "审计问题"),
    ("甲:2026年半年度报告", None),
    ("甲:第五届董事会第十次会议决议公告", None),
])
def test_keyword_category(title, cat):
    assert nr.keyword_category(title) == cat


def test_eleven_categories_each_with_four_levels():
    assert len(nr.RISK_CATEGORIES) == 11
    for name, c in nr.RISK_CATEGORIES.items():
        assert c["keywords"] and c["title"]
        assert set(c["levels"]) == set(nr.LEVELS), name


def test_title_criteria_has_other_and_loss():
    crit = nr.title_criteria()
    assert set(nr.RISK_CATEGORIES) <= set(crit)
    assert "其他" in crit and "亏损" in crit


def test_risk_question_uses_category_levels():
    q = nr.risk_question("回购")["risk"]
    assert q["type"] == "choice"
    assert set(q["criteria"]) == set(nr.LEVELS)
    assert "终止" in q["criteria"]["高"]


def test_risk_state_with_and_without_body():
    n = Notice("AN1", "600001", "甲:回购进展", "回购", "2026-09-20")
    s = nr.risk_state(n, "回购", "正文" * 2000, date(2026, 9, 26))
    assert s["距今天数"] == 6 and s["疑似类别"] == "回购"
    assert len(s["正文摘要"]) == 1500
    s2 = nr.risk_state(n, "回购", None, date(2026, 9, 26))
    assert s2["正文摘要"] == "未读到正文，仅凭标题判断"


@pytest.mark.parametrize("probs,score", [
    ({"高": 1.0}, 1.0),
    ({"中": 1.0}, 0.6),
    ({"低": 1.0}, 0.2),
    ({"无": 1.0}, 0.0),
    ({"高": 0.5, "低": 0.5}, 0.6),
    ({}, 0.0),
])
def test_risk_score(probs, score):
    assert nr.risk_score(probs) == pytest.approx(score)


@pytest.mark.parametrize("score,verdict", [
    (0.349, "pass"), (0.35, "unsure"), (0.499, "unsure"), (0.5, "reject"), (1.0, "reject"),
])
def test_classify_score_boundaries(score, verdict):
    assert nr.classify_score(score) == verdict
