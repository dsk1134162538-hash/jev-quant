"""AI 灵活交易的 ai_loop 一侧（dj 2026-11-03）：关键时点调度、提示词（知识库手册 + 回测结论）、答案校验、写订单文件、推送。"""
import json
from datetime import datetime, timedelta

import pytest

from dashboard import flex_trader as ft
from jevq.rules.themes import ladder


def at(h, m, s=0):
    return datetime(2026, 11, 3, h, m, s)


def test_due_slot_runs_each_key_time_once_and_not_long_after_it(tmp_path):
    state = tmp_path / "flex_state.json"
    assert ft.due_slot(at(9, 20), state) is None                                  # 还没到第一个时点
    assert ft.due_slot(at(9, 27), state) == "09:26"
    ft.mark_done("09:26", at(9, 27), state)
    assert ft.due_slot(at(9, 30), state) is None                                  # 09:26 跑过了，10:00 还没到
    assert ft.due_slot(at(10, 1), state) == "10:00"
    assert ft.due_slot(at(10, 40), state) == "10:30"                              # 10:00 错过了（超过 20 分钟）不补，取最近的
    assert ft.due_slot(at(15, 30), state) is None                                 # 收盘后没有
    assert ft.due_slot(datetime(2026, 11, 4, 9, 27), state) == "09:26"            # 新的一天重新开始


def test_ladder_can_carry_members_so_the_trader_has_real_codes_to_pick_from():
    rows = [{"code": "600001", "name": "甲", "reason": "机器人", "high_days": "5天5板"},
            {"code": "600002", "name": "乙", "reason": "机器人", "high_days": "首板"},
            {"code": "300003", "name": "创", "reason": "机器人", "high_days": "首板"}]
    t = ladder(rows, with_members=True)[0]
    assert [(m["code"], m["boards"]) for m in t["members"]] == [("600001", 5), ("600002", 1)]    # 只留主板，连板高的在前
    assert "members" not in ladder(rows)[0]


def _ctx():
    return {"time": "10:00", "slot": "10:00", "snapshotId": "FLEX-20261103-1000",
            "market": {"sentiment": {"涨停家数": 60, "跌停家数": 4, "炸板率%": 20.0}},
            "themeLadder": [{"theme": "机器人", "n": 8, "maxb": 5, "levels": 3, "leaders": ["甲"], "phase": "发酵",
                             "members": [{"code": "600001", "name": "甲", "boards": 5}]}],
            "strategyTargets": [], "quotes": {"600001": {"name": "甲", "price": 10.0, "pct": 3.0}, "600002": {"name": "乙", "price": 20.0, "pct": 1.0}},
            "account": {"equity": 120000, "cash": 120000, "positions": [{"code": "600002", "buyDate": "2026-11-02", "buyPrice": 19.0,
                                                                          "price": 20.0, "stop": 18.5}], "guards": "护栏正常"},
            "notes": []}


def test_prompt_carries_the_handbook_the_research_calibration_and_the_guardrails():
    system, prompt = ft.build_prompt(_ctx())
    for text in ("突破", "模式外", "30%", "止损", "错了的信号", "冰点", "弱转强", "补涨", "龙头"):
        assert text in system, text
    assert "一致高潮" in system and "分歧" in system                                # 回测校准
    assert "600001" in prompt and "机器人" in prompt and "FLEX-20261103-1000" in prompt


def _good_answer(**order):
    o = {"action": "买入", "code": "600001", "pct": 0.2, "stop": 9.5, "ref_price": 10.0, "invalid": "跌破 9.5 说明分歧没被接住",
         "mode": "模式内", "pattern": "补涨", "reason": "龙头 5 板，补涨第一只"}
    o.update(order)
    return {"snapshotId": "FLEX-20261103-1000", "analysis": {"market": "修复延续", "stage": "发酵", "plan": "盯龙头"},
            "orders": [o], "next": "14:00 再看"}


