"""交易台经理（dj 2026-11-09）：汇报员用代码算，经理调 DeepSeek 判断今天是什么行情、开哪位交易员；交易员照常选股下单，
被经理关掉开仓的那位，买单进「被丢弃」并写明原因；经理没给指令（失败 / 还没跑）时一切照旧。"""
import json
from datetime import datetime

import pytest

from dashboard import flex_trader as ft
from dashboard import manager as mg


def at(h, m, s=0, d=9):
    return datetime(2026, 11, d, h, m, s)


def _ctx():
    return {"time": "09:26", "slot": "09:26", "snapshotId": "FLEX-20261109-0926",
            "market": {"breadth": {"up": 3600, "down": 1200, "amount": 1.2e12},
                       "sentiment": {"涨停家数": 69, "跌停家数": 3, "炸板率%": 18.0}},
            "highBoards": {"count": 6, "weak": 4, "verdict": "高标走弱",
                           "items": [{"code": "601599", "name": "高标甲", "boards": 5, "openPct": -3.0, "pct": -6.0}]},
            "themeLadder": [{"theme": "先进封装", "n": 9, "maxb": 3, "levels": 3, "leaders": ["甲"], "phase": "发酵",
                             "members": [{"code": "600001", "name": "甲", "boards": 3}]}],
            "strategyTargets": [],
            "quotes": {"600001": {"name": "甲", "price": 10.0, "pct": 10.0, "sealed": True, "amount亿": 5.0, "src": "梯队"},
                       "600002": {"name": "乙", "price": 20.0, "pct": 4.5, "sealed": False, "amount亿": 12.0, "src": "异动"},
                       "000003": {"name": "丙", "price": 8.0, "pct": 3.1, "sealed": False, "amount亿": 3.0, "src": "异动"},
                       "600004": {"name": "丁", "price": 6.0, "pct": 10.0, "sealed": True, "amount亿": 2.0, "src": "扫板"}},
            "account": {"equity": 120000, "cash": 120000, "positions": [], "guards": "护栏正常（空仓）"}, "notes": []}


def _accounts():
    return {"aiflex": {"equity": 118000, "cash": 60000, "positions": [{"code": "600009", "pnlPct": -2.0}], "guards": "当日 -1.0%；护栏正常"},
            "aiflex2": {"equity": 121000, "cash": 121000, "positions": [], "guards": "当日 +0.0%；【连亏 3 笔，今天不开新仓】"}}


def _logs():
    return {"aiflex": [{"ts": "2026-11-09T09:26:30", "slot": "09:26", "analysis": {"stage": "分歧", "plan": "盯先进封装"},
                        "orders": [{"action": "买入", "code": "600002"}], "rawOrders": 2,
                        "dropped": [{"action": "买入", "code": "600999", "why": "不在候选里"}], "candidates": 40}],
            "aiflex2": []}


def test_reporters_turn_the_scene_into_one_line_per_desk_with_a_level():
    reports = {r["role"]: r for r in mg.desk_reports(_ctx(), _accounts(), _logs(), at(9, 27))}
    assert {"breadth", "ladder", "high", "movers", "scan", "book", "risk"} <= set(reports)
    assert "3600" in reports["breadth"]["line"] and "69" in reports["breadth"]["line"] and reports["breadth"]["level"] == "ok"
    assert "先进封装" in reports["ladder"]["line"]
    assert reports["high"]["level"] == "bad" and "走弱" in reports["high"]["line"]
    assert "2" in reports["movers"]["line"] and "乙" in reports["movers"]["line"]          # 两只异动，成交额最大的乙排前
    assert reports["risk"]["level"] == "bad" and "连亏" in reports["risk"]["line"]        # 二号的护栏触发了
    for r in reports.values():
        assert r["kind"] in mg.KINDS and r["code"] and r["name"] and r["ts"] == "09:27"


def test_reporters_survive_an_empty_scene():
    reports = mg.desk_reports({}, {}, {}, at(9, 27))
    assert len(reports) >= 7 and all(r["line"] for r in reports)


