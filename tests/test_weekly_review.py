"""每周复盘会（dj 2026-11-11）：周五收盘后经理和交易员开会——交易员各自复盘，经理总评并给自己定下周的风险档位（低 / 中 / 中高 / 高）。
老板的风格偏中高风险，经理默认也是中高，再按市场调整；档位写进下周每一次判断的现场。"""
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

from dashboard import flex_trader as ft
from dashboard import manager as mg
from dashboard import weekly_review as wr

FRI = datetime(2026, 11, 13, 15, 41)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE fills(id INTEGER PRIMARY KEY, account TEXT, code TEXT, side TEXT, price REAL, shares INTEGER, fee REAL, ts TEXT, reason TEXT)")
    conn.execute("CREATE TABLE equity(date TEXT, account TEXT, value REAL)")
    rows = [("aiflex_main", "600001", "buy", 10.0, 1000, 5.0, "2026-11-06T10:00:00", "上周买的"),
            ("aiflex_main", "600001", "sell", 11.0, 1000, 16.0, "2026-11-10T09:31:00", "冲高走"),          # +979
            ("aiflex_main", "600002", "buy", 20.0, 500, 5.0, "2026-11-11T10:00:00", "接力"),
            ("aiflex_main", "600002", "sell", 19.0, 500, 15.0, "2026-11-12T09:31:00", "止损"),             # -520
            ("aiflex_main", "600003", "buy", 30.0, 300, 5.0, "2026-11-13T10:00:00", "还拿着"),
            ("aiflex2_main", "600009", "sell", 9.0, 100, 5.0, "2026-11-05T10:00:00", "上周的卖出不算")]
    conn.executemany("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO equity VALUES (?,?,?)", [("2026-11-06", "aiflex_main", 120000), ("2026-11-13", "aiflex_main", 121200),
                                                           ("2026-11-13", "aiflex2_main", 119400)])
    return conn


def test_week_stats_count_only_trades_closed_this_week_with_win_rate_best_and_worst():
    s = wr.week_stats(_conn(), "aiflex_main", FRI)
    assert s["closed"] == 2 and s["wins"] == 1 and s["buys"] == 2 and s["pnlYuan"] == 459.0
    assert s["best"]["code"] == "600001" and s["best"]["pct"] == 9.79 and s["worst"]["code"] == "600002" and s["worst"]["pct"] == -5.2
    assert s["weekPct"] == 1.0 and s["equity"] == 121200
    empty = wr.week_stats(_conn(), "aiflex2_main", FRI)
    assert empty["closed"] == 0 and empty["best"] is None and empty["weekPct"] == -0.5      # 上周没有净值记录，用本金 12 万当基数


def test_due_only_after_the_close_on_friday_and_once_a_week(tmp_path):
    st = tmp_path / "s.json"
    assert wr.due(FRI, st) and not wr.due(datetime(2026, 11, 13, 14, 0), st) and not wr.due(datetime(2026, 11, 12, 15, 41), st)
    st.write_text(json.dumps({"week": "2026-W46"}), encoding="utf-8")
    assert not wr.due(FRI, st) and wr.due(datetime(2026, 11, 20, 15, 41), st)


def scene():
    return {"week": "2026-W46", "days": ["2026-11-09", "2026-11-13"],
            "traders": {"aiflex": {"label": "AI灵活交易", "closed": 2, "wins": 1, "weekPct": 1.0}, "aiflex2": {"label": "AI趋势交易员", "closed": 0, "weekPct": -0.5}},
            "mgr": {"closed": 1, "wins": 1, "weekPct": 2.1}, "company": {"items": [{"key": "week", "target": 5.0, "actual": 0.25}]},
            "regimes": [{"date": "2026-11-10", "regime": "打板日"}, {"date": "2026-11-11", "regime": "混沌日"}], "market": {"up": 2055, "down": 3282}}


def paths(tmp_path):
    return dict(stance_path=tmp_path / "stance.json", log_path=tmp_path / "w.jsonl", state_path=tmp_path / "s.json")


