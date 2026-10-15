"""超跌主账户看板块（用户 2026-10-15）：板块数据、黑名单、个股自己跌、Claude 冷门板块否决。"""
import json
from datetime import date, datetime
from time import monotonic

from factories import quote

from jevq import config, db
from jevq.data import sectors
from jevq.judge.sector_veto import ClaudeSectorVeto, candidate_line
from jevq.ledger import Ledger
from jevq.strategies.rebound import ReboundStrategy

TODAY = date(2026, 9, 24)
PREV_DAY = date(2026, 9, 23)


def at(h, m):
    return datetime(2026, 9, 24, h, m)


# ---- 板块数据 ----

def _conn_with_sectors():
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO concepts VALUES (?, ?, '2026-09-20')",
                     [("600001", "机器人"), ("600002", "机器人"), ("600003", "机器人"), ("600001", "融资融券")])
    conn.executemany("INSERT INTO industries VALUES (?, ?, '2026-09-20')",
                     [("600004", "酿酒行业"), ("600005", "电力行业")])
    conn.execute("INSERT INTO own_sectors VALUES ('liquor', '白酒', '白酒及黄酒', '[]', 'active', '2026-10-01')")
    conn.execute("INSERT INTO own_members VALUES ('liquor', '600006', 'core', 0.9, '{}', 'manual', 'confirmed', "
                 "'2026-09-20', '2099-12-31')")
    conn.commit()
    return conn


def test_load_map_skips_trading_flags_and_prefixes_industry_and_own():
    m = sectors.load_map(_conn_with_sectors(), TODAY)
    assert m["600001"] == {"机器人"}                              # 融资融券不是板块
    assert m["600004"] == {"行业·酿酒行业"}
    assert m["600006"] == {"自建·白酒"}


def test_blacklist_by_own_sector_id_and_name_words():
    conn = _conn_with_sectors()
    m = sectors.load_map(conn, TODAY)
    bl = sectors.blacklisted_codes(conn, m, TODAY)
    assert bl == {"600006": "自建·白酒", "600004": "行业·酿酒行业"}


def test_sector_ret5_needs_min_members_and_weakest_takes_the_lowest():
    m = {"600001": {"A", "B"}, "600002": {"A"}, "600003": {"A", "B"}, "600004": {"B"}}
    r5 = {"600001": -6.0, "600002": -2.0, "600003": -4.0, "600004": 1.0}
    s = sectors.sector_ret5(m, r5, min_members=3)
    assert s == {"A": -4.0, "B": -4.0}
    s = sectors.sector_ret5(m, r5, min_members=2)
    assert sectors.weakest_sector("600002", m, s) == ("A", -4.0)
    assert sectors.weakest_sector("600009", m, s) is None


def test_lone_drop_is_stock_down_more_than_5_with_sector_down_less_than_3(monkeypatch):
    monkeypatch.setattr(config, "REBOUND_LONE_FILTER", True)
    m = {c: {"板块"} for c in ("600001", "600002", "600003", "600004", "600005")}
    r5 = {"600001": -8.0, "600002": -1.0, "600003": -1.0, "600004": -1.5, "600005": -2.0}   # 板块中位数 -1.5
    v = sectors.SectorView(m, r5, {})
    assert v.lone_drop("600001") == ("板块", -1.5)
    assert v.lone_drop("600002") is None                          # 个股没跌超 5%
    assert "个股自己跌" in v.reason_blocked("600001")
    deep = sectors.SectorView(m, {**r5, "600002": -6.0, "600003": -5.0, "600004": -4.0}, {})   # 板块中位数 -5
    assert deep.lone_drop("600001") is None                       # 板块也跌了：不算自己跌
    assert sectors.SectorView({}, r5, {}).lone_drop("600001") is None     # 没板块归属：不算