def test_validate_keeps_good_orders_with_ids_and_drops_unknown_codes_and_extra_orders():
    ctx = _ctx()
    ok = ft.validate_answer(_good_answer(), ctx)
    assert ok["orders"][0]["id"] == "FLEX-20261103-1000-1" and ok["orders"][0]["code"] == "600001"
    bad = _good_answer(code="600999")                                              # 不在候选里（模型编的代码）
    assert ft.validate_answer(bad, ctx)["orders"] == []
    many = _good_answer()
    many["orders"] = many["orders"] * 6
    assert len(ft.validate_answer(many, ctx)["orders"]) == ft.MAX_ORDERS_PER_CYCLE
    sell = _good_answer(action="卖出", code="600002")                               # 持仓可以卖
    sell["orders"][0].update(pct=0, stop=0)
    assert ft.validate_answer(sell, ctx)["orders"][0]["action"] == "卖出"


def test_validate_rejects_a_wrong_snapshot_id_or_a_non_object():
    with pytest.raises(ValueError, match="编号"):
        ft.validate_answer({**_good_answer(), "snapshotId": "x"}, _ctx())
    with pytest.raises(ValueError):
        ft.validate_answer([], _ctx())


class Notifier:
    def __init__(self):
        self.messages = []

    def send(self, title, content):
        self.messages.append((title, content))

    def flush(self, timeout=5):
        pass


def test_cycle_writes_orders_logs_the_round_and_pushes_the_reasoning(tmp_path):
    orders_path, log_path, state = tmp_path / "orders.json", tmp_path / "log.jsonl", tmp_path / "state.json"
    notifier = Notifier()
    result = ft.run_cycle(at(10, 0, 20), "10:00", gather=lambda now, slot: _ctx(), ask=lambda system, prompt, schema: _good_answer(),
                          orders_path=orders_path, log_path=log_path, state_path=state, notifier=notifier)
    assert result["orders"] == 1
    written = json.loads(orders_path.read_text(encoding="utf-8"))
    assert written["ts"] == "2026-11-03T10:00:20" and written["orders"][0]["code"] == "600001"
    assert json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])["slot"] == "10:00"
    title, body = notifier.messages[0]
    assert "灵活交易" in title and "发酵" in title
    assert "买入" in body and "600001" in body and "模式内" in body and "跌破 9.5" in body and "按实时行情执行" in body
    assert ft.due_slot(at(10, 5), state) is None                                   # 这个时点记为已跑


def test_cycle_failure_is_recorded_but_does_not_leave_stale_orders_behind(tmp_path):
    orders_path = tmp_path / "orders.json"
    orders_path.write_text(json.dumps({"ts": "2026-11-03T09:26:00", "orders": [{"id": "old"}]}), encoding="utf-8")

    def boom(system, prompt, schema):
        raise TimeoutError("WorkBuddy 超时")
    with pytest.raises(TimeoutError):
        ft.run_cycle(at(10, 0), "10:00", gather=lambda now, slot: _ctx(), ask=boom, orders_path=orders_path,
                     log_path=tmp_path / "log.jsonl", state_path=tmp_path / "state.json", notifier=Notifier())
    assert ft.due_slot(at(10, 1), tmp_path / "state.json") == "10:00"               # 失败不记为已跑，窗口内还会再试


def _flex_db(tmp_path):
    from datetime import date
    from jevq import config, db
    from jevq.ledger import Ledger
    from factories import quote
    path = tmp_path / "jevq.sqlite"
    conn = db.connect(str(path))
    ledger = Ledger(conn)
    ledger.buy("aiflex_main", quote("600002", pct=0.0, pre_close=19.0, ts=at(10, 0)), at(10, 0) - timedelta(days=1), "x", budget=30000)
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, '600002', 'stop', 's', ?, 'aiflex')",
                 ("2026-11-02T10:00:00", json.dumps({"code": "600002", "stop": 18.5, "invalid": "跌破 18.5"})))
    conn.commit()
    conn.close()
    return path


