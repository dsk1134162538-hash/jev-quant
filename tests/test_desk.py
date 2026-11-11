"""交易台面板的数据（dj 2026-11-09，仿「横行」面板）：岗位卡片、经理指令、每位交易员的账户 / 漏斗 / 净值、事件日志、护栏参数。只读。"""
import json
from datetime import datetime, timedelta

from dashboard import desk
from dashboard import flex_trader as ft


def at(h, m, s=0):
    return datetime(2026, 11, 9, h, m, s)


class Q:
    def __init__(self, price, pre_close, name="乙"):
        self.price, self.pre_close, self.name = price, pre_close, name


def _db(tmp_path):
    from jevq import db
    from jevq.ledger import Ledger
    from factories import quote
    path = tmp_path / "jevq.sqlite"
    conn = db.connect(str(path))
    ledger = Ledger(conn)
    ledger.ensure_account("aiflex2_main", 120000)
    ledger.buy("aiflex_main", quote("600002", pct=0.0, pre_close=19.0, ts=at(9, 40)), at(9, 40), "模式内·趋势", budget=30000)
    conn.execute("INSERT INTO equity(account, date, value) VALUES ('aiflex_main', '2026-11-06', 119000)")
    conn.commit()
    conn.close()
    return path


def _runtime(tmp_path):
    rt = tmp_path / "runtime"
    rt.mkdir()
    (rt / "manager_directive.json").write_text(json.dumps(
        {"date": "2026-11-09", "slot": "09:26", "ts": "2026-11-09T09:26:40", "regime": "趋势日", "read": "高标走弱",
         "traders": {"aiflex": {"buy": False, "maxPct": 0.1, "why": "接力吃面"}, "aiflex2": {"buy": True, "maxPct": 0.2, "why": "异动多"}},
         "falsify": "10:30 前涨停数 <30", "watch": "先进封装"}), encoding="utf-8")
    (rt / "manager_log.jsonl").write_text(json.dumps(
        {"date": "2026-11-09", "slot": "09:26", "ts": "2026-11-09T09:26:40", "regime": "趋势日", "read": "高标走弱",
         "traders": {"aiflex": {"buy": False}, "aiflex2": {"buy": True}}, "falsify": "10:30 前涨停数 <30"}) + "\n", encoding="utf-8")
    (rt / "desk_reports.json").write_text(json.dumps({"ts": "2026-11-09T09:26:40", "reports": [
        {"role": "breadth", "code": "BREADTH", "name": "宽度", "kind": "行情", "line": "涨 1 / 跌 1", "level": "warn", "ts": "09:26"},
        {"role": "high", "code": "HIGH", "name": "高标", "kind": "算术", "line": "高标走弱", "level": "bad", "ts": "09:26"}]}), encoding="utf-8")
    (rt / "flex_log.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"ts": "2026-11-06T14:35:00", "slot": "14:35", "analysis": {"stage": "分歧"}, "orders": [], "rawOrders": 0, "dropped": []},
        {"ts": "2026-11-09T09:27:10", "slot": "09:26", "analysis": {"stage": "分歧", "market": "m", "plan": "盯先进封装"},
         "orders": [], "rawOrders": 2, "candidates": 40, "manager": {"regime": "趋势日", "buy": False},
         "dropped": [{"action": "买入", "code": "600001", "why": "经理今天关了开仓（趋势日）"}, {"action": "买入", "code": "600999", "why": "不在候选里"}]},
    ]) + "\n", encoding="utf-8")
    (rt / "flex2_log.jsonl").write_text(json.dumps(
        {"ts": "2026-11-09T09:27:50", "slot": "09:26", "analysis": {"stage": "分歧", "plan": "跟放量票"},
         "orders": [{"action": "买入", "code": "600002", "pct": 0.2, "reason": "放量突破"}], "rawOrders": 1, "candidates": 40,
         "dropped": []}, ensure_ascii=False) + "\n", encoding="utf-8")
    return rt


