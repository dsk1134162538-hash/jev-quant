import json
from datetime import date, datetime

from factories import quote

from jevq import db
from jevq.data.closes import prev_closes_from_db, save_closes
from jevq.ledger import Ledger
from jevq.rules.board import WATCH_MAX, Candidate
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
# 首板条件：昨收（行情 pre_close=10.0）< round(前日收 * 1.1, 2)
PREV_CLOSES = {c: 9.5 for c in ["600001", "600002", "600003"]}


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
    """按 jevq.rules.board.leader 的规则（三项名次之和最小，并列取 pct 名次更小者）计算 AI
    题材当前的龙头代码，供测试断言用，避免在每个用例里手写期望值。"""
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

    result = prev_closes_from_db(conn, ["600001"], date(2026, 9, 23), PREV_DAY)
    assert result == {"600001": 8.5}


# ---- fix round 1 ----------------------------------------------------------

def test_full_account_defers_judge_then_judges_once_slot_frees():
    notice = Judge()
    news = Judge()
    strat, conn, ledger = make(notice=notice, news=news)
    quotes = hot_quotes()
    code = leader_code(quotes)
    filler = quote("900001", pct=-1.0, pre_close=10.0, ts=DAY)
    ledger.buy("board_main", filler, at(9, 30), "filler")

    strat.on_minute(quotes, at(9, 35))
    assert code in strat.watch
    assert notice.calls == []                      # 满仓：进观察池但不问 Jev

    conn.execute("DELETE FROM positions WHERE account='board_main' AND code='900001'")
    conn.commit()

    strat.on_minute(quotes, at(9, 36))
    assert notice.calls == [code]                   # 空位后恰好判断一次
    assert strat.verdicts[code][0] == "pass"

    strat.on_minute(quotes, at(9, 37))
    assert notice.calls == [code]                   # 已判断过，不重复


def test_full_account_skips_jev_calls_but_still_maintains_watch():
    notice = Judge()
    strat, conn, ledger = make(notice=notice)
    quotes = hot_quotes()
    code = leader_code(quotes)
    filler = quote("900001", pct=-1.0, pre_close=10.0, ts=DAY)
    ledger.buy("board_main", filler, at(9, 30), "filler")

    strat.on_minute(quotes, at(9, 35))

    assert code in strat.watch
    assert notice.calls == []
    assert code not in strat.verdicts


def test_restores_rejected_from_decisions_on_first_minute_of_day():
    strat, conn, _ = make()
    conn.executemany(
        "INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES (?, ?, ?, ?, ?)", [
            ("2026-09-23T09:31:00", "600002", "reject", "新增减持", "board"),
            ("2026-09-23T09:32:00", "600003", "error", "判断异常", "board"),
            ("2026-09-22T09:31:00", "600001", "reject", "昨天的", "board"),          # 昨天的不恢复
            ("2026-09-23T09:33:00", "600004", "reject", "低吸的否决", "lowbuy"),      # 别的策略不恢复
        ])
    conn.commit()

    strat.on_minute({}, at(10, 0))
    assert strat.rejected == {"600002", "600003"}


def test_board_reject_does_not_leak_into_lowbuy_engine():
    import test_engine as te

    notice = te.Judge()
    eng, ledger, _ = te.make(te.qs(**{"600002": -1.0}), notice=notice)
    eng.conn.execute(
        "INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES (?, ?, ?, ?, ?)",
        ("2026-09-23T09:31:00", "600002", "reject", "board 的否决", "board"))
    eng.conn.commit()

    eng.tick(te.at(10, 0))
    assert notice.calls == ["600002"]               # board 的否决不影响低吸判断
    assert ledger.holds("main", "600002")


def test_sells_exception_isolated_per_position():
    strat, conn, ledger = make()
    yesterday = DAY.replace(day=DAY.day - 1)
    a = quote("600001", pct=8.0, pre_close=10.0, ts=at(14, 0, yesterday))
    b = quote("600002", pct=8.0, pre_close=10.0, ts=at(14, 0, yesterday))
    ledger.buy("board_main", a, at(10, 0, yesterday), "manual")
    ledger.buy("board_shadow", b, at(10, 0, yesterday), "manual")

    real_sell = ledger.sell

    def flaky_sell(account, pos, q, ts, reason):
        if pos.code == "600001":
            raise RuntimeError("boom")
        return real_sell(account, pos, q, ts, reason)

    ledger.sell = flaky_sell

    blowup_a = quote("600001", pct=5.0, open_pct=10.0, pre_close=10.0, ts=at(10, 0))
    blowup_b = quote("600002", pct=5.0, open_pct=10.0, pre_close=10.0, ts=at(10, 0))
    fills = strat.on_minute({"600001": blowup_a, "600002": blowup_b}, at(10, 0))

    assert any(f.code == "600002" and f.account == "board_shadow" for f in fills)
    assert ledger.holds("board_main", "600001")       # 卖出异常，仍持有，没有卡死其它票
    assert not ledger.holds("board_shadow", "600002")