def test_gather_context_collects_positions_stops_ladder_members_and_quotes(tmp_path):
    from factories import quote
    rows = [{"code": "600001", "name": "甲", "reason": "机器人", "high_days": "5天5板"},
            {"code": "600003", "name": "丙", "reason": "机器人", "high_days": "首板"}]
    seen = {}

    def fetch_quotes(codes):
        seen["codes"] = list(codes)
        return {c: quote(c, pct=3.0, pre_close=10.0, ts=at(10, 0), name=f"名{c}") for c in codes}
    ctx = ft.gather_context(at(10, 0, 20), "10:00", db_path=_flex_db(tmp_path), snapshot_path=tmp_path / "none.json",
                            fetch_quotes=fetch_quotes, fetch_rows=lambda d: rows, log_path=tmp_path / "log.jsonl",
                            theme_cache=tmp_path / "theme.json")
    assert ctx["snapshotId"] == "FLEX-20261103-1000" and ctx["slot"] == "10:00"
    assert {"600001", "600002", "600003"} <= set(seen["codes"]) and set(ctx["quotes"]) == set(seen["codes"])   # 持仓 + 梯队成员
    pos = ctx["account"]["positions"][0]
    assert pos["code"] == "600002" and pos["stop"] == 18.5 and pos["buyDate"] == "2026-11-02"
    assert ctx["account"]["equity"] > 0 and "当日" in ctx["account"]["guards"]
    assert ctx["themeLadder"][0]["theme"] == "机器人" and ctx["themeLadder"][0]["members"][0]["code"] == "600001"


def test_high_board_state_tells_whether_yesterdays_leaders_are_collapsing():
    # 2026-11-04：昨天的 3~4 连板全部低开低走（-8%~-11%），资金换去低位科技。让 AI 一眼看到「高标今天怎么走」
    from factories import quote
    rows = [{"code": "600261", "name": "甲", "reason": "x", "high_days": "4天4板"},
            {"code": "002086", "name": "乙", "reason": "x", "high_days": "4天4板"},
            {"code": "000995", "name": "丙", "reason": "x", "high_days": "3天3板"},
            {"code": "600003", "name": "首", "reason": "x", "high_days": "首板"},
            {"code": "300009", "name": "创", "reason": "x", "high_days": "5天5板"}]
    qs = {"600261": quote("600261", pct=-9.9, pre_close=10.0, ts=at(10, 0)),
          "002086": quote("002086", pct=-3.0, pre_close=10.0, ts=at(10, 0)),
          "000995": quote("000995", pct=2.0, pre_close=10.0, ts=at(10, 0))}
    st = ft.high_board_state(rows, qs)
    assert [i["code"] for i in st["items"]] == ["600261", "002086", "000995"]          # 只看主板 ≥3 板；首板、创业板不算
    assert st["count"] == 3 and st["weak"] == 2 and st["verdict"] == "高标走弱"
    assert ft.high_board_state(rows, {})["verdict"] == "无数据"
    assert ft.high_board_state([], qs)["count"] == 0


def test_gather_context_includes_high_board_state_and_prompt_carries_the_rotation_case(tmp_path):
    from factories import quote
    rows = [{"code": "600001", "name": "甲", "reason": "机器人", "high_days": "4天4板"}]
    ctx = ft.gather_context(at(10, 0, 20), "10:00", db_path=_flex_db(tmp_path), snapshot_path=tmp_path / "none.json",
                            fetch_quotes=lambda codes: {c: quote(c, pct=-5.0, pre_close=10.0, ts=at(10, 0)) for c in codes},
                            fetch_rows=lambda d: rows, log_path=tmp_path / "log.jsonl", theme_cache=tmp_path / "theme.json")
    assert ctx["highBoards"]["weak"] == 1 and ctx["highBoards"]["verdict"] == "高标走弱"
    assert "11-04" in ft.SYSTEM_PROMPT and "highBoards" in ft.SYSTEM_PROMPT and "低位" in ft.SYSTEM_PROMPT


def test_prompt_lets_it_probe_on_strong_days_instead_of_vetoing_every_climax_day():
    # 2026-11-05：普涨放量日（上涨 85%、昨日涨停红盘率 91%），aiflex 三个时点 0 单——「高潮日兑现，不接」被当成一票否决
    p = ft.SYSTEM_PROMPT
    assert "兑现，不接" not in p                                   # 兑现只针对已有持仓
    assert "试错" in p and "最想买的两只" in p and "不限市值" in p
    assert "不是禁令" in p and "一票否决" in p
    assert "单票最多占账户净值 30%" in p and "只做沪深主板" in p     # 护栏原样保留