def test_refresh_industries_keeps_old_data_when_less_than_half_fetched():
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO industries VALUES ('600001', '旧行业', '2026-09-01')")
    conn.commit()

    def members(label):
        if label != "a":
            raise RuntimeError("boom")
        return ["600002"]
    n = sectors.refresh_industries(conn, TODAY, fetch_list=lambda: [("a", "甲"), ("b", "乙"), ("c", "丙")],
                                   fetch_members=members, pause=0, log=lambda *_: None)
    assert n == 0 and conn.execute("SELECT industry FROM industries").fetchall() == [("旧行业",)]
    n = sectors.refresh_industries(conn, TODAY, fetch_list=lambda: [("a", "甲")], fetch_members=members, pause=0,
                                   log=lambda *_: None)
    assert n == 1 and conn.execute("SELECT code, industry FROM industries").fetchall() == [("600002", "甲")]
    assert not sectors.needs_refresh(conn, TODAY)


# ---- 策略：主账户板块条件，影子账户不看 ----

class Judge:
    def __init__(self, verdicts=None):
        self.verdicts, self.calls, self.last_trace = verdicts or {}, [], None

    def check(self, code, *args):
        self.calls.append(code)
        return self.verdicts.get(code, ("pass", "ok"))


UNIVERSE = {"600001": "甲", "600002": "乙", "600003": "丙"}
SHRINK4 = {"vols": [100.0, 400.0, 300.0, 200.0, 150.0], "vols_until": PREV_DAY.isoformat()}
RET5 = {"600001": -5.0, "600002": -15.0, "600003": -8.0}         # 大盘中位数 -8%：普跌


def make(view=None, veto=None):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    strat = ReboundStrategy(conn, ledger, Judge(), Judge(), UNIVERSE, dict(RET5), set(), PREV_DAY,
                            lambda: "正常", features={c: SHRINK4 for c in UNIVERSE})
    strat.sector_view, strat.sector_veto = view, veto
    return strat, conn


def _quotes(t, **pcts):
    return {c: quote(c, pct=p, amount=2e8, ts=t, volume=10) for c, p in pcts.items()}


Q = {"600001": -1.0, "600002": -1.0, "600003": -1.0}


def _bought(fills):
    return {(f.account, f.code) for f in fills if f.side == "buy"}


def _view(blacklist=None):
    """三只票都在一个跌了 8% 的板块里（不是个股自己跌）。"""
    m = {c: {"板块"} for c in UNIVERSE}
    return sectors.SectorView(m, dict(RET5), blacklist or {})


def test_main_skips_blacklisted_sector_shadow_still_buys_it_and_logs_once():
    strat, conn = make(view=_view({"600002": "自建·白酒"}))
    fills = strat.on_minute(_quotes(at(10, 0), **Q), at(10, 0))
    assert _bought(fills) == {("rebound_main", "600003"), ("rebound_shadow", "600002")}
    strat.on_minute(_quotes(at(10, 1), **Q), at(10, 1))
    rows = conn.execute("SELECT code, verdict, detail FROM decisions WHERE verdict='skip'").fetchall()
    assert rows == [("600002", "skip", "板块黑名单（自建·白酒）")]


def test_lone_drop_filter_is_off_by_default():
    m = {c: {"板块"} for c in ("600001", "600002", "600003", "600004", "600005")}
    v = sectors.SectorView(m, {"600001": -8.0, "600002": -1.0, "600003": -1.0, "600004": -1.5, "600005": -2.0}, {})
    assert v.lone_drop("600001") is not None and v.reason_blocked("600001") is None


def test_main_skips_lone_drop_when_switched_on(monkeypatch):
    monkeypatch.setattr(config, "REBOUND_LONE_FILTER", True)
    m = {"600001": {"A"}, "600002": {"A"}, "600003": {"A"}, **{f"60{i:04d}": {"A"} for i in range(100, 105)}}
    r5 = {**RET5, **{f"60{i:04d}": 0.0 for i in range(100, 105)}}       # 板块中位数 0：600002 跌 15% 是自己跌
    strat, conn = make(view=sectors.SectorView(m, r5, {}))
    strat.market_ret5 = -6.0                                           # 比大盘多跌 9 点：大盘那条不拦
    fills = strat.on_minute(_quotes(at(10, 0), **Q), at(10, 0))
    assert ("rebound_main", "600002") not in _bought(fills) and ("rebound_shadow", "600002") in _bought(fills)
    detail = conn.execute("SELECT detail FROM decisions WHERE verdict='skip' AND code='600002'").fetchone()[0]
    assert detail.startswith("个股自己跌")


