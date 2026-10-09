"""情绪拐点抄底的规则部分（用户 2026-10-09：恐慌后一有反弹拉升迹象就快速抄底，直接实盘，信号要明确）。"""
from datetime import datetime, time

from factories import quote

from jevq.rules import turn
from jevq.rules.turn import Breadth

DAY = datetime(2026, 10, 9)

# 10-09 当天后台每 10 分钟记下的全市场统计（股票池 3046 只）：时间、上涨、下跌、跌停、成交额较昨日同期%
TODAY_1009 = [
    ("09:30", 916, 1519, 1, -8.3), ("09:40", 654, 2277, 0, -2.3), ("09:50", 1045, 1851, 3, -5.2),
    ("10:01", 797, 2136, 5, -6.0), ("10:12", 681, 2267, 9, -4.0), ("10:22", 743, 2193, 8, 0.4),
    ("10:32", 657, 2295, 14, 3.2), ("10:43", 738, 2202, 11, 3.6), ("10:56", 847, 2112, 11, 4.1),
    ("11:06", 760, 2194, 15, 5.1), ("11:16", 649, 2309, 18, 5.5), ("11:27", 696, 2247, 16, 6.2),
    ("13:00", 890, 2045, 15, 6.7), ("13:10", 1237, 1724, 10, 9.2), ("13:20", 1335, 1612, 9, 10.9),
    ("13:31", 1373, 1569, 8, 13.3), ("13:41", 1504, 1443, 6, 14.4), ("14:02", 1513, 1438, 10, 12.8),
    ("14:12", 1835, 1098, 4, 14.4),
]


def _series(rows=TODAY_1009, total=3046):
    out = []
    for hm, up, down, ld, amt in rows:
        h, m = map(int, hm.split(":"))
        out.append(Breadth(DAY.replace(hour=h, minute=m), up, down, total, 0, ld, amt))
    return out


def _first_trigger(series):
    for i in range(len(series)):
        ok, _ = turn.turn_check(series[:i + 1], series[i].ts)
        if ok:
            return series[i].ts.strftime("%H:%M")
    return None


def test_trading_index_skips_lunch():
    assert turn.trading_index(time(9, 30)) == 0
    assert turn.trading_index(time(11, 29)) == 119
    assert turn.trading_index(time(13, 0)) == 120
    assert turn.trading_index(time(14, 0)) == 180


def test_1009_replay_triggers_at_1310_not_in_the_morning():
    """10-09：上午恐慌（红盘最少 649 家、跌停最多 18 家），13:10 红盘 1237、跌停降到 10、放量 → 触发。
    程序的情绪标签 13:41 才转「正常」，拐点信号早半小时。"""
    assert _first_trigger(_series()) == "13:10"


def test_1009_morning_bounce_at_0950_is_not_a_turn():
    """09:40 → 09:50 红盘 654 → 1045：低点才 10 分钟、红盘占比 34% 不到 35%，不算拐点。"""
    ok, d = turn.turn_check(_series()[:3], DAY.replace(hour=9, minute=50))
    assert not ok and not d["checks"]["low_age"] and not d["checks"]["up_min"]


def test_no_panic_no_turn():
    """一整天红盘都没低于 30%：不是恐慌日，涨起来也不叫抄底（那是正常行情，走正常的情绪买点）。"""
    rows = [("09:40", 1200, 1700, 2, 0.0), ("10:30", 1100, 1800, 3, 1.0), ("13:10", 1700, 1200, 1, 5.0)]
    ok, d = turn.turn_check(_series(rows), DAY.replace(hour=13, minute=10))
    assert not ok and not d["checks"]["panic"]


def test_limit_downs_must_shrink():
    rows = [r if r[0] != "13:10" else ("13:10", 1237, 1724, 17, 9.2) for r in TODAY_1009[:14]]
    ok, d = turn.turn_check(_series(rows), DAY.replace(hour=13, minute=10))
    assert not ok and not d["checks"]["limit_down"]


def test_volume_must_be_expanding():
    rows = [r if r[0] != "13:10" else ("13:10", 1237, 1724, 10, 4.0) for r in TODAY_1009[:14]]
    ok, d = turn.turn_check(_series(rows), DAY.replace(hour=13, minute=10))
    assert not ok and not d["checks"]["volume"]


def test_missing_volume_ratio_is_not_a_pass():
    rows = [(hm, up, down, ld, None) for hm, up, down, ld, _ in TODAY_1009[:14]]
    ok, d = turn.turn_check(_series(rows), DAY.replace(hour=13, minute=10))
    assert not ok and not d["checks"]["volume"]


def test_outside_window_never_triggers():
    s = _series()[:14]
    ok, d = turn.turn_check(s, DAY.replace(hour=14, minute=51))
    assert not ok and not d["checks"]["window"]


def test_new_low_resets_the_age():
    """13:10 又创新低：恐慌还没释放完，不算拐点。"""
    rows = TODAY_1009[:13] + [("13:10", 600, 2350, 10, 9.2)]
    ok, d = turn.turn_check(_series(rows), DAY.replace(hour=13, minute=10))
    assert not ok and not d["checks"]["low_age"]