def test_desk_payload_has_roles_manager_traders_funnel_log_and_params(tmp_path):
    p = desk.desk_payload(_db(tmp_path), _runtime(tmp_path), {"600002": Q(20.0, 19.0)},
                          {"available": True, "complete": True, "up": 3600, "down": 1200}, at(10, 0))
    roles = {r["role"]: r for r in p["roles"]}
    assert roles["breadth"]["line"].startswith("涨 3600") and roles["breadth"]["level"] == "ok"   # 宽度换成秒级的
    assert roles["high"]["level"] == "bad"
    assert p["manager"]["regime"] == "趋势日" and p["manager"]["traders"]["aiflex"]["buy"] is False
    assert len(p["manager"]["history"]) == 1
    t1, t2 = (next(t for t in p["traders"] if t["sid"] == s) for s in ("aiflex", "aiflex2"))
    assert t1["label"] == ft.TRADERS["aiflex"].label and t1["style"]
    assert t1["account"]["positions"][0]["code"] == "600002" and t1["account"]["positions"][0]["pnl"] is not None
    assert t1["account"]["today"] is not None                                     # 有昨天的净值
    assert [r["slot"] for r in t1["rounds"]] == ["09:26"]                         # 只要今天的
    f = t1["funnel"]
    assert f["candidates"] == 40 and f["named"] == 2 and f["manager"] == 1 and f["passed"] == 0 and f["filled"] == 1
    assert t2["funnel"]["passed"] == 1 and t2["account"]["equity"] == 120000
    kinds = {e["tag"] for e in p["log"]}
    assert {"经理", "丢弃", "买入", "成交"} <= kinds
    assert p["log"] == sorted(p["log"], key=lambda e: e["t"], reverse=True)
    names = {x["name"] for x in p["params"]}
    assert "单票上限" in names and "日亏停手" in names
    assert p["calls"] == 3                                                       # 经理 1 次 + 两位交易员各 1 次


def test_desk_payload_survives_missing_everything(tmp_path):
    p = desk.desk_payload(tmp_path / "none.sqlite", tmp_path / "rt", {}, {}, at(10, 0))
    assert p["manager"] is None and len(p["traders"]) == len(ft.TRADERS) and p["log"] == []
    assert all(t["funnel"]["candidates"] == 0 for t in p["traders"])


def test_yesterdays_directive_is_not_shown_as_today(tmp_path):
    rt = _runtime(tmp_path)
    p = desk.desk_payload(_db(tmp_path), rt, {}, {}, at(10, 0) + timedelta(days=1))
    assert p["manager"] is None


def test_desk_payload_carries_todays_strategy_desk_without_evidence_and_ignores_old_days(tmp_path):
    rt = _runtime(tmp_path)
    rec = {"date": "2026-11-09", "slot": "09:26", "ts": "2026-11-09T09:27:00", "final": {"read": "r", "items": [], "fallback": False},
           "traders": [{"key": "relay", "label": "高标接力官", "candidates": 2, "passed": 1, "rejected": [], "error": None,
                        "picks": [{"code": "002086", "name": "甲", "why": "w", "evidence": {"big": 1}}], "vetoed": []}]}
    (rt / "desk_submissions.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    db = _db(tmp_path)
    p = desk.desk_payload(db, rt, {}, {}, at(10, 0))
    sd = p["strategyDesk"]
    assert sd["slot"] == "09:26" and sd["traders"][0]["picks"] == [{"code": "002086", "name": "甲", "why": "w"}]
    (rt / "desk_submissions.json").write_text(json.dumps({**rec, "date": "2026-11-08"}), encoding="utf-8")
    assert desk.desk_payload(db, rt, {}, {}, at(10, 0))["strategyDesk"] is None


def test_positions_carry_the_stop_the_trader_set_and_how_far_price_is_from_it(tmp_path):
    import sqlite3
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy, trace) VALUES (?, '600002', 'stop', '止损 18.0', 'aiflex', ?)",
                 ("2026-11-09T09:40:00", json.dumps({"code": "600002", "stop": 18.0, "invalid": "跌破平台"})))
    conn.commit()
    conn.close()
    p = desk.desk_payload(db, _runtime(tmp_path), {"600002": Q(20.0, 19.0)}, {}, at(10, 0))
    pos = {t["sid"]: t["account"]["positions"] for t in p["traders"]}["aiflex"][0]
    assert pos["stop"] == 18.0 and pos["toStop"] == -10.0           # 现价 20，离止损 18 还有 10%
    assert all("stop" in x for t in p["traders"] for x in t["account"]["positions"])