def test_manager_sees_reports_and_each_trader_but_not_the_whole_quote_table():
    reports = mg.desk_reports(_ctx(), _accounts(), _logs(), at(9, 27))
    mctx = mg.manager_context(at(9, 27), "09:26", _ctx(), reports, _accounts(), _logs())
    assert mctx["snapshotId"] == "MGR-20261109-0926"
    assert set(mctx["traders"]) == set(ft.TRADERS)
    assert mctx["traders"]["aiflex"]["style"] and mctx["traders"]["aiflex"]["today"][0]["stage"] == "分歧"
    assert "quotes" not in mctx and all("members" not in t for t in mctx["themeLadder"])
    system, prompt = mg.build_prompt(mctx)
    for text in ("打板日", "趋势日", "空仓日", "否证", "不选股"):
        assert text in system, text
    assert "MGR-20261109-0926" in prompt and "先进封装" in prompt


def _answer(**over):
    a = {"snapshotId": "MGR-20261109-0926", "regime": "趋势日", "read": "高标走弱，资金去低位科技放量",
         "traders": {"aiflex": {"buy": False, "maxPct": 0.1, "why": "高标走弱，接力吃面"},
                     "aiflex2": {"buy": True, "maxPct": 0.2, "why": "异动票放量，趋势单有承接"}},
         "falsify": "10:30 前先进封装涨停数回落到 3 家以下", "watch": "盯先进封装中军"}
    a.update(over)
    return a


def test_schema_requires_a_decision_for_every_trader_and_a_falsification():
    schema = mg.answer_schema("MGR-1", list(ft.TRADERS))
    assert set(schema["properties"]["traders"]["required"]) == set(ft.TRADERS)
    assert "falsify" in schema["required"] and schema["properties"]["regime"]["enum"] == list(mg.REGIMES)


def test_validate_directive_checks_the_id_and_fills_missing_traders_as_open():
    d = mg.validate_directive(_answer(), "MGR-20261109-0926", list(ft.TRADERS))
    assert d["traders"]["aiflex"]["buy"] is False and d["traders"]["aiflex2"]["maxPct"] == 0.2
    partial = _answer(traders={"aiflex": {"buy": False, "maxPct": 0.5, "why": "x"}})
    d2 = mg.validate_directive(partial, "MGR-20261109-0926", list(ft.TRADERS))
    assert d2["traders"]["aiflex"]["maxPct"] == ft.config.FLEX_MAX_PCT                   # 超过护栏上限按上限
    assert d2["traders"]["aiflex2"]["buy"] is True                                      # 漏掉的交易员照常开
    with pytest.raises(ValueError, match="编号"):
        mg.validate_directive(_answer(snapshotId="x"), "MGR-20261109-0926", list(ft.TRADERS))


class Notifier:
    def __init__(self):
        self.messages = []

    def send(self, title, content):
        self.messages.append((title, content))

    def flush(self, timeout=5):
        pass


def _paths(tmp_path):
    return dict(directive_path=tmp_path / "directive.json", log_path=tmp_path / "mgr.jsonl",
                state_path=tmp_path / "mgr_state.json", reports_path=tmp_path / "reports.json")


def _gather(now, slot):
    return {"ctx": _ctx(), "accounts": _accounts(), "logs": _logs()}


def test_run_manager_writes_the_directive_reports_and_log_and_pushes(tmp_path):
    p, n = _paths(tmp_path), Notifier()
    out = mg.run_manager(at(9, 26, 40), "09:26", gather=_gather, ask=lambda s, pr, sc: _answer(), notifier=n, **p)
    assert out == {"slot": "09:26", "regime": "趋势日", "buy": ["aiflex2"]}
    d = json.loads(p["directive_path"].read_text(encoding="utf-8"))
    assert d["date"] == "2026-11-09" and d["slot"] == "09:26" and d["traders"]["aiflex"]["buy"] is False
    rep = json.loads(p["reports_path"].read_text(encoding="utf-8"))
    assert rep["ts"] == "2026-11-09T09:26:40" and any(r["role"] == "breadth" for r in rep["reports"])
    assert json.loads(p["log_path"].read_text(encoding="utf-8").splitlines()[-1])["regime"] == "趋势日"
    title, body = n.messages[0]
    assert "经理" in title and "趋势日" in title
    assert "AI灵活交易" in body and "关" in body and "AI趋势交易员" in body and "否证" in body
    assert mg.due_slot(at(9, 30), p["state_path"]) is None