def test_breadth_counts_universe_quotes_of_today_only():
    now = datetime(2026, 10, 9, 13, 10)
    qs = {"600001": quote("600001", pct=1.0, ts=now), "600002": quote("600002", pct=-10.0, ts=now),
          "600003": quote("600003", pct=-1.0, ts=now), "600004": quote("600004", pct=2.0, ts=datetime(2026, 10, 8, 15)),
          "000001": quote("000001", pct=10.0, ts=now)}
    b = turn.breadth(qs, {"600001": "", "600002": "", "600003": "", "600004": ""}, now, amount_ratio=9.2)
    assert (b.up, b.down, b.total, b.limit_down, b.limit_up) == (1, 2, 3, 1, 0)
    assert b.amount_ratio == 9.2 and round(b.up_ratio, 3) == 0.333


# ---- 引擎：每分钟记广度，规则满足 → 问 Jev，确认了当天锁定 ----
import json  # noqa: E402
from datetime import date, timedelta  # noqa: E402

from jevq import db  # noqa: E402
from jevq.data.news7x24 import Flash  # noqa: E402
from jevq.engine import Engine  # noqa: E402
from jevq.judge.jev import JevError  # noqa: E402
from jevq.judge.turn import YES, TurnJudge, trajectory  # noqa: E402
from jevq.ledger import Ledger  # noqa: E402
from jevq.rules.buy import buy_window_open, rebound_mode  # noqa: E402


class FakeTurnJudge:
    def __init__(self, probs):
        self.probs, self.calls, self.last_trace = list(probs), [], None

    def confirm(self, series, detail, now, sentiment, overseas):
        self.calls.append(now)
        p = self.probs.pop(0) if self.probs else 0.9
        self.last_trace = {"prob": p}
        return p


class Market:
    sentiment_trace = None

    def sentiment(self, stats, *a, **k):
        return "偏弱"


def _engine(judge, conn=None):
    conn = conn or db.connect(":memory:")
    eng = Engine(conn, Ledger(conn), Market(), lambda codes: {}, lambda d: {"涨停家数": 1}, {"600001": ""},
                 date(2026, 10, 8), "偏弱")
    eng.turn_judge = judge
    return eng, conn


def _feed(eng, rows=TODAY_1009):
    eng.breadth_series = _series(rows)


def test_engine_confirms_turn_once_and_records_it(monkeypatch):
    monkeypatch.setattr("jevq.config.TURN_JEV_REQUIRED", True)
    judge = FakeTurnJudge([0.8])
    eng, conn = _engine(judge)
    _feed(eng, TODAY_1009[:13])
    eng._turn_step(DAY.replace(hour=13, minute=0))
    assert judge.calls == [] and not eng.turn_on                     # 13:00 规则还不满足
    _feed(eng, TODAY_1009[:14])
    eng._turn_step(DAY.replace(hour=13, minute=10))
    assert eng.turn_on and len(judge.calls) == 1
    eng._turn_step(DAY.replace(hour=13, minute=11))
    assert len(judge.calls) == 1                                      # 锁定后不再问
    (detail,) = conn.execute("SELECT detail FROM decisions WHERE verdict='turn'").fetchone()
    d = json.loads(detail)
    assert d["confirmed"] and d["prob"] == 0.8 and "红盘 1237/3046" in d["text"]


def test_engine_retries_after_jev_says_no_and_immediately_after_jev_fails(monkeypatch):
    monkeypatch.setattr("jevq.config.TURN_JEV_REQUIRED", True)
    judge = FakeTurnJudge([0.3, None, 0.7])
    eng, conn = _engine(judge)
    _feed(eng, TODAY_1009[:14])
    t = DAY.replace(hour=13, minute=10)
    eng._turn_step(t)                                                 # Jev 不认 → 5 分钟后再问
    eng._turn_step(t + timedelta(minutes=4))
    assert len(judge.calls) == 1 and not eng.turn_on
    eng._turn_step(t + timedelta(minutes=5))                          # Jev 没答上 → 下一分钟就再问
    eng._turn_step(t + timedelta(minutes=6))
    assert len(judge.calls) == 3 and eng.turn_on
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE verdict='turn'").fetchone()[0] == 3


def test_rules_alone_confirm_by_default_and_jev_is_only_recorded():
    """默认 TURN_JEV_REQUIRED=False：规则满足就确认（快、明确），Jev 不认也照样确认，记下它的看法。"""
    for probs in ([0.3], [None]):
        judge = FakeTurnJudge(probs)
        eng, conn = _engine(judge)
        _feed(eng, TODAY_1009[:14])
        eng._turn_step(DAY.replace(hour=13, minute=10))
        assert eng.turn_on and len(judge.calls) == 1
        d = json.loads(conn.execute("SELECT detail FROM decisions WHERE verdict='turn'").fetchone()[0])
        assert d["confirmed"] and d["by"] == "规则" and d["jev_agrees"] is False


