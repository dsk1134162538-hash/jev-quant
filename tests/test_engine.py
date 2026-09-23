import json
from datetime import date, datetime

import pytest

import jevq.engine as engine_mod
from factories import quote
from jevq import db
from jevq.engine import Engine
from jevq.ledger import Ledger

DAY = datetime(2026, 9, 23)


def at(h, m, d=DAY):
    return d.replace(hour=h, minute=m)


class Judge:
    def __init__(self, verdicts=None):
        self.verdicts, self.calls = verdicts or {}, []

    def check(self, code, *args):
        self.calls.append(code)
        return self.verdicts.get(code, ("pass", "ok"))


class Market:
    def __init__(self, label):
        self.label = label

    def sentiment(self, stats):
        return self.label


def make(quotes, notice=None, news=None, sentiment="正常", overseas="中性", ret5=None):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    feed = {"quotes": quotes}

    def fetch(codes):
        q = feed["quotes"]
        if isinstance(q, Exception):
            raise q
        return {c: x for c, x in q.items() if c in codes}

    universe = {c: "x" for c in ["600001", "600002", "600003", "600004"]}
    ret5 = ret5 or {"600001": 5.0, "600002": 15.0, "600003": 10.0, "600004": 1.0}
    eng = Engine(conn, ledger, notice or Judge(), news or Judge(), Market(sentiment),
                 fetch, lambda d: {"涨停家数": 50}, universe, ret5, date(2026, 9, 22), overseas)
    return eng, ledger, feed


def qs(**pcts):
    return {c: quote(c, pct=p) for c, p in pcts.items()}


def test_main_skips_rejected_shadow_buys_top_ranked():
    quotes = qs(**{"600001": -1.0, "600002": -2.0, "600003": -0.5, "600004": 2.0})
    notice = Judge({"600002": ("reject", "新增减持")})
    eng, ledger, _ = make(quotes, notice=notice)
    eng.tick(at(10, 0))
    assert [p.code for p in ledger.positions("shadow")] == ["600002"]
    assert sorted(p.code for p in ledger.positions("main")) == ["600003"]
    rows = eng.conn.execute("SELECT code, verdict FROM decisions").fetchall()
    assert ("600002", "reject") in rows


def test_rejected_not_rechecked_same_day():
    notice = Judge({"600002": ("reject", "x")})
    eng, _, _ = make(qs(**{"600002": -1.0}), notice=notice)
    eng.tick(at(10, 0))
    eng.tick(at(10, 1))
    assert notice.calls == ["600002"]


def test_error_verdict_not_rechecked_same_day():
    notice = Judge({"600002": ("error", "Jev 不可用")})
    eng, ledger, _ = make(qs(**{"600002": -1.0}), notice=notice)
    eng.tick(at(10, 0))
    notice.verdicts = {}
    eng.tick(at(10, 1))
    assert notice.calls == ["600002"]
    assert not ledger.holds("main", "600002")


def test_news_checked_only_after_notice_passes():
    notice = Judge({"600002": ("reject", "x")})
    news = Judge({"600003": ("reject", "消息面偏空")})
    eng, ledger, _ = make(qs(**{"600002": -1.0, "600003": -1.0}), notice=notice, news=news)
    eng.tick(at(10, 0))
    assert news.calls == ["600003"]
    assert ledger.positions("main") == []


@pytest.mark.parametrize("sentiment,overseas", [("冰点", "中性"), ("正常", "偏弱")])
def test_tail_mode_waits_until_1450(sentiment, overseas):
    eng, ledger, _ = make(qs(**{"600001": -1.0}), sentiment=sentiment, overseas=overseas)
    eng.tick(at(10, 0))
    assert ledger.positions("main") == []
    eng.tick(at(14, 50))
    assert ledger.holds("main", "600001")


def test_next_day_sell_on_high_open():
    eng, ledger, feed = make(qs(**{"600001": -1.0}))
    eng.tick(at(10, 0))
    feed["quotes"] = {"600001": quote("600001", pct=1.5, open_pct=1.5)}
    fills = eng.tick(at(9, 31, datetime(2026, 9, 24)))
    assert {(f.account, f.side, f.reason) for f in fills} == {
        ("main", "sell", "高开止盈"), ("shadow", "sell", "高开止盈")}


def test_three_fetch_failures_halt_new_buys():
    eng, ledger, feed = make(RuntimeError("down"))
    for m in range(3):
        assert eng.tick(at(10, m)) == []
    assert eng.halted
    feed["quotes"] = qs(**{"600001": -1.0})
    eng.tick(at(10, 5))
    assert ledger.positions("main") == [] and ledger.positions("shadow") == []


