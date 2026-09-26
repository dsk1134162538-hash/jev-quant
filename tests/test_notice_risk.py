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
    ("甲:关于收到上海证券交易所关注函的公告", "监管处罚"),
    ("甲:关于收到深圳证券交易所监管工作函的公告", "监管处罚"),
    ("甲:关于法院裁定受理公司重整的公告", "退市风险"),
    ("甲:关于公司被债权人申请破产重整的提示性公告", "退市风险"),
    ("甲:关于子公司破产清算的公告", "退市风险"),
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


def test_new_keywords_mentioned_in_level_texts():
    assert "关注函" in nr.RISK_CATEGORIES["监管处罚"]["levels"]["中"]
    assert "工作函" in nr.RISK_CATEGORIES["监管处罚"]["levels"]["中"]
    assert "破产重整" in nr.RISK_CATEGORIES["退市风险"]["levels"]["高"]


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


# ---- review fixes ----


@pytest.mark.parametrize("title", [
    "甲:关于2024年限制性股票激励计划第一个解除限售期解除限售暨上市流通的提示性公告",
    "甲:关于发行股份购买资产之部分限售股份解除限售上市流通的提示性公告",
    "甲:关于2022年限制性股票激励计划的第三次解除限售期解锁上市流通的提示性公告",
    "甲:关于部分首次公开发行前已发行股份及首次公开发行战略配售股份上市流通提示性公告",
    "甲:关于首发限售股解禁的风险提示性公告",           # 「风险提示」是通用后缀，不能抢走解禁
])
def test_unlock_titles_seen_in_real_data_hit_unlock(title):
    assert nr.keyword_category(title) == "限售解禁"


def test_other_risk_warning_and_par_value_covered_by_delisting_criteria():
    lv = nr.RISK_CATEGORIES["退市风险"]["levels"]
    assert "其他风险警示" in lv["高"] and "其他风险警示" in lv["低"]
    assert "1 元" in lv["高"]                       # 面值退市说清楚，别和可转债「面值总额」混淆


@pytest.mark.parametrize("probs,verdict", [
    ({"中": 0.75, "低": 0.25}, "reject"),           # 数学上正好 0.5，浮点算出 0.49999999999999994
    ({"中": 0.82, "低": 0.04, "无": 0.14}, "reject"),
    ({"中": 0.5, "低": 0.25, "无": 0.25}, "unsure"),  # 正好 0.35
])
def test_score_on_exact_threshold_is_not_lost_to_float_error(probs, verdict):
    assert nr.classify_score(nr.risk_score(probs)) == verdict


@pytest.mark.parametrize("title,routine", [
    ("大连圣亚:股票交易异常波动公告", True),                       # 交易所强制披露的普通异动
    ("甲:关于公司股票交易异常波动的公告", True),
    ("甲:股票交易严重异常波动公告", False),
    ("甲:股票交易异常波动暨风险提示公告", False),
    ("国恩股份:2026年1-6月非经营性资金占用及其他关联资金往来情况汇总表", True),
    ("甲:关于控股股东非经营性资金占用的专项说明", True),
    ("甲:关于控股股东非经营性资金占用的公告", False),
    ("甲:关于股东减持计划的公告", False),
])
def test_is_routine(title, routine):
    assert nr.is_routine(title) is routine


def test_risk_hint_only_counts_within_10_days():
    assert nr.in_window("风险提示", 10) and not nr.in_window("风险提示", 11)
    assert nr.in_window("减持", 90)