def test_on_fast_main_buy_exception_does_not_block_shadow_or_other_codes():
    q1 = quote("600001", pct=9.2, pre_close=10.0, ts=DAY)
    q2 = quote("600002", pct=9.2, pre_close=10.0, ts=DAY)
    strat, conn, ledger = make(fetch_quotes=lambda codes: {"600001": q1, "600002": q2})
    strat.watch = {
        "600001": Candidate("600001", "AI", 3, 4, {}),
        "600002": Candidate("600002", "AI", 3, 4, {}),
    }
    strat.verdicts = {
        "600001": ("pass", "ok", {"outcome": {"bought": False}}),
        "600002": ("pass", "ok", {"outcome": {"bought": False}}),
    }

    real_buy = ledger.buy

    def flaky_buy(account, q, ts, reason):
        if account == "board_main" and q.code == "600001":
            raise RuntimeError("boom")
        return real_buy(account, q, ts, reason)

    ledger.buy = flaky_buy

    strat.on_fast(at(9, 35))

    assert not ledger.holds("board_main", "600001")    # 主账户买入异常
    assert ledger.holds("board_shadow", "600001")       # 影子账户不受影响（先处理到的票占满仓位）
    assert ledger.holds("board_main", "600002")         # 主账户仍能买到下一只票，异常没有中断整个循环


def test_on_fast_buy_updates_trace_outcome_and_logs_second_decision_row():
    q = quote("600001", pct=9.2, pre_close=10.0, ts=DAY)
    strat, conn, ledger = make(fetch_quotes=lambda codes: {"600001": q})
    trace = {"strategy": "board", "candidate": {}, "notices": None, "news": None,
             "outcome": {"bought": False, "shares": None, "price": None}}
    strat.watch = {"600001": Candidate("600001", "AI", 3, 4, {})}
    strat.verdicts = {"600001": ("pass", "ok", trace)}

    strat.on_fast(at(9, 35))

    assert trace["outcome"]["bought"] is True
    assert trace["outcome"]["shares"] > 0
    assert trace["outcome"]["price"] == q.price
    rows = conn.execute(
        "SELECT verdict, detail, strategy FROM decisions WHERE code='600001' ORDER BY id").fetchall()
    assert rows[-1][0] == "pass"
    assert rows[-1][1].startswith("扫板")
    assert rows[-1][2] == "board"


def test_exit_pct_removes_candidate_from_watch():
    strat, conn, _ = make()
    strat.watch = {"600001": Candidate("600001", "AI", 3, 4, {})}
    strat.verdicts = {"600001": ("pass", "ok", {"outcome": {}})}

    dropped = {"600001": quote("600001", pct=5.9, pre_close=10.0, ts=at(10, 0))}
    strat.on_minute(dropped, at(10, 0))

    assert "600001" not in strat.watch
    assert "600001" not in strat.verdicts


def test_watch_max_caps_total_size_across_concepts():
    universe = {c: f"n{c}" for c in ["600001", "600002", "600003", "700001", "700002", "700003"]}
    concept_map = {"600001": ["AI"], "600002": ["AI"], "600003": ["AI"],
                   "700001": ["BIO"], "700002": ["BIO"], "700003": ["BIO"]}
    ret5 = {c: 5.0 for c in universe}
    prev_closes = {c: 9.5 for c in universe}
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    strat = BoardStrategy(conn, ledger, Judge(), Judge(), lambda codes: {}, universe, ret5,
                          concept_map, prev_closes, PREV_DAY)
    strat.watch = {f"F{i}": Candidate(f"F{i}", "OTHER", 3, 1, {}) for i in range(4)}

    quotes = {c: quote(c, pct=8.0, pre_close=10.0, ts=DAY) for c in universe}
    quotes.update({f"F{i}": quote(f"F{i}", pct=8.0, pre_close=10.0, ts=DAY) for i in range(4)})

    strat.on_minute(quotes, at(9, 35))
    assert len(strat.watch) == WATCH_MAX


def test_stale_quote_position_not_sold():
    strat, conn, ledger = make()
    yesterday = DAY.replace(day=DAY.day - 1)
    pos_q = quote("600001", pct=8.0, pre_close=10.0, ts=at(14, 0, yesterday))
    ledger.buy("board_main", pos_q, at(10, 0, yesterday), "manual")

    stale = quote("600001", pct=-5.0, pre_close=10.0, ts=datetime(2026, 9, 22, 15, 0))
    fills = strat.on_minute({"600001": stale}, at(9, 31, DAY))

    assert fills == []
    assert ledger.holds("board_main", "600001")


def test_on_fast_stale_quote_not_bought():
    stale_q = quote("600001", pct=9.2, pre_close=10.0, ts=datetime(2026, 9, 22, 10, 0))
    strat, conn, ledger = make(fetch_quotes=lambda codes: {"600001": stale_q})
    strat.watch = {"600001": Candidate("600001", "AI", 3, 4, {})}
    strat.verdicts = {"600001": ("pass", "ok", {"outcome": {"bought": False}})}

    fills = strat.on_fast(at(9, 35))

    assert fills == []
    assert not ledger.holds("board_main", "600001")
    assert not ledger.holds("board_shadow", "600001")


class RaisingJudge:
    def __init__(self):
        self.calls = []

    def check(self, code, *args):
        self.calls.append(code)
        raise RuntimeError("boom")


def test_judge_exception_becomes_error_and_rejected():
    notice = RaisingJudge()
    strat, conn, _ = make(notice=notice)
    quotes = hot_quotes()
    code = leader_code(quotes)

    strat.on_minute(quotes, at(9, 35))

    assert code not in strat.watch
    assert code in strat.rejected
    row = conn.execute("SELECT verdict, detail, strategy FROM decisions WHERE code=?", (code,)).fetchone()
    assert row[0] == "error"
    assert "判断异常" in row[1]
    assert row[2] == "board"