def test_pick_movers_finds_strong_unsealed_main_board_stocks_that_are_not_first_boards():
    # 2026-11-05：候选只来自昨日涨停/梯队/策略标的，永远在首板里挑；盘中放量走强、没封板的票（老题材、大票）AI 看不到
    from factories import quote
    t = at(10, 30)
    qs = {"600001": quote("600001", pct=5.0, amount=8e8, ts=t, name="放量甲"),
          "000002": quote("000002", pct=3.0, amount=3e8, ts=t, name="温和乙"),
          "600003": quote("600003", pct=7.5, amount=2.5e8, ts=t, name="强势丙"),
          "600004": quote("600004", pct=10.0, amount=9e8, ts=t, name="已封板"),
          "600005": quote("600005", pct=5.0, amount=8e8, ts=t, name="ST丁"),
          "300006": quote("300006", pct=5.0, amount=8e8, ts=t, name="创业板"),
          "600007": quote("600007", pct=1.0, amount=8e8, ts=t, name="涨幅不够"),
          "600008": quote("600008", pct=5.0, amount=5e7, ts=t, name="没量"),
          "600009": quote("600009", pct=5.0, amount=8e8, ts=t - timedelta(days=1), name="旧数据")}
    assert ft.pick_movers(qs, t) == ["600001", "000002", "600003"] or set(ft.pick_movers(qs, t)) == {"600001", "000002", "600003"}
    assert ft.pick_movers(qs, t)[0] == "600001"                                   # 成交额大的在前


def test_gather_context_adds_market_movers_tagged_by_source_and_survives_a_failed_scan(tmp_path):
    from factories import quote
    rows = [{"code": "600001", "name": "甲", "reason": "机器人", "high_days": "首板"}]
    market = {"601111": quote("601111", pct=4.0, amount=6e8, ts=at(10, 0), name="异动股")}
    ctx = ft.gather_context(at(10, 0, 20), "10:00", db_path=_flex_db(tmp_path), snapshot_path=tmp_path / "none.json",
                            fetch_quotes=lambda codes: {c: quote(c, pct=3.0, ts=at(10, 0)) for c in codes},
                            fetch_rows=lambda d: rows, fetch_market=lambda: market,
                            log_path=tmp_path / "log.jsonl", theme_cache=tmp_path / "theme.json")
    assert "601111" in ctx["quotes"] and ctx["quotes"]["601111"]["src"] == "异动"
    assert ctx["quotes"]["600002"]["src"] == "持仓"                               # 账户里的持仓仍在，来源另标

    def boom():
        raise TimeoutError("腾讯超时")
    ctx = ft.gather_context(at(10, 0, 20), "10:00", db_path=_flex_db(tmp_path), snapshot_path=tmp_path / "none.json",
                            fetch_quotes=lambda codes: {c: quote(c, pct=3.0, ts=at(10, 0)) for c in codes},
                            fetch_rows=lambda d: rows, fetch_market=boom,
                            log_path=tmp_path / "log.jsonl", theme_cache=tmp_path / "theme.json")
    assert "601111" not in ctx["quotes"] and "600002" in ctx["quotes"]


def test_prompt_tells_it_not_to_fixate_on_first_boards():
    assert "不要只盯" in ft.SYSTEM_PROMPT and "异动" in ft.SYSTEM_PROMPT


def test_gather_context_survives_missing_snapshot_theme_data_and_empty_account(tmp_path):
    from jevq import db
    path = tmp_path / "jevq.sqlite"
    db.connect(str(path)).close()

    def boom(d):
        raise TimeoutError("同花顺超时")
    ctx = ft.gather_context(at(10, 0), "10:00", db_path=path, snapshot_path=tmp_path / "none.json", fetch_quotes=lambda codes: {},
                            fetch_rows=boom, log_path=tmp_path / "log.jsonl", theme_cache=tmp_path / "theme.json")
    assert ctx["themeLadder"] == [] and ctx["quotes"] == {} and ctx["account"]["positions"] == []