class FakeVeto:
    def __init__(self, answers):
        self.answers, self.calls, self.last_trace = list(answers), [], {"answer": "x"}

    def judge(self, lines, now, timeout=None):
        self.calls.append(list(lines))
        got = self.answers.pop(0) if self.answers else None
        return None if got is None else {c: got.get(c, (True, "默认保留")) for c in lines}


def test_claude_veto_rejects_cold_sector_and_main_buys_the_next_one(monkeypatch):
    monkeypatch.setattr(config, "REBOUND_SECTOR_VETO", True)
    veto = FakeVeto([{"600002": (False, "白酒没人关注")}])
    strat, conn = make(view=_view(), veto=veto)
    fills = strat.on_minute(_quotes(at(10, 0), **Q), at(10, 0))
    assert ("rebound_main", "600003") in _bought(fills)
    assert veto.calls == [["600002", "600003", "600001"]]          # 一次把前几只一起问
    row = conn.execute("SELECT detail FROM decisions WHERE verdict='reject' AND code='600002'").fetchone()
    assert row == ("板块冷门（Claude）：白酒没人关注",)
    trace = json.loads(conn.execute("SELECT trace FROM decisions WHERE verdict='pass' AND code='600003'").fetchone()[0])
    assert trace["candidate"]["sector_keep"] == "默认保留"


def test_claude_veto_survives_restart_through_rejected_set(monkeypatch):
    monkeypatch.setattr(config, "REBOUND_SECTOR_VETO", True)
    veto = FakeVeto([{"600002": (False, "冷门")}])
    strat, conn = make(view=_view(), veto=veto)
    strat.on_minute(_quotes(at(10, 0), **Q), at(10, 0))
    strat2 = ReboundStrategy(conn, Ledger(conn), Judge(), Judge(), UNIVERSE, dict(RET5), set(), PREV_DAY,
                             lambda: "正常", features={c: SHRINK4 for c in UNIVERSE})
    strat2.on_minute(_quotes(at(10, 1), **Q), at(10, 1))
    assert "600002" in strat2.rejected


def test_claude_no_answer_buys_by_rules_and_waits_before_asking_again(monkeypatch):
    monkeypatch.setattr(config, "REBOUND_SECTOR_VETO", True)
    veto = FakeVeto([None, None])
    strat, conn = make(view=_view(), veto=veto)
    fills = strat.on_minute(_quotes(at(10, 0), **Q), at(10, 0))
    assert ("rebound_main", "600002") in _bought(fills)
    assert conn.execute("SELECT detail FROM decisions WHERE verdict='sector'").fetchone() == (
        "Claude 没答上板块冷热，按规则买",)
    assert strat._veto_retry_at == at(10, 0 + config.REBOUND_SECTOR_VETO_RETRY)


def test_veto_waits_when_minute_budget_is_almost_used(monkeypatch):
    monkeypatch.setattr(config, "REBOUND_SECTOR_VETO", True)
    veto = FakeVeto([{"600002": (True, "ok")}])
    strat, conn = make(view=_view(), veto=veto)
    fills = strat.buy_step(_quotes(at(10, 0), **Q), at(10, 0), deadline=monotonic() + 3)
    assert not any(f.account == "rebound_main" for f in fills) and veto.calls == []


def test_veto_switch_off_means_rules_only():
    veto = FakeVeto([{"600002": (False, "冷门")}])
    strat, conn = make(view=_view(), veto=veto)
    config_val = config.REBOUND_SECTOR_VETO
    assert config_val is False                                      # conftest 关掉了：测试不问
    fills = strat.on_minute(_quotes(at(10, 0), **Q), at(10, 0))
    assert ("rebound_main", "600002") in _bought(fills) and veto.calls == []


