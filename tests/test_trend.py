"""Jev 判断持仓票今天的分时走向（超跌反弹主账户止损 / 低开反抽前问）。"""
from datetime import date, datetime

from factories import position, quote

from jevq.judge.jev import JevError
from jevq.judge.trend import TrendJudge, minute_summary

NOW = datetime(2026, 10, 9, 10, 15)


class ScoreJev:
    def __init__(self, label, fail=False):
        self.label, self.fail, self.states, self.kw = label, fail, [], None

    def ask(self, state, questions, **kw):
        self.states.append(state)
        self.kw = kw
        if self.fail:
            raise JevError("down")
        (key, q), = questions.items()
        legend = {str(i): v for i, v in enumerate(q["criteria"])}
        idx = q["criteria"].index(self.label)
        probs = {k: (0.9 if int(k) == idx else 0.02) for k in legend}
        return {key: {"type": "score", "score": idx, "legend": legend, "probabilities": probs, "confidence": 0.8}}


def _rows(prices, start=(9, 30), vol_step=100):
    h, m = start
    out = []
    for i, p in enumerate(prices):
        mm = h * 60 + m + i
        out.append((f"{mm // 60:02d}:{mm % 60:02d}", p, (i + 1) * vol_step))
    return out


def test_minute_summary_samples_every_five_minutes_and_keeps_the_last_ten():
    rows = _rows([10.0 - i * 0.01 for i in range(46)])      # 09:30–10:15
    lines = minute_summary(rows, pre_close=10.0)
    assert lines[0].startswith("09:30 10.00 +0.00%")
    assert "09:35 9.95 -0.50%" in lines[1] and "量 500手" in lines[1]   # 5 分钟增量
    assert lines[-1].startswith("10:15 9.55 -4.50%")
    assert sum(1 for x in lines if x[:5] >= "10:06") == 10                # 最后 10 分钟逐分钟


def test_judge_asks_jev_with_price_shape_and_returns_label():
    jev = ScoreJev("探底回升")
    seen = []
    judge = TrendJudge(jev, fetch_minutes=lambda code, timeout: seen.append((code, timeout)) or _rows([10.0, 9.8, 9.9]))
    p = position("002658", account="rebound_main", buy_price=10.0, buy_date=date(2026, 10, 8))
    q = quote("002658", pct=-1.0, open_pct=-0.5, low_pct=-2.5, name="雪迪龙", ts=NOW)
    assert judge.judge(p, q, NOW) == "探底回升"
    state = jev.states[0]
    assert state["名称"] == "雪迪龙" and state["持仓盈亏%"] == -1.0 and state["最低涨幅%"] == -2.5
    assert len(state["分时"]) == 3
    assert seen[0][0] == "002658" and seen[0][1] > 0
    assert judge.last_trace["label"] == "探底回升" and "probabilities" in judge.last_trace


def test_judge_still_asks_without_minute_data():
    jev = ScoreJev("单边下跌")

    def boom(code, timeout):
        raise OSError("down")
    judge = TrendJudge(jev, fetch_minutes=boom)
    q = quote("002658", pct=-2.2, ts=NOW)
    assert judge.judge(position("002658"), q, NOW) == "单边下跌"
    assert jev.states[0]["分时"] == []
    assert "分时获取失败" in judge.last_trace["note"]


def test_judge_returns_none_when_jev_fails():
    judge = TrendJudge(ScoreJev("震荡", fail=True), fetch_minutes=lambda code, timeout: [])
    assert judge.judge(position("002658"), quote("002658", pct=-2.2, ts=NOW), NOW) is None
    assert judge.last_trace["label"] is None
