from datetime import date, datetime, time

from factories import quote
from jevq.rules.board import (Candidate, hot_concepts, in_scan_window, is_first_board, leader, limit_price,
                              scan_trigger, watch_candidates)

TODAY = date(2026, 9, 24)
TS = datetime(2026, 9, 24, 10, 0)


def q(code, pct, amount=1e8, **kw):
    return quote(code, pct=pct, amount=amount, ts=TS, **kw)


def test_limit_and_first_board():
    assert limit_price(10.0) == 11.0
    assert is_first_board(10.5, 10.0)          # 昨日 +5%
    assert not is_first_board(11.0, 10.0)      # 昨日涨停
    assert not is_first_board(None, 10.0)


def test_hot_concepts_needs_three_strong_members():
    quotes = {"600001": q("600001", 6), "600002": q("600002", 5.5), "600003": q("600003", 5.0),
              "600004": q("600004", 4.9), "000001": q("000001", 8)}
    cmap = {"600001": ["机器人"], "600002": ["机器人"], "600003": ["机器人"], "600004": ["机器人"],
            "000001": ["芯片"]}
    assert hot_concepts(quotes, cmap, TODAY) == {"机器人": ["600001", "600002", "600003"]}


def test_hot_concepts_ignores_stale_and_st():
    stale = quote("600001", pct=8, ts=datetime(2026, 9, 23, 15, 0))
    quotes = {"600001": stale, "600002": q("600002", 6, name="ST甲"), "600003": q("600003", 6),
              "600004": q("600004", 6)}
    cmap = {c: ["机器人"] for c in quotes}
    assert hot_concepts(quotes, cmap, TODAY) == {}


def test_leader_sums_three_ranks():
    quotes = {"600001": q("600001", 9, amount=3e8), "600002": q("600002", 8, amount=5e8),
              "600003": q("600003", 6, amount=1e8)}
    ret5 = {"600001": 10.0, "600002": 20.0, "600003": 30.0}
    c = leader("机器人", list(quotes), quotes, ret5)
    # 600001: pct1+amt2+ret3=6；600002: 2+1+2=5；600003: 3+3+1=7
    assert c.code == "600002" and c.score == 5 and c.ranks == {"pct": 2, "amount": 1, "ret5": 2}


def test_watch_candidates_filters():
    quotes = {"600001": q("600001", 8.5, amount=9e8), "600002": q("600002", 6), "600003": q("600003", 5.5)}
    cmap = {c: ["机器人"] for c in quotes}
    ret5 = {c: 1.0 for c in quotes}
    prev = {"600001": 9.5}                                       # 前日收；昨收取行情 pre_close=10.0
    got = watch_candidates(quotes, cmap, ret5, prev, TODAY)
    assert [c.code for c in got] == ["600001"] and got[0].heat == 3
    prev_limit = {"600001": 9.0}                               # 昨收 10.0 >= 9.9 → 昨日涨停，不是首板
    assert watch_candidates(quotes, cmap, ret5, prev_limit, TODAY) == []
    sealed = dict(quotes, **{"600001": q("600001", 10.0, amount=9e8)})   # 已封死
    assert watch_candidates(sealed, cmap, ret5, prev, TODAY) == []


def test_watch_candidates_excludes_recent_limit_up_leader():
    quotes = {"600001": q("600001", 8.5, amount=9e8), "600002": q("600002", 6), "600003": q("600003", 5.5)}
    cmap = {c: ["机器人"] for c in quotes}
    ret5 = {c: 1.0 for c in quotes}
    prev = {"600001": 9.5}
    # 600001 是龙头，正常能进观察池
    assert [c.code for c in watch_candidates(quotes, cmap, ret5, prev, TODAY)] == ["600001"]
    # 但它近 10 日涨停过：题材整个跳过，不取第二名
    assert watch_candidates(quotes, cmap, ret5, prev, TODAY, exclude={"600001"}) == []


def test_scan_trigger_window_and_sealed():
    assert in_scan_window(time(9, 30)) and in_scan_window(time(10, 29)) and not in_scan_window(time(10, 30))
    assert scan_trigger(q("600001", 9.2), datetime(2026, 9, 24, 9, 45))
    assert not scan_trigger(q("600001", 8.9), datetime(2026, 9, 24, 9, 45))
    assert not scan_trigger(q("600001", 10.0), datetime(2026, 9, 24, 9, 45))   # 已封死
    assert not scan_trigger(q("600001", 9.5), datetime(2026, 9, 24, 13, 30))   # 窗口外


def _touched_limit(code, pct):
    from dataclasses import replace
    x = q(code, pct, amount=9e8)
    return replace(x, high=x.limit_up)


def test_already_touched_limit_is_not_scanned_again():
    broke = _touched_limit("600001", 9.5)                       # 摸过 11.00 又回落到 10.95
    assert broke.price == 10.95 and broke.high == broke.limit_up
    assert not scan_trigger(broke, datetime(2026, 9, 24, 9, 45))
    quotes = {"600001": broke, "600002": q("600002", 6), "600003": q("600003", 5.5)}
    cmap = {c: ["机器人"] for c in quotes}
    assert watch_candidates(quotes, cmap, {c: 1.0 for c in quotes}, {"600001": 9.5}, TODAY) == []


def test_first_board_uses_live_pre_close_not_db_yesterday():
    # 行情 pre_close=10.0，前日收 9.0 → 昨天其实涨停了（10.0 >= 9.9），就算库里没有昨收也要排除
    quotes = {"600001": q("600001", 8.5, amount=9e8), "600002": q("600002", 6), "600003": q("600003", 5.5)}
    cmap = {c: ["机器人"] for c in quotes}
    assert watch_candidates(quotes, cmap, {c: 1.0 for c in quotes}, {"600001": 9.0}, TODAY) == []
    assert watch_candidates(quotes, cmap, {c: 1.0 for c in quotes}, {}, TODAY) == []   # 缺前日收 → 保守不算


def test_sentiment_gate_blocks_on_any_weak_reading_and_explains_why():
    from jevq.rules.board import sentiment_gate
    ok = {"涨停家数": 57, "跌停家数": 9, "炸板率%": 12.3, "最高连板": 6}
    assert sentiment_gate(ok) == (False, "炸板率 12.3%、跌停 9 家、最高 6 连板")
    blocked, why = sentiment_gate({**ok, "炸板率%": 30.1})
    assert blocked is True and "炸板率 30.1% 高于 30%" in why
    assert sentiment_gate({**ok, "炸板率%": 30.0})[0] is False                # 刚好等于线不算
    assert sentiment_gate({**ok, "跌停家数": 11}) == (True, "跌停 11 家多于 10 家")
    assert sentiment_gate({**ok, "最高连板": 2}) == (True, "最高只有 2 连板，不到 3 板")
    blocked, why = sentiment_gate({**ok, "炸板率%": 45.0, "跌停家数": 20})
    assert blocked is True and "炸板率" in why and "跌停 20 家" in why


def test_sentiment_gate_unknown_when_stats_missing():
    from jevq.rules.board import sentiment_gate
    assert sentiment_gate(None) == (None, "没有上一交易日的情绪统计")
    assert sentiment_gate({}) == (None, "没有上一交易日的情绪统计")
    assert sentiment_gate({"涨停家数": 50, "跌停家数": 3})[0] is None           # 缺炸板率/连板高度：不下结论