def test_turnover_ratio_vs_previous_day_same_minute():
    eng, _, _ = make(qs(**{"600001": -1.0}))
    eng.conn.execute("INSERT INTO turnover VALUES ('2026-09-22 10:00', 5000000)")
    stats = eng._market_stats(at(10, 0), eng._fetch(at(10, 0)))
    assert stats["成交额较昨日同期%"] == 100.0          # 1e7 vs 5e6


def test_error_breaker_stops_checking_after_max_errors_per_tick():
    quotes = qs(**{"600001": -1.0, "600002": -1.0, "600003": -1.0, "600004": -1.0})
    notice = Judge({c: ("error", "x") for c in quotes})
    eng, ledger, _ = make(quotes, notice=notice)
    eng.tick(at(10, 0))
    # ranked by ret5 desc: 600002(15) 600003(10) 600001(5) 600004(1) -> breaker stops at 3
    assert notice.calls == ["600002", "600003", "600001"]
    assert ledger.positions("main") == []


def test_unaffordable_candidate_skipped_without_judge_call():
    quotes = {**qs(**{"600002": -1.0}), "600001": quote("600001", pct=0.0, pre_close=310.0)}
    ret5 = {"600001": 20.0, "600002": 10.0, "600003": 10.0, "600004": 1.0}
    notice = Judge()
    eng, ledger, _ = make(quotes, notice=notice, ret5=ret5)
    eng.tick(at(10, 0))
    assert notice.calls == ["600002"]
    assert not ledger.holds("main", "600001")
    assert ledger.holds("main", "600002")


class RaisingJudge:
    def __init__(self):
        self.calls = []

    def check(self, code, *args):
        self.calls.append(code)
        raise RuntimeError("boom")


def test_judge_exception_treated_as_error_and_rejected_same_day():
    notice = RaisingJudge()
    eng, ledger, _ = make(qs(**{"600002": -1.0}), notice=notice)
    eng.tick(at(10, 0))
    eng.tick(at(10, 1))
    assert notice.calls == ["600002"]
    row = eng.conn.execute(
        "SELECT verdict, detail FROM decisions WHERE code='600002'").fetchone()
    assert row[0] == "error"
    assert "判断异常" in row[1]
    assert not ledger.holds("main", "600002")


def test_halted_still_sells_held_position():
    eng, ledger, feed = make(qs(**{"600001": -1.0}))
    eng.tick(at(10, 0))
    assert ledger.holds("main", "600001")
    eng.halted = True
    feed["quotes"] = {"600001": quote("600001", pct=1.5, open_pct=1.5)}
    fills = eng.tick(at(9, 31, datetime(2026, 9, 24)))
    assert any(f.code == "600001" and f.side == "sell" for f in fills)
    assert not ledger.holds("main", "600001")


def test_held_code_outside_universe_still_gets_quoted_and_sold():
    eng, ledger, feed = make(qs(**{"600001": -1.0}))
    outside = quote("700001", pct=-1.0)
    ledger.buy("main", outside, at(10, 0), "manual")
    feed["quotes"] = {**feed["quotes"], "700001": quote("700001", pct=1.5, open_pct=1.5)}
    fills = eng.tick(at(9, 31, datetime(2026, 9, 24)))
    assert any(f.code == "700001" and f.side == "sell" for f in fills)
    assert not ledger.holds("main", "700001")


def test_max_checks_per_tick_caps_judge_calls(monkeypatch):
    monkeypatch.setattr(engine_mod, "MAX_CHECKS_PER_TICK", 2)
    quotes = qs(**{"600001": -1.0, "600002": -1.0, "600003": -1.0, "600004": -1.0})
    notice = Judge({c: ("reject", "x") for c in quotes})
    eng, ledger, _ = make(quotes, notice=notice)
    eng.tick(at(10, 0))
    assert len(notice.calls) == 2
    assert ledger.positions("main") == []


def test_main_decision_trace():
    eng, ledger, _ = make(qs(**{"600001": -1.0, "600002": -2.0}))
    eng.tick(at(10, 0))
    rows = eng.conn.execute("SELECT code, trace FROM decisions WHERE code != '' ORDER BY id").fetchall()
    traces = {code: json.loads(t) for code, t in rows}
    t = traces["600002"]
    assert t["candidate"]["rank"] == 1 and t["candidate"]["of"] == 2 and t["candidate"]["ret5"] == 15.0
    assert t["market"]["mode"] == "intraday" and t["market"]["sentiment"] == "正常"
    assert t["outcome"]["bought"] is True and t["outcome"]["shares"] > 0
    assert t["notices"] is None and t["news"] is None     # 测试替身没有 last_trace