def test_engine_restores_breadth_and_confirmed_turn_after_restart(tmp_path):
    path = str(tmp_path / "j.sqlite")
    eng, conn = _engine(FakeTurnJudge([0.8]), db.connect(path))
    for b in _series(TODAY_1009[:14]):
        conn.execute("INSERT INTO market_breadth VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (b.ts.strftime("%Y-%m-%d %H:%M"), b.up, b.down, b.total, b.limit_up, b.limit_down, b.amount_ratio))
    conn.commit()
    eng._restore_turn(DAY.date())
    assert len(eng.breadth_series) == 14 and eng.breadth_series[-1].up == 1237
    eng._turn_step(DAY.replace(hour=13, minute=10))
    eng2, _ = _engine(FakeTurnJudge([]), db.connect(path))
    eng2._restore_turn(DAY.date())
    assert eng2.turn_on and eng2.turn_detail["confirmed"]


def test_engine_records_breadth_each_minute_with_volume_ratio():
    from factories import quote
    conn = db.connect(":memory:")
    t = DAY.replace(hour=10, minute=0)
    conn.execute("INSERT INTO turnover VALUES ('2026-10-08 10:00', 1e7)")
    eng = Engine(conn, Ledger(conn), Market(),
                 lambda codes: {"600001": quote("600001", pct=-10.0, ts=t, amount=1.2e7)},
                 lambda d: {}, {"600001": ""}, date(2026, 10, 8), "偏弱")
    eng.sell_step(t)
    assert conn.execute("SELECT * FROM market_breadth").fetchall() == [("2026-10-09 10:00", 0, 1, 1, 0, 1, 20.0)]
    assert eng.breadth_series[-1].limit_down == 1


def test_rebound_mode_turn_only_matters_when_sentiment_is_weak():
    assert rebound_mode("偏弱", turn=True) == "turn" and rebound_mode(None, turn=True) == "turn"
    assert rebound_mode("正常", turn=True) == "intraday"
    assert rebound_mode("冰点") == "none"
    assert buy_window_open("turn", time(13, 10)) and not buy_window_open("turn", time(14, 57))


# ---- Jev 确认 + 7x24 快讯 ----

class ScoreJev:
    def __init__(self, label, fail=False):
        self.label, self.fail, self.states = label, fail, []

    def ask(self, state, questions, **kw):
        self.states.append(state)
        if self.fail:
            raise JevError("down")
        (key, q), = questions.items()
        legend = {str(i): v for i, v in enumerate(q["criteria"])}
        idx = q["criteria"].index(self.label)
        probs = {k: (0.8 if int(k) == idx else 0.2) for k in legend}
        return {key: {"type": "score", "score": idx, "legend": legend, "probabilities": probs, "confidence": 0.7}}


def test_turn_judge_sends_trajectory_overseas_and_news():
    now = DAY.replace(hour=13, minute=10)
    flash = Flash({"财联社": lambda: [(now - timedelta(minutes=3), "央行开展逆回购"),
                                      (now - timedelta(hours=5), "很早的旧闻")],
                   "东方财富": lambda: [(now - timedelta(minutes=3), "央行开展逆回购")]}, log=lambda m: None)
    jev = ScoreJev(YES)
    s = _series()[:14]
    _, detail = turn.turn_check(s, now)
    prob = TurnJudge(jev, flash).confirm(s, detail, now, "偏弱", {"纳斯达克": 1.2})
    assert prob == 0.8
    st = jev.states[0]
    assert st["7x24快讯"] == ["13:07 财联社｜央行开展逆回购"]          # 去重、只要近 2 小时
    assert st["隔夜外围%"] == {"纳斯达克": 1.2} and st["情绪标签"] == "偏弱"
    assert any("红盘家数低于 1000 家" in x for x in st["短线经验（知识库）"])
    assert any("13:10 红盘 1237" in x for x in st["今日广度"])


def test_turn_judge_none_when_jev_fails_and_news_failure_is_tolerated():
    now = DAY.replace(hour=13, minute=10)

    def boom():
        raise OSError("down")
    flash = Flash({"财联社": boom}, log=lambda m: None)
    s = _series()[:14]
    _, detail = turn.turn_check(s, now)
    j = TurnJudge(ScoreJev(YES, fail=True), flash)
    assert j.confirm(s, detail, now, None, None) is None
    assert "Jev 没答上" in j.last_trace["note"] and j.last_trace["inputs"]["7x24快讯"] == []


def test_flash_caches_for_a_minute():
    calls = []
    clock = {"t": 0.0}
    now = DAY.replace(hour=13, minute=10)
    flash = Flash({"财联社": lambda: calls.append(1) or [(now, "x")]}, log=lambda m: None, clock=lambda: clock["t"])
    flash.recent(now)
    clock["t"] = 30
    flash.recent(now)
    assert len(calls) == 1
    clock["t"] = 61
    flash.recent(now)
    assert len(calls) == 2


def test_trajectory_samples_every_ten_minutes_plus_last():
    lines = trajectory(_series()[:14])
    assert lines[0].startswith("09:30") and lines[-1].startswith("13:10")
    assert not any(x.startswith("10:01") for x in lines)