def test_manager_failure_leaves_no_directive_and_retries_in_the_window(tmp_path):
    p = _paths(tmp_path)

    def boom(*a):
        raise TimeoutError("WorkBuddy 超时")
    with pytest.raises(TimeoutError):
        mg.run_manager(at(9, 26), "09:26", gather=_gather, ask=boom, notifier=Notifier(), **p)
    assert not p["directive_path"].exists() and mg.due_slot(at(9, 27), p["state_path"]) == "09:26"


def test_manager_slots_are_fewer_than_the_traders_and_start_with_theirs():
    assert mg.SLOTS[0] == ft.SLOTS[0] and set(mg.SLOTS) <= set(ft.SLOTS) and len(mg.SLOTS) < len(ft.SLOTS)


def test_directive_for_only_counts_today(tmp_path):
    path = tmp_path / "directive.json"
    assert mg.directive_for("aiflex", at(10, 0), path) is None
    path.write_text(json.dumps({"date": "2026-11-06", "regime": "空仓日", "traders": {"aiflex": {"buy": False}}}), encoding="utf-8")
    assert mg.directive_for("aiflex", at(10, 0), path) is None                          # 上周五的指令今天不算
    path.write_text(json.dumps({"date": "2026-11-09", "slot": "09:26", "regime": "趋势日",
                                "traders": {"aiflex": {"buy": False, "maxPct": 0.1, "why": "高标走弱"}}}), encoding="utf-8")
    d = mg.directive_for("aiflex", at(10, 0), path)
    assert d["buy"] is False and d["regime"] == "趋势日" and d["slot"] == "09:26"
    assert mg.directive_for("aiflex2", at(10, 0), path) is None                         # 没提到的交易员：没有指令


def test_apply_directive_blocks_buys_caps_size_and_keeps_sells():
    result = {"orders": [{"action": "买入", "code": "600001", "pct": 0.25}, {"action": "卖出", "code": "600002", "pct": None}],
              "dropped": []}
    closed = mg.apply_directive(json.loads(json.dumps(result)), {"buy": False, "maxPct": 0.1, "regime": "空仓日", "why": "跌停扩散"})
    assert [o["action"] for o in closed["orders"]] == ["卖出"]
    assert closed["dropped"][0]["code"] == "600001" and "经理" in closed["dropped"][0]["why"]
    capped = mg.apply_directive(json.loads(json.dumps(result)), {"buy": True, "maxPct": 0.1, "regime": "趋势日", "why": ""})
    assert capped["orders"][0]["pct"] == 0.1 and len(capped["orders"]) == 2
    assert mg.apply_directive(json.loads(json.dumps(result)), None) == result


def _good_trader_answer():
    return {"snapshotId": "FLEX-20261109-0926", "analysis": {"market": "m", "stage": "分歧", "plan": "p"},
            "orders": [{"action": "买入", "code": "600002", "pct": 0.25, "stop": 19.0, "ref_price": 20.0, "invalid": "破 19",
                        "mode": "模式内", "pattern": "趋势", "reason": "放量"}], "next": ""}


def test_trader_cycle_obeys_the_managers_directive_and_tells_the_model_about_it(tmp_path, monkeypatch):
    path = tmp_path / "directive.json"
    path.write_text(json.dumps({"date": "2026-11-09", "slot": "09:26", "regime": "空仓日",
                                "traders": {"aiflex": {"buy": False, "maxPct": 0.1, "why": "跌停扩散"}}}), encoding="utf-8")
    monkeypatch.setattr(mg, "DIRECTIVE_PATH", path)
    seen, n = {}, Notifier()

    def ask(system, prompt, schema):
        seen["prompt"] = prompt
        return _good_trader_answer()
    ctx = _ctx()
    ctx["account"]["positions"] = [{"code": "600009"}]                                 # 有持仓：照样要问它管持仓
    out = ft.run_cycle(at(9, 27), "09:26", gather=lambda now, slot: ctx, ask=ask, orders_path=tmp_path / "o.json",
                       log_path=tmp_path / "l.jsonl", state_path=tmp_path / "s.json", notifier=n)
    assert out["orders"] == 0 and "空仓日" in seen["prompt"]
    assert json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))["orders"] == []
    assert "经理" in n.messages[0][1]
    log = json.loads((tmp_path / "l.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert log["manager"]["regime"] == "空仓日" and log["candidates"] == 4


def test_trader_closed_by_the_manager_with_nothing_held_skips_the_model_call(tmp_path, monkeypatch):
    path = tmp_path / "directive.json"
    path.write_text(json.dumps({"date": "2026-11-09", "slot": "09:26", "regime": "空仓日",
                                "traders": {"aiflex": {"buy": False, "maxPct": 0.1, "why": "跌停扩散"}}}), encoding="utf-8")
    monkeypatch.setattr(mg, "DIRECTIVE_PATH", path)

    def ask(*a):
        raise AssertionError("不该调用 DeepSeek")
    out = ft.run_cycle(at(9, 27), "09:26", gather=lambda now, slot: _ctx(), ask=ask, orders_path=tmp_path / "o.json",
                       log_path=tmp_path / "l.jsonl", state_path=tmp_path / "s.json", notifier=Notifier())
    assert out["orders"] == 0 and "经理" in out["skipped"]
    assert ft.due_slot(at(9, 30), tmp_path / "s.json") is None
    assert json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))["orders"] == []


