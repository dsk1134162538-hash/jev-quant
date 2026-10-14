"""大盘情绪、板块情绪改由 Claude 判（用户 2026-10-14：直接切换）。ClaudeJev 和 JevClient 接口一样，
判断器不用改；Claude 失败自动退回 Jev，交易不断。"""
import json
from datetime import date, datetime

import pytest

from jevq import db
from jevq.judge.claude_jev import ClaudeJev
from jevq.judge.jev import JevError, score_label, score_probs
from jevq.judge.market import MarketJudge
from jevq.judge.own_sectors import OwnSectorJudge

LEVELS = ["冰点", "偏弱", "正常", "火热"]
SCORE_Q = {"level": {"type": "score", "instructions": "情绪？", "criteria": LEVELS}}


class FakeCLI:
    def __init__(self, *answers):
        self.answers, self.calls, self.last_trace = list(answers), [], {"cost_usd": 0.03}

    def ask_json(self, system, user, timeout=None):
        self.calls.append((system, user, timeout))
        return self.answers.pop(0) if self.answers else None


class FakeJev:
    def __init__(self):
        self.calls = []

    def ask(self, state, questions, timeout=None, deadline=None):
        self.calls.append(state)
        return {"level": {"type": "score", "legend": {"0": "冰点", "1": "偏弱", "2": "正常", "3": "火热"},
                          "probabilities": {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0}, "confidence": 1.0, "score": 1.0}}


def test_score_answer_converted_to_jev_format():
    cli = FakeCLI({"level": {"冰点": 0.05, "偏弱": 0.25, "正常": 0.6, "火热": 0.1}})
    ans = ClaudeJev(cli).ask({"涨停家数": 40}, SCORE_Q)["level"]
    assert score_label(ans) == "正常" and score_probs(ans) == {"冰点": 0.05, "偏弱": 0.25, "正常": 0.6, "火热": 0.1}
    assert ans["confidence"] == 0.6
    system, user, _ = cli.calls[0]
    assert "涨停家数" in user and "冰点" in user and "情绪？" in user and "JSON" in system


def test_probabilities_normalised_and_missing_levels_are_zero():
    ans = ClaudeJev(FakeCLI({"level": {"正常": 3, "火热": 1}})).ask({"x": 1}, SCORE_Q)["level"]
    assert score_probs(ans) == {"冰点": 0.0, "偏弱": 0.0, "正常": 0.75, "火热": 0.25}


def test_choice_question():
    q = {"fit": {"type": "choice", "instructions": "属于吗？", "criteria": {"是": "主要是", "否": "不是"}}}
    ans = ClaudeJev(FakeCLI({"fit": {"choice": "否", "confidence": 0.8}})).ask({"x": 1}, q)["fit"]
    assert ans["choice"] == "否" and ans["confidence"] == 0.8


def test_falls_back_to_jev_when_claude_fails_and_says_who_answered():
    jev = FakeJev()
    for bad in (None, {"level": {"未知档": 1}}, {"level": "正常"}):
        c = ClaudeJev(FakeCLI(bad), fallback=jev)
        assert score_label(c.ask({"x": 1}, SCORE_Q)["level"]) == "偏弱" and c.last_source == "jev"
    c = ClaudeJev(FakeCLI({"level": {"正常": 1}}), fallback=jev)
    c.ask({"x": 1}, SCORE_Q)
    assert c.last_source == "claude"


def test_without_fallback_failure_raises_jev_error():
    with pytest.raises(JevError):
        ClaudeJev(FakeCLI(None)).ask({"x": 1}, SCORE_Q)


def test_calls_logged_like_jev():
    conn = db.connect(":memory:")
    ClaudeJev(FakeCLI({"level": {"正常": 1}}), conn=conn).ask({"x": 1}, SCORE_Q)
    req, resp = conn.execute("SELECT request, response FROM jev_calls").fetchone()
    assert json.loads(req)["model"].startswith("claude") and "正常" in resp


def test_market_judge_uses_claude_records_source_and_passes_news_and_catalysts():
    cli = FakeCLI({"level": {"正常": 0.7, "偏弱": 0.3}})
    judge = MarketJudge(ClaudeJev(cli, fallback=FakeJev()))
    label = judge.sentiment({"涨停家数": 40}, news=["10:05 财联社｜快讯"], catalyst=["人形机器人（28 条）：量产提速"])
    assert label == "正常" and judge.sentiment_trace["by"] == "claude"
    user = cli.calls[0][1]
    assert "快讯" in user and "人形机器人（28 条）" in user and "隔夜消息催化" in user


def test_market_judge_with_plain_jev_still_says_jev():
    judge = MarketJudge(FakeJev())
    assert judge.sentiment({"涨停家数": 40}) == "偏弱" and judge.sentiment_trace["by"] == "jev"


def test_own_sector_strength_uses_its_own_client_classification_stays_on_jev():
    jev, cli = FakeJev(), FakeCLI({"level": {"火热": 0.9, "正常": 0.1}})
    j = OwnSectorJudge(jev, strength_jev=ClaudeJev(cli))
    label, probs, conf = j.strength({"name": "光模块", "product_def": "光通信模块"}, {"strong": 6, "相关新闻": ["CPO 订单"]})
    assert label == "火热" and jev.calls == [] and "CPO 订单" in cli.calls[0][1]
    assert OwnSectorJudge(jev).strength({"name": "x", "product_def": "y"}, {})[0] == "偏弱"   # 不给就仍用 Jev


def test_sector_news_from_overnight_catalyst_digest():
    from jevq.data import catalyst
    from jevq.data.own_sectors import sector_news
    conn = db.connect(":memory:")
    catalyst.record(conn, date(2026, 10, 15), {"window": ["a", "b"], "ok": 1, "failed": [], "news": 3, "topics": [
        {"topic": "光模块", "topics": ["光模块"], "count": 3,
         "news": [{"title": "CPO 大单", "source": "36氪", "link": "l", "published": "10-15 07:00"}]},
        {"topic": "储能", "topics": ["储能"], "count": 1, "news": []}]})
    assert sector_news(conn, {"name": "光模块", "aliases": ["CPO"]}, date(2026, 10, 15)) == \
        {"隔夜相关新闻数": 3, "隔夜相关新闻": ["CPO 大单（36氪）"]}
    assert sector_news(conn, {"name": "半导体", "aliases": []}, date(2026, 10, 15)) == {}


def test_catalyst_summary_lines_for_sentiment():
    from jevq.data import catalyst
    conn = db.connect(":memory:")
    assert catalyst.summary_lines(conn, date(2026, 10, 15)) == []
    catalyst.record(conn, date(2026, 10, 15), {"window": ["a", "b"], "ok": 1, "failed": [], "news": 3, "topics": [
        {"topic": "人形机器人", "topics": ["人形机器人"], "count": 28,
         "news": [{"title": "量产提速", "source": "36氪", "link": "l", "published": "x"},
                  {"title": "融资", "source": "东财", "link": "l", "published": "x"}]}]})
    assert catalyst.summary_lines(conn, date(2026, 10, 15)) == ["人形机器人（28 条）：量产提速；融资"]