def test_meeting_lets_each_trader_speak_then_the_manager_sets_his_risk_level_for_next_week(tmp_path):
    asked, sent = [], []

    def ask(system, prompt, schema):
        asked.append(system[:12])
        snap = schema["properties"]["snapshotId"]["const"]
        assert snap in prompt
        if "risk" in schema["properties"]:
            assert "AI灵活交易" in prompt and "接力做对了" in prompt and "中高" in system
            return {"snapshotId": snap, "summary": "本周赚在 PCB，亏在高标接力。", "risk": {"level": "高", "why": "普涨放量，主线清楚", "market": "趋势向上"},
                    "rules": ["打板日把灵活开足", "混沌日只留一只", "x", "多出来的不要"], "traders": {"aiflex": "下周少做 2 板", "aiflex2": "保持"}}
        return {"snapshotId": snap, "good": "接力做对了", "bad": "止损慢", "next": "只做最高标"}
    notifier = SimpleNamespace(send=lambda t, b: sent.append((t, b)), flush=lambda timeout: None)
    out = wr.run(FRI, gather=lambda now: scene(), ask=ask, notifier=notifier, **paths(tmp_path))
    assert out == {"week": "2026-W46", "level": "高"} and len(asked) == 3
    stance = json.loads((tmp_path / "stance.json").read_text(encoding="utf-8"))
    assert stance["level"] == "高" and stance["week"] == "2026-W46" and len(stance["rules"]) == 3 and stance["why"]
    rec = json.loads((tmp_path / "w.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["statements"]["aiflex"]["good"] == "接力做对了" and rec["summary"].startswith("本周")
    assert "周复盘" in sent[0][0] and "高" in sent[0][0] and "AI灵活交易" in sent[0][1] and "下周少做 2 板" in sent[0][1]
    assert not wr.due(FRI, tmp_path / "s.json")


def test_a_trader_who_fails_to_report_does_not_stop_the_meeting_and_a_bad_level_falls_back_to_medium_high(tmp_path):
    def ask(system, prompt, schema):
        snap = schema["properties"]["snapshotId"]["const"]
        if "risk" not in schema["properties"]:
            raise TimeoutError("超时")
        return {"snapshotId": snap, "summary": "s", "risk": {"level": "激进", "why": "w"}, "rules": [], "traders": {}}
    out = wr.run(FRI, gather=lambda now: scene(), ask=ask, notifier=None, **paths(tmp_path))
    assert out["level"] == "中高"
    rec = json.loads((tmp_path / "w.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert "TimeoutError" in rec["statements"]["aiflex"]["error"]


def test_stance_defaults_to_the_bosss_medium_high_until_the_first_meeting(tmp_path):
    s = wr.read_stance(tmp_path / "none.json")
    assert s["level"] == "中高" and "老板" in s["why"] and s["week"] is None
    (tmp_path / "x.json").write_text(json.dumps({"week": "2026-W46", "level": "中", "why": "退潮", "rules": ["少做"]}), encoding="utf-8")
    assert wr.read_stance(tmp_path / "x.json")["level"] == "中"


def test_manager_sees_his_stance_every_time_he_decides():
    mctx = mg.manager_context(datetime(2026, 11, 16, 9, 27), "09:26", {}, [], {}, {}, stance={"level": "中高", "why": "w", "rules": ["a"]})
    assert mctx["stance"]["level"] == "中高" and "stance" in mg.SYSTEM_PROMPT and "中高" in mg.SYSTEM_PROMPT
    assert "stance" not in mg.manager_context(datetime(2026, 11, 16, 9, 27), "09:26", {}, [], {}, {})
    assert set(ft.TRADERS) <= set(wr.TRADER_ASK)                         # 每位交易员都有自己的复盘提问


def test_week_stats_split_the_cost_when_a_position_is_sold_in_parts():
    # 越涨越卖（dj 2026-11-11）：分批卖出的每一笔只摊它卖掉的那部分成本，不能把第一笔算成巨亏
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE fills(id INTEGER PRIMARY KEY, account TEXT, code TEXT, side TEXT, price REAL, shares INTEGER, fee REAL, ts TEXT, reason TEXT)")
    conn.execute("CREATE TABLE equity(date TEXT, account TEXT, value REAL)")
    conn.executemany("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) VALUES (?,?,?,?,?,?,?,?)", [
        ("aiflex_main", "600001", "buy", 10.0, 900, 0.0, "2026-11-09T10:00:00", "b"),
        ("aiflex_main", "600001", "sell", 11.0, 300, 0.0, "2026-11-10T10:00:00", "分批止盈"),
        ("aiflex_main", "600001", "sell", 12.0, 600, 0.0, "2026-11-11T10:00:00", "移动止盈")])
    s = wr.week_stats(conn, "aiflex_main", FRI)
    assert s["closed"] == 2 and s["wins"] == 2 and s["pnlYuan"] == 1500.0 and s["worst"]["pct"] == 10.0 and s["best"]["pct"] == 20.0