# ---- ClaudeSectorVeto 本身 ----

class FakeCLI:
    def __init__(self, answer):
        self.answer, self.asked, self.last_trace = answer, [], None

    def ask_json(self, system, user, timeout=None):
        self.asked.append(user)
        return self.answer


def test_sector_veto_parses_items_and_keeps_unmentioned():
    cli = FakeCLI({"items": [{"code": "600002", "keep": False, "reason": "冷门"},
                             {"code": "600003", "keep": "否", "reason": "格式不对按保留"}]})
    v = ClaudeSectorVeto(cli, news_provider=lambda t: ["10:00 财联社｜机器人订单"],
                         catalyst_provider=lambda t: ["机器人（5 条）：……"])
    got = v.judge({"600002": "乙", "600003": "丙", "600001": "甲"}, at(10, 0))
    assert got["600002"] == (False, "冷门")
    assert got["600003"][0] is True and got["600001"][0] is True
    assert "机器人订单" in cli.asked[0] and "机器人（5 条）" in cli.asked[0]


def test_sector_veto_returns_none_when_claude_did_not_answer():
    assert ClaudeSectorVeto(FakeCLI(None)).judge({"600002": "乙"}, at(10, 0)) is None
    assert ClaudeSectorVeto(FakeCLI({"x": 1})).judge({"600002": "乙"}, at(10, 0)) is None


def test_candidate_line_lists_weakest_sectors_first():
    q = quote("600002", pct=-1.2, ts=at(10, 0))
    line = candidate_line(q, -8.5, ["A", "B", "C"], {"A": -1.0, "B": -6.0})
    assert line.index("B(-6.0%)") < line.index("A(-1.0%)") and "C" in line and "5 日 -8.50%" in line


def test_main_buys_on_ice_point_in_broad_drop_only_when_switched_on(monkeypatch):
    conn = db.connect(":memory:")
    strat = ReboundStrategy(conn, Ledger(conn), Judge(), Judge(), UNIVERSE, dict(RET5), set(), PREV_DAY,
                            lambda: "冰点", features={c: SHRINK4 for c in UNIVERSE})
    monkeypatch.setattr(config, "REBOUND_MAIN_BUY_ON_ICE", False)
    assert strat.on_minute(_quotes(at(10, 0), **Q), at(10, 0)) == []
    monkeypatch.setattr(config, "REBOUND_MAIN_BUY_ON_ICE", True)
    fills = strat.on_minute(_quotes(at(10, 1), **Q), at(10, 1))
    assert _bought(fills) == {("rebound_main", "600002")}                          # 影子账户照旧冰点不买


def test_attach_sectors_sets_view_and_skips_veto_without_claude(monkeypatch):
    import run
    conn = _conn_with_sectors()
    strat = type("S", (), {"sector_view": None, "sector_veto": None})()
    logs, state = [], {}
    monkeypatch.setattr(config, "REBOUND_SECTOR_VETO", True)          # conftest 把 CLAUDE_BIN 指到不存在的路径
    monkeypatch.setattr(run, "_warn", lambda st, log, notifier, msg: logs.append(msg))
    run._attach_sectors(strat, conn, TODAY, {"600001": -1.0}, flash=None, log=logs.append, state=state, notifier=None)
    assert strat.sector_view is not None and "600006" in strat.sector_view.blacklist
    assert strat.sector_veto is None and any("找不到本机 claude" in m for m in logs)


def test_attach_sectors_failure_leaves_rules_only():
    import run
    conn = db.connect(":memory:")
    conn.execute("DROP TABLE industries")
    strat = type("S", (), {"sector_view": "old", "sector_veto": None})()
    logs = []
    run._attach_sectors(strat, conn, TODAY, {}, flash=None, log=logs.append, state={}, notifier=None)
    assert strat.sector_view is None and any("板块数据失败" in m for m in logs)
