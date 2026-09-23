from datetime import datetime, timedelta

from jevq import db
from jevq.data.akdata import News
from jevq.judge.jev import JevError
from jevq.judge.market import MarketJudge
from jevq.judge.news import NewsJudge

NOW = datetime(2026, 9, 24, 10, 0)


class ScoreJev:
    def __init__(self, label, fail=False):
        self.label, self.fail, self.calls, self.states = label, fail, 0, []

    def ask(self, state, questions):
        self.calls += 1
        self.states.append(state)
        if self.fail:
            raise JevError("down")
        (key, q), = questions.items()
        legend = {str(i): v for i, v in enumerate(q["criteria"])}
        idx = q["criteria"].index(self.label)
        probs = {k: (0.9 if int(k) == idx else 0.1 / (len(legend) - 1)) for k in legend}
        return {key: {"type": "score", "score": idx, "legend": legend,
                      "probabilities": probs, "confidence": 0.8}}


def news(hours_ago, title="标题"):
    return News(title, "内容" * 80, NOW - timedelta(hours=hours_ago))


def test_bearish_news_rejects_and_caches():
    conn = db.connect(":memory:")
    jev = ScoreJev("偏空")
    judge = NewsJudge(conn, jev, fetch=lambda code: [news(2, "利空标题")])
    assert judge.check("600001", "甲", NOW) == ("reject", "消息面偏空")
    assert judge.check("600001", "甲", NOW)[0] == "reject"
    assert jev.calls == 1
    assert len(jev.states[0]["近24小时新闻"][0]) <= 130   # 标题 + 截断内容


def test_old_news_ignored_counts_as_neutral():
    jev = ScoreJev("偏空")
    judge = NewsJudge(db.connect(":memory:"), jev, fetch=lambda code: [news(30)])
    assert judge.check("600001", "甲", NOW) == ("pass", "消息面中性")
    assert jev.calls == 0


def test_news_errors():
    judge = NewsJudge(db.connect(":memory:"), ScoreJev("中性", fail=True), fetch=lambda c: [news(1)])
    assert judge.check("600001", "甲", NOW)[0] == "error"

    def boom(code):
        raise RuntimeError("akshare down")
    judge = NewsJudge(db.connect(":memory:"), ScoreJev("中性"), fetch=boom)
    assert judge.check("600001", "甲", NOW)[0] == "error"


def test_market_judge():
    assert MarketJudge(ScoreJev("火热")).sentiment({"涨停家数": 80}) == "火热"
    assert MarketJudge(ScoreJev("偏弱")).overseas({"道琼斯": -1.2}) == "偏弱"
    assert MarketJudge(ScoreJev("正常", fail=True)).sentiment({"涨停家数": 1}) is None
    assert MarketJudge(ScoreJev("中性")).overseas({}) is None


def test_news_trace_and_cache_roundtrip():
    conn = db.connect(":memory:")
    judge = NewsJudge(conn, ScoreJev("中性"), fetch=lambda code: [news(1, "利好标题")])
    assert judge.check("600001", "甲", NOW) == ("pass", "消息面中性")
    t = judge.last_trace
    assert t["label"] == "中性" and t["news"][0].startswith("利好标题")
    assert t["probabilities"]["中性"] == 0.9
    judge2 = NewsJudge(conn, ScoreJev("偏空"), fetch=lambda code: [])   # 走缓存
    assert judge2.check("600001", "甲", NOW) == ("pass", "消息面中性")
    assert judge2.last_trace == t


def test_news_reads_legacy_plain_cache():
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO day_cache VALUES (?, 'news', '600001', '偏空')", (NOW.date().isoformat(),))
    judge = NewsJudge(conn, ScoreJev("中性"), fetch=lambda code: [])
    assert judge.check("600001", "甲", NOW) == ("reject", "消息面偏空")
    assert judge.last_trace["label"] == "偏空"


def test_market_traces():
    m = MarketJudge(ScoreJev("火热"))
    m.sentiment({"涨停家数": 80})
    assert m.sentiment_trace["inputs"] == {"涨停家数": 80} and m.sentiment_trace["label"] == "火热"
    assert m.sentiment_trace["probabilities"]["火热"] == 0.9
    m2 = MarketJudge(ScoreJev("中性", fail=True))
    assert m2.overseas({"道琼斯": 0.1}) is None and m2.overseas_trace is None