def test_without_a_directive_the_trader_works_as_before(tmp_path):
    out = ft.run_cycle(at(9, 27), "09:26", gather=lambda now, slot: _ctx(), ask=lambda *a: _good_trader_answer(),
                       orders_path=tmp_path / "o.json", log_path=tmp_path / "l.jsonl", state_path=tmp_path / "s.json")
    assert out["orders"] == 1
    assert json.loads((tmp_path / "l.jsonl").read_text(encoding="utf-8").splitlines()[-1])["manager"] is None


def test_manager_is_told_the_company_target_and_how_far_it_is_and_that_guards_stay(tmp_path):
    # dj 2026-11-11：「经理肯定要知道这个目标，所有动作都是为了这个目标」
    company = {"equity": 239000, "items": [{"key": "day", "label": "今日", "target": 1.0, "actual": -0.2, "hit": False, "gapYuan": 2870}]}
    reports = mg.desk_reports(_ctx(), _accounts(), _logs(), at(9, 27))
    mctx = mg.manager_context(at(9, 27), "09:26", _ctx(), reports, _accounts(), _logs(), company=company)
    assert mctx["company"] == company
    system, prompt = mg.build_prompt(mctx)
    assert "公司目标" in system and "company" in system and "护栏" in system and "2870" in prompt
    assert "company" not in mg.manager_context(at(9, 27), "09:26", _ctx(), reports, _accounts(), _logs())   # 算不出来就不给，不编

    seen = {}

    def gather(now, slot):
        return {**_gather(now, slot), "company": company}

    def ask(system, prompt, schema):
        seen["prompt"] = prompt
        return _answer()
    mg.run_manager(at(9, 26, 40), "09:26", gather=gather, ask=ask, **_paths(tmp_path))
    assert "gapYuan" in seen["prompt"]


def test_company_score_uses_last_equity_before_each_period_or_capital():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE equity(date TEXT, account TEXT, value REAL)")
    conn.execute("INSERT INTO equity VALUES ('2026-11-06', 'aiflex_main', 119000)")
    c = mg.company_score(conn, [("aiflex_main", 120190.0), ("aiflex2_main", 120000.0)], at(10, 0))
    day, week, month = c["items"]
    assert day["actual"] == week["actual"] == round((240190 / 239000 - 1) * 100, 2) and month["actual"] == round((240190 / 240000 - 1) * 100, 2)
    assert day["gapYuan"] == round(239000 * 1.01 - 240190) and not day["hit"]


def test_manager_sizes_bigger_and_concentrates_because_many_small_positions_cannot_reach_the_target():
    # dj 2026-11-11：「持有票数太多了，仓位又低，要么降低票数，要么提高仓位」；风格偏中高风险
    assert (ft.config.FLEX_MAX_POSITIONS, ft.config.FLEX_MAX_PCT, ft.DEFAULT_PCT) == (3, 0.40, 0.30)
    assert "15%~20%" in mg.SYSTEM_PROMPT and "10%~15%" not in mg.SYSTEM_PROMPT and "0.03~0.40" in mg.SYSTEM_PROMPT
    assert mg.answer_schema("S", ["aiflex"])["properties"]["traders"]["properties"]["aiflex"]["properties"]["maxPct"]["maximum"] == 0.40
