import json
from datetime import date, datetime

from factories import quote

from jevq import db
from jevq.data.closes import prev_closes_from_db, save_closes
from jevq.ledger import Ledger
from jevq.rules.board import Candidate
from jevq.strategies.board import BoardStrategy

DAY = datetime(2026, 9, 23)
PREV_DAY = date(2026, 9, 22)


def at(h, m, d=DAY):
    return d.replace(hour=h, minute=m)


class Judge:
    def __init__(self, verdicts=None):
        self.verdicts, self.calls = verdicts or {}, []

    def check(self, code, *args):
        self.calls.append(code)
        return self.verdicts.get(code, ("pass", "ok"))


UNIVERSE = {c: f"股票{c}" for c in ["600001", "600002", "600003", "600004"]}
CONCEPT_MAP = {"600001": ["AI"], "600002": ["AI"], "600003": ["AI"], "600004": []}
RET5 = {"600001": 5.0, "600002": 10.0, "600003": 1.0, "600004": 1.0}
# 首板条件：昨收 < round(前日收 * 1.1, 2)
PREV_CLOSES = {c: (9.5, 9.0) for c in ["600001", "600002", "600003"]}


def hot_quotes(now=DAY):
    """题材 AI 内三只强势股（pct >= HOT_PCT），互相竞争龙头分。"""
    return {
        "600001": quote("600001", pct=8.0, pre_close=10.0, amount=3e7, ts=now),
        "600002": quote("600002", pct=9.0, pre_close=10.0, amount=1e7, ts=now),
        "600003": quote("600003", pct=7.0, pre_close=10.0, amount=2e7, ts=now),
        "600004": quote("600004", pct=1.0, pre_close=10.0, ts=now),
    }


def make(notice=None, news=None, fetch_quotes=None, enabled=True):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)

    def default_fetch(codes):
        raise AssertionError("fetch_quotes 不应在未设置时被调用")

    strat = BoardStrategy(conn, ledger, notice or Judge(), news or Judge(),
                          fetch_quotes or default_fetch, UNIVERSE, RET5, CONCEPT_MAP,
                          PREV_CLOSES, PREV_DAY, enabled=enabled)
    return strat, conn, ledger


def leader_code(quotes):
    """三项名次之和最小者：600002 (pct=9,rank1; amount=1e7,rank3; ret5=10,rank1 -> 5)
    600001 (pct=8,rank2; amount=3e7,rank1; ret5=5,rank2 -> 5) -> 并列，取 rp 更小者(600001 rp=2 vs 600002 rp=1)
    实际按代码算出后用测试自己核对，这里返回计算得到的龙头代码。"""
    from jevq.rules.board import hot_concepts, leader
    hot = hot_concepts(quotes, CONCEPT_MAP, DAY.date())
    cand = leader("AI", hot["AI"], quotes, RET5)
    return cand.code


def test_watch_enter_triggers_one_jev_check_no_repeat_next_minute():
    notice = Judge()
    news = Judge()
    strat, conn, _ = make(notice=notice, news=news)
    quotes = hot_quotes()
    code = leader_code(quotes)

    strat.on_minute(quotes, at(9, 35))
    assert code in strat.watch
    assert notice.calls == [code]
    assert news.calls == [code]
    assert strat.verdicts[code][0] == "pass"

    strat.on_minute(quotes, at(9, 36))
    assert notice.calls == [code]                 # 不重复判断


def test_rejected_leaves_watch_and_not_readded_same_day():
    quotes = hot_quotes()
    code = leader_code(quotes)
    notice = Judge({code: ("reject", "新增减持")})
    strat, conn, _ = make(notice=notice)

    strat.on_minute(quotes, at(9, 35))
    assert code not in strat.watch
    assert code in strat.rejected

    strat.on_minute(quotes, at(9, 36))
    assert code not in strat.watch
    assert notice.calls == [code]                  # 当天不再进入，不重复判断


def test_on_fast_buys_main_and_shadow_when_unsealed_and_triggered():
    q = quote("600001", pct=9.2, pre_close=10.0, ts=DAY)
    strat, conn, ledger = make(fetch_quotes=lambda codes: {"600001": q})
    strat.watch = {"600001": Candidate("600001", "AI", 3, 4, {"pct": 1, "amount": 1, "ret5": 1})}
    strat.verdicts = {"600001": ("pass", "ok", {"outcome": {"bought": False}})}

    fills = strat.on_fast(at(9, 35))
    assert {f.account for f in fills} == {"board_main", "board_shadow"}
    assert ledger.holds("board_main", "600001")
    assert ledger.holds("board_shadow", "600001")


def test_on_fast_no_buy_when_sealed():
    q = quote("600001", pct=10.0, pre_close=10.0, ts=DAY)     # price == limit_up
    strat, conn, ledger = make(fetch_quotes=lambda codes: {"600001": q})
    strat.watch = {"600001": Candidate("600001", "AI", 3, 4, {})}
    strat.verdicts = {"600001": ("pass", "ok", {"outcome": {"bought": False}})}

    fills = strat.on_fast(at(9, 35))
    assert fills == []
    assert not ledger.holds("board_main", "600001")
    assert not ledger.holds("board_shadow", "600001")
    assert strat.verdicts["600001"][2]["sealed_at_snapshot"] is True