def test_rejected_decision_trace_has_no_news():
    class NoticeWithTrace(Judge):
        last_trace = [{"title": "减持", "label": "新增减持", "mine": True}]
    news = Judge()
    news.last_trace = {"label": "中性"}
    eng, _, _ = make(qs(**{"600002": -1.0}), notice=NoticeWithTrace({"600002": ("reject", "新增减持")}), news=news)
    eng.tick(at(10, 0))
    t = json.loads(eng.conn.execute("SELECT trace FROM decisions WHERE code='600002'").fetchone()[0])
    assert t["notices"][0]["label"] == "新增减持" and t["news"] is None
    assert t["outcome"]["bought"] is False


class JevErrorJudge(RaisingJudge):
    def check(self, code, *args):
        self.calls.append(code)
        from jevq.judge.jev import JevError
        raise JevError("down")


def test_jev_error_from_judge_becomes_error_verdict_not_exception():
    notice = JevErrorJudge()
    eng, ledger, _ = make(qs(**{"600002": -1.0}), notice=notice)
    eng.tick(at(10, 0))
    row = eng.conn.execute("SELECT verdict FROM decisions WHERE code='600002'").fetchone()
    assert row[0] == "error"
    assert not ledger.holds("main", "600002")


def test_stale_position_sold_at_open():
    eng, ledger, feed = make(qs(**{"600001": 0.0}))
    ledger.buy("main", quote("600001", pct=-1.0), datetime(2026, 9, 21, 10, 0), "old")
    feed["quotes"] = {"600001": quote("600001", pct=0.0, open_pct=0.0)}
    fills = eng.tick(at(9, 31))                           # engine prev_day = 09-22, buy_date 09-21
    assert [(f.code, f.side, f.reason) for f in fills if f.account == "main" and f.side == "sell"] == [
        ("600001", "sell", "隔夜超期")]


def test_stale_quote_position_not_sold():
    eng, ledger, feed = make(qs(**{"600001": -1.0}))
    ledger.buy("main", quote("600001", pct=-1.0), at(10, 0), "x")
    feed["quotes"] = {"600001": quote("600001", pct=-5.0, ts=datetime(2026, 9, 23, 15, 0))}
    fills = eng.tick(at(9, 31, datetime(2026, 9, 24)))
    assert fills == []
    assert ledger.holds("main", "600001")


def test_fresh_quote_position_sold():
    eng, ledger, feed = make(qs(**{"600001": -1.0}))
    ledger.buy("main", quote("600001", pct=-1.0), at(10, 0), "x")
    feed["quotes"] = {"600001": quote("600001", pct=-5.0, ts=datetime(2026, 9, 24, 9, 31))}
    fills = eng.tick(at(9, 31, datetime(2026, 9, 24)))
    assert [(f.code, f.reason) for f in fills] == [("600001", "止损")]


def test_stale_candidate_not_bought():
    eng, ledger, feed = make(qs(**{"600001": -1.0}))
    feed["quotes"] = {"600001": quote("600001", pct=-1.0, ts=datetime(2026, 9, 22, 10, 0))}
    eng.tick(at(10, 0))
    assert ledger.positions("main") == [] and ledger.positions("shadow") == []


def test_stale_before_none_disables_overnight_rule():
    eng, ledger, feed = make(qs(**{"600001": 0.0}))
    eng.stale_before = None
    ledger.buy("main", quote("600001", pct=-1.0), datetime(2026, 9, 21, 10, 0), "old")
    feed["quotes"] = {"600001": quote("600001", pct=0.0, open_pct=0.0, ts=at(9, 31))}
    fills = eng.tick(at(9, 31))
    assert [f for f in fills if f.side == "sell"] == []
    assert ledger.holds("main", "600001")


def test_rejected_restored_from_todays_decisions():
    notice = Judge()
    eng, ledger, _ = make(qs(**{"600002": -1.0, "600003": -1.0}), notice=notice)
    eng.conn.executemany("INSERT INTO decisions(ts, code, verdict, detail) VALUES (?, ?, ?, ?)", [
        ("2026-09-23T09:40:00", "600002", "reject", "x"),
        ("2026-09-23T09:41:00", "600003", "error", "x"),
        ("2026-09-22T10:00:00", "600001", "reject", "昨天的"),
        ("2026-09-23T09:42:00", "", "error", "市场"),
    ])
    eng.conn.commit()
    eng.tick(at(10, 0))
    assert notice.calls == []
    assert eng.rejected == {"600002", "600003"}