def test_manager_log_line_joins_a_list_of_falsify_conditions_into_plain_text(tmp_path):
    rt = _runtime(tmp_path)
    (rt / "manager_log.jsonl").write_text(json.dumps(
        {"date": "2026-11-09", "slot": "09:26", "ts": "2026-11-09T09:26:40", "regime": "趋势日", "read": "r",
         "traders": {"aiflex": {"buy": True}}, "falsify": ["涨停数 <30", "炸板率 >40%"]}, ensure_ascii=False) + "\n", encoding="utf-8")
    p = desk.desk_payload(_db(tmp_path), rt, {}, {}, at(10, 0))
    line = next(e["text"] for e in p["log"] if e["who"] == "经理")
    assert "否证：涨停数 <30；炸板率 >40%" in line and "[" not in line


def test_company_scoreboard_compares_today_week_and_month_with_the_targets(tmp_path):
    # dj 2026-11-11：给公司（两位 AI 交易员合计）定目标——每天 1%、每周 5%、每月 20%。只记分，不改护栏
    p = desk.desk_payload(_db(tmp_path), _runtime(tmp_path), {"600002": Q(20.0, 19.0)}, {}, at(10, 0))   # 11-09 是周一
    c = p["company"]
    now = sum(t["account"]["equity"] for t in p["traders"])
    assert c["equity"] == round(now, 2) and [i["key"] for i in c["items"]] == ["day", "week", "month"]
    day, week, month = c["items"]
    assert (day["target"], week["target"], month["target"]) == (1.0, 5.0, 20.0)
    assert day["actual"] == week["actual"] == round((now / (119000 + 120000) - 1) * 100, 2)   # 上周五有净值的用净值，没有的用本金
    assert month["actual"] == round((now / 240000 - 1) * 100, 2) and day["hit"] is (day["actual"] >= 1.0)


def test_payload_carries_the_managers_own_account_with_its_lower_target_and_todays_thinking(tmp_path):
    # dj 2026-11-11：经理自己操作一万元账户（单票全仓），目标比公司低；它的盈亏就是对经理的考核
    rt = _runtime(tmp_path)
    (rt / "manager_trade_log.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"date": "2026-11-06", "ts": "2026-11-06T10:31:00", "slot": "10:30", "think": "旧的", "plan": "", "orders": []},
        {"date": "2026-11-09", "ts": "2026-11-09T09:31:00", "slot": "09:26", "think": "接力最强", "plan": "明天冲高走",
         "orders": [{"action": "买入", "code": "600002", "reason": "两家都推", "stop": 18.0}],
         "dropped": [{"action": "买入", "code": "600888", "why": "一万块买不起一手（现价 150）"}]}]) + "\n", encoding="utf-8")
    p = desk.desk_payload(_db(tmp_path), rt, {}, {}, at(10, 0))
    m = p["mgr"]
    assert m["account"]["equity"] == 10000 and m["account"]["positions"] == [] and m["label"] == "经理账户"
    assert [i["target"] for i in m["score"]["items"]] == [0.5, 2.5, 10.0] and m["score"]["items"][0]["actual"] == 0.0
    assert len(m["rounds"]) == 1 and m["rounds"][0]["think"] == "接力最强" and m["rounds"][0]["orders"][0]["code"] == "600002"
    mine = [e for e in p["log"] if e["who"] == "经理账户"]
    assert {e["tag"] for e in mine} == {"判断", "买入", "丢弃"} and any("买不起一手" in e["text"] for e in mine)
    assert desk.desk_payload(tmp_path / "none.sqlite", tmp_path / "rt2", {}, {}, at(10, 0))["mgr"]["account"]["equity"] == 10000