def test_on_fast_no_buy_outside_scan_window():
    q = quote("600001", pct=9.2, pre_close=10.0, ts=DAY)
    strat, conn, ledger = make(fetch_quotes=lambda codes: {"600001": q})
    strat.watch = {"600001": Candidate("600001", "AI", 3, 4, {})}
    strat.verdicts = {"600001": ("pass", "ok", {"outcome": {"bought": False}})}

    fills = strat.on_fast(at(13, 30))
    assert fills == []
    assert not ledger.holds("board_main", "600001")
    assert not ledger.holds("board_shadow", "600001")


def test_on_fast_main_rejected_only_shadow_buys():
    q = quote("600001", pct=9.2, pre_close=10.0, ts=DAY)
    strat, conn, ledger = make(fetch_quotes=lambda codes: {"600001": q})
    strat.watch = {"600001": Candidate("600001", "AI", 3, 4, {})}
    strat.verdicts = {"600001": ("reject", "新增减持", {"outcome": {"bought": False}})}

    fills = strat.on_fast(at(9, 35))
    assert {f.account for f in fills} == {"board_shadow"}
    assert not ledger.holds("board_main", "600001")
    assert ledger.holds("board_shadow", "600001")


def test_board_main_position_sold_on_blowup_next_day():
    strat, conn, ledger = make()
    yesterday = quote("600001", pct=8.0, pre_close=10.0, ts=at(14, 0, DAY.replace(day=DAY.day - 1)))
    ledger.buy("board_main", yesterday, at(10, 0, DAY.replace(day=DAY.day - 1)), "manual")
    ledger.buy("main", yesterday, at(10, 0, DAY.replace(day=DAY.day - 1)), "manual")

    # 炸板：盘中摸过涨停(open=limit_up) 但当前价未封死
    blowup = quote("600001", pct=5.0, open_pct=10.0, pre_close=10.0, ts=at(10, 0))
    fills = strat.on_minute({"600001": blowup}, at(10, 0))

    board_fills = [f for f in fills if f.account == "board_main"]
    assert board_fills and board_fills[0].reason == "炸板"
    assert not ledger.holds("board_main", "600001")
    assert ledger.holds("main", "600001")          # 低吸账户不受 BoardStrategy 影响


def test_disabled_only_sells_no_watch_selection():
    notice = Judge()
    strat, conn, ledger = make(notice=notice, enabled=False)
    yesterday = quote("600001", pct=8.0, pre_close=10.0, ts=at(14, 0, DAY.replace(day=DAY.day - 1)))
    ledger.buy("board_main", yesterday, at(10, 0, DAY.replace(day=DAY.day - 1)), "manual")
    blowup = quote("600001", pct=5.0, open_pct=10.0, pre_close=10.0, ts=at(10, 0))

    quotes = {**hot_quotes(), "600001": blowup}
    fills = strat.on_minute(quotes, at(10, 0))

    assert any(f.account == "board_main" and f.reason == "炸板" for f in fills)
    assert strat.watch == {}
    assert notice.calls == []


def test_on_fast_fetch_exception_returns_empty_and_recovers_next_call():
    calls = {"n": 0}

    def flaky(codes):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"600001": quote("600001", pct=9.2, pre_close=10.0, ts=DAY)}

    strat, conn, ledger = make(fetch_quotes=flaky)
    strat.watch = {"600001": Candidate("600001", "AI", 3, 4, {})}
    strat.verdicts = {"600001": ("pass", "ok", {"outcome": {"bought": False}})}

    assert strat.on_fast(at(9, 35)) == []
    assert "600001" in strat.watch                # 观察池不受影响

    fills = strat.on_fast(at(9, 36))
    assert any(f.account == "board_main" for f in fills)


def test_decisions_have_board_strategy_and_trace_fields():
    notice = Judge()
    news = Judge()
    strat, conn, _ = make(notice=notice, news=news)
    quotes = hot_quotes()
    code = leader_code(quotes)

    strat.on_minute(quotes, at(9, 35))
    rows = conn.execute("SELECT code, verdict, strategy, trace FROM decisions").fetchall()
    assert all(strategy == "board" for _, _, strategy, _ in rows)
    trace = json.loads(next(t for c, _, _, t in rows if c == code))
    assert trace["strategy"] == "board"
    assert trace["candidate"]["concept"] == "AI"
    assert "heat" in trace["candidate"] and "ranks" in trace["candidate"]


def test_prev_closes_from_db_returns_latest_two_of_three_days():
    conn = db.connect(":memory:")
    save_closes(conn, date(2026, 9, 18), {"600001": 8.0})
    save_closes(conn, date(2026, 9, 19), {"600001": 8.5})
    save_closes(conn, date(2026, 9, 22), {"600001": 9.0})

    result = prev_closes_from_db(conn, ["600001"], date(2026, 9, 23))
    assert result == {"600001": (9.0, 8.5)}
