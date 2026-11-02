from research import first_board_history as fb


def _series(code, closes, last=None):
    """平盘的日 K，最后几根手工指定：(open, close, high, low)。"""
    rows, day = [], 1
    for c in closes:
        rows.append((f"2026-06-{day:02d}", c, c, c, c, 1000.0))
        day += 1
    for o, c, h, lo in (last or []):
        rows.append((f"2026-06-{day:02d}", o, c, h, lo, 1000.0))
        day += 1
    return rows


def test_candidates_follow_the_scan_rules_without_survivor_bias():
    flat = [10.0] * 22
    bars = {
        # 第 23 天摸 9%（10.9）后炸板收 10.5，次日开 10.2：照样算候选，买在 10.9
        "600001": _series("600001", flat, [(10.0, 10.5, 10.95, 9.9), (10.2, 10.0, 10.3, 9.9)]),
        # 一字板买不到
        "600002": _series("600002", flat, [(11.0, 11.0, 11.0, 11.0), (11.5, 11.2, 11.6, 11.0)]),
        # 开盘就 +9.5%：按开盘价买，封板
        "600003": _series("600003", flat, [(10.95, 11.0, 11.0, 10.9), (11.4, 11.6, 11.8, 11.2)]),
        # 前 10 天收过涨停，不算首板
        "600004": _series("600004", [10.0] * 15 + [11.0] + [11.0] * 6, [(11.0, 12.1, 12.1, 11.0), (12.5, 12.3, 12.6, 12.0)]),
    }
    bars.update({f"60{n:04d}": _series("x", [10.0] * 24) for n in range(100, 1200)})   # 撑起全市场家数
    data = fb.build(bars, {"600001": "电子"}, {}, {"600001": 1e7})
    day = "2026-06-23"
    cands = {c["code"]: c for c in data["cands"][day]}
    assert set(cands) == {"600001", "600003"}
    assert cands["600001"]["sealed"] is False and cands["600001"]["ret_open%"] == round((10.2 / 10.9 - 1) * 100 - fb.FEE, 2)
    assert cands["600003"]["ret_open%"] == round((11.4 / 10.95 - 1) * 100 - fb.FEE, 2)
    assert cands["600001"]["industry"] == "电子" and cands["600001"]["floatMcap亿"] == 1.0
    assert data["market"][day]["昨日涨停"] == 0


def test_day_result_and_equity_stats():
    cands = [{"code": "a", "ret_open%": 4.0}, {"code": "b", "ret_open%": -2.0}]
    assert fb.day_result(cands, ["a", "b"]) == 1.0 and fb.day_result(cands, []) == 0.0
    s = fb.equity_stats([2.0, 0.0, -3.0, 1.0])
    assert s == {"total%": 0.0, "tradeDays": 3, "days": 4, "winRate": 0.667, "avgPerTrade%": 0.0, "maxDD%": -3.0}