def test_payload_shows_the_managers_risk_stance_and_the_latest_weekly_review(tmp_path):
    # 周复盘会（dj 2026-11-11）：经理每周给自己定风险档位；还没开过会时显示老板的默认风格「中高」
    rt, db = _runtime(tmp_path), _db(tmp_path)
    p = desk.desk_payload(db, rt, {}, {}, at(10, 0))
    assert p["stance"]["level"] == "中高" and p["stance"]["week"] is None and p["weekly"] is None
    (rt / "manager_stance.json").write_text(json.dumps({"week": "2026-W45", "level": "高", "why": "普涨放量", "rules": ["开足"]}, ensure_ascii=False), encoding="utf-8")
    (rt / "weekly_review.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"week": "2026-W44", "level": "中", "summary": "旧的"},
        {"week": "2026-W45", "level": "高", "summary": "赚在 PCB", "rules": ["开足"], "traders": {"aiflex": "少做 2 板"},
         "statements": {"aiflex": {"label": "AI灵活交易", "good": "g", "bad": "b", "next": "n"}}, "stats": {"big": 1}}]) + "\n", encoding="utf-8")
    p = desk.desk_payload(db, rt, {}, {}, at(10, 0))
    assert p["stance"]["level"] == "高" and p["weekly"]["summary"] == "赚在 PCB" and p["weekly"]["statements"]["aiflex"]["good"] == "g"
    assert "big" not in p["weekly"]["stats"]                         # 原始统计不整块端出来，只留面板用得上的


def test_weekly_review_on_the_panel_carries_the_weeks_numbers_and_past_risk_levels(tmp_path):
    # dj 2026-11-11：「复盘要写在面板上」——不只是结论，这一周每个人的成绩和历次档位也要在
    rt, db = _runtime(tmp_path), _db(tmp_path)
    stats = {"traders": {"aiflex": {"label": "AI灵活交易", "weekPct": 1.2, "closed": 3, "wins": 2, "pnlYuan": 900.0, "best": {"code": "600001", "pct": 9.8},
                                    "worst": {"code": "600002", "pct": -5.2}, "equity": 121440, "buys": 4}},
             "mgr": {"weekPct": 2.1, "closed": 1, "wins": 1, "pnlYuan": 210.0, "best": None, "worst": None, "equity": 10210, "buys": 1},
             "company": {"equity": 241000, "items": [{"key": "week", "label": "本周", "target": 5.0, "actual": 0.4, "hit": False}]}, "regimes": [{"date": "2026-11-09", "regime": "打板日"}]}
    (rt / "weekly_review.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"week": "2026-W44", "level": "中", "summary": "旧的"},
        {"week": "2026-W45", "level": "高", "summary": "赚在 PCB", "stats": stats}]) + "\n", encoding="utf-8")
    w = desk.desk_payload(db, rt, {}, {}, at(10, 0))["weekly"]
    assert w["history"] == [{"week": "2026-W44", "level": "中"}, {"week": "2026-W45", "level": "高"}]
    assert w["stats"]["traders"]["aiflex"] == {"label": "AI灵活交易", "weekPct": 1.2, "closed": 3, "wins": 2, "pnlYuan": 900.0,
                                               "best": {"code": "600001", "pct": 9.8}, "worst": {"code": "600002", "pct": -5.2}}
    assert w["stats"]["mgr"]["weekPct"] == 2.1 and w["stats"]["companyWeek"] == 0.4 and w["stats"]["regimes"][0]["regime"] == "打板日"
