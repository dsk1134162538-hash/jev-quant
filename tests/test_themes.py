"""题材梯队 / 阶段 / 补涨候选池（dj 2026-11-03）。数据是同花顺涨停揭秘：每只涨停股的原因题材（用 + 分隔）和连板描述。
research/theme_ladder.py（一年真实题材）：知识库认为「好」的一致高潮反而次日更差；题材涨停家数回落的分歧日、
龙头 ≥5 板的题材里小幅高开的首板（补涨）相对更好。"""
from jevq.rules.themes import bz_candidates, bz_pool, ladder, parse_boards, phase, themes_of


def _r(code, name, reason, hd="首板"):
    return {"code": code, "name": name, "reason": reason, "high_days": hd}


def test_parse_boards_and_themes():
    assert [parse_boards(x) for x in ("首板", "2天2板", "5天3板", "", None)] == [1, 2, 3, 1, 1]
    assert themes_of("机器人+业绩预增+算力 + 年报增长") == ["机器人", "算力"]          # 业绩类不是题材
    assert themes_of("") == []


def test_ladder_counts_levels_leaders_and_orders_themes():
    rows = [_r("600001", "甲", "机器人+算力", "5天5板"), _r("600002", "乙", "机器人", "2天2板"),
            _r("600003", "丙", "机器人", "首板"), _r("600004", "丁", "机器人", "首板"),
            _r("000005", "戊", "算力", "3天3板"), _r("600006", "己", "消费", "首板")]
    got = ladder(rows, top=5)
    by = {t["theme"]: t for t in got}
    assert [t["theme"] for t in got][:2] == ["机器人", "算力"]                   # 涨停家数多的在前；单只首板的「消费」被滤掉
    assert "消费" not in by
    r = by["机器人"]
    assert (r["n"], r["maxb"], r["levels"]) == (4, 5, 3) and r["leaders"] == ["甲"]
    assert by["算力"]["n"] == 2 and by["算力"]["maxb"] == 5 and by["算力"]["leaders"] == ["甲"]


def test_phase_follows_the_knowledge_base_stages():
    assert phase(n=3, prev_n=1, maxb=2) == "启动"
    assert phase(n=6, prev_n=4, maxb=3) == "发酵"
    assert phase(n=9, prev_n=9, maxb=6) == "高潮"
    assert phase(n=4, prev_n=9, maxb=3) == "分歧/退潮"
    assert phase(n=2, prev_n=0, maxb=2) == ""


def test_ladder_uses_previous_day_for_the_phase():
    today = [_r(f"60000{i}", f"股{i}", "机器人") for i in range(1, 5)]
    prev = [_r(f"60001{i}", f"前{i}", "机器人") for i in range(1, 10)]
    t = ladder(today, prev_rows=prev, top=3)[0]
    assert t["prev_n"] == 9 and t["phase"] == "分歧/退潮"


def test_bz_pool_takes_first_boards_of_themes_whose_leader_has_five_or_more_boards():
    rows = [_r("600001", "龙", "机器人", "6天6板"),
            _r("600002", "补一", "机器人", "首板"), _r("300003", "创业板", "机器人", "首板"),
            _r("600004", "二板", "机器人", "2天2板"),                          # 不是首板
            _r("600005", "他题材", "消费", "首板"), _r("600006", "小龙", "消费", "3天3板")]
    pool = bz_pool(rows, universe={"600001", "600002", "600004", "600005", "600006", "300003"})
    assert set(pool) == {"600002"} and pool["600002"] == {"theme": "机器人", "leader": 6}
    assert bz_pool(rows, universe={"600001"}) == {}                              # 不在股票池里的不要


def test_bz_pool_picks_the_theme_with_the_highest_leader_when_a_stock_has_several():
    rows = [_r("600001", "龙甲", "机器人", "5天5板"), _r("600002", "龙乙", "算力", "8天8板"),
            _r("600003", "补", "机器人+算力", "首板")]
    assert bz_pool(rows, universe={"600003"})["600003"] == {"theme": "算力", "leader": 8}


def test_bz_candidates_need_a_zero_to_three_percent_open_ranked_by_leader_height():
    from datetime import date, datetime
    from factories import quote
    t = datetime(2026, 11, 3, 9, 30, 20)
    q = lambda code, g, ts=t: quote(code, pct=g, open_pct=g, pre_close=10.0, ts=ts)           # noqa: E731
    pool = {"600001": {"theme": "A", "leader": 5}, "600002": {"theme": "B", "leader": 8}, "600003": {"theme": "A", "leader": 5},
            "600004": {"theme": "A", "leader": 5}, "600005": {"theme": "A", "leader": 5}}
    quotes = {"600001": q("600001", 1.0), "600002": q("600002", 2.5), "600003": q("600003", 4.0),
              "600004": q("600004", -0.5), "600005": q("600005", 1.5, ts=datetime(2026, 11, 2, 15))}
    assert [(x.code, n) for x, n in bz_candidates(quotes, pool, date(2026, 11, 3))] == [("600002", 8), ("600001", 5)]
