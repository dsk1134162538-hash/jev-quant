"""高位不买（dj 2026-11-12）：冰山冷热一个月翻倍、20 日涨 79%，交易员和经理只看到「前 2 连板炸板、竞价高开 0.4%」，
当成弱转强买在了山顶。现在候选都带 rise5 / rise20 / fromLow60；rise20 ≥ FLEX_MAX_RISE20 的买单在三处被拦：
交易员回答校验、经理账户校验、执行器（最后一道，AI 绕不过去）。"""
import json
from datetime import date, datetime, timedelta

from factories import quote

from dashboard import flex_trader as ft
from dashboard import manager_trade as mt
from jevq import config, db
from jevq.data.closes import position_stats
from jevq.ledger import Ledger
from jevq.strategies.flex import AiflexStrategy

TODAY = date(2026, 11, 12)
NOW = datetime(2026, 11, 12, 9, 30, 30)


def _closes(conn, code, prices):
    """prices 从旧到新，最后一个是昨收（TODAY 前一天）。"""
    days = [TODAY - timedelta(days=len(prices) - i) for i in range(len(prices))]
    conn.executemany("INSERT INTO closes(date, code, close) VALUES (?, ?, ?)", [(d.isoformat(), code, p) for d, p in zip(days, prices)])


def test_position_stats_uses_only_closes_before_today():
    conn = db.connect(":memory:")
    _closes(conn, "000530", [5.0] * 40 + [6.3] + [8.0] * 19 + [11.28])      # 20 个交易日前 6.3，昨收 11.28
    conn.execute("INSERT INTO closes(date, code, close) VALUES (?, ?, ?)", (TODAY.isoformat(), "000530", 99.0))  # 今天的不算
    _closes(conn, "600001", [10.0, 10.1, 10.2])                             # 历史太短：不给
    st = position_stats(conn, ["000530", "600001"], TODAY)
    assert st["000530"] == {"rise5": 41.0, "rise20": 79.0, "fromLow60": 125.6}
    assert "600001" not in st


def test_executor_rejects_a_buy_twenty_days_up_past_the_limit_and_still_buys_a_normal_one(tmp_path):
    path = tmp_path / "flex_orders.json"
    order = lambda i, code: {"id": f"FLEX-20261112-0926-{i}", "action": "买入", "code": code, "pct": 0.2, "stop": 10.8,  # noqa: E731
                             "ref_price": 11.33, "invalid": "跌破 11 弱转强失败", "mode": "模式内", "pattern": "弱转强", "reason": "x"}
    path.write_text(json.dumps({"cycleId": "FLEX-20261112-0926", "ts": NOW.isoformat(timespec="seconds"),
                                "orders": [order(1, "000530"), order(2, "600002")]}, ensure_ascii=False), encoding="utf-8")
    conn = db.connect(":memory:")
    _closes(conn, "000530", [6.0] * 40 + [6.3] + [8.0] * 19 + [11.28])
    _closes(conn, "600002", [11.0] * 61)
    s = AiflexStrategy(conn, Ledger(conn), [TODAY], orders_path=path)
    q = {c: quote(c, pct=0.44, pre_close=11.28, ts=NOW) for c in ("000530", "600002")}          # 11.28 × 1.0044 ≈ 11.33
    fills = s.buy_step(q, NOW)
    assert [f.code for f in fills] == ["600002"]
    reject = conn.execute("SELECT detail FROM decisions WHERE verdict='order_reject' AND code='000530'").fetchone()[0]
    assert "高位不买" in reject and "79%" in reject


def test_trader_and_manager_validators_drop_high_buys_before_they_reach_the_executor():
    hi = {"name": "冰山冷热", "price": 11.33, "pct": 0.4, "rise20": 79.0}
    ctx = {"snapshotId": "FLEX-20261112-0926", "quotes": {"000530": hi, "600002": {"name": "乙", "price": 11.0, "pct": 1.0, "rise20": 5.0}},
           "account": {"positions": []}}
    o = {"action": "买入", "pct": 0.2, "stop": 10.8, "ref_price": 11.33, "invalid": "跌破 11", "mode": "模式内", "pattern": "弱转强", "reason": "x"}
    r = ft.validate_answer({"snapshotId": ctx["snapshotId"], "analysis": {}, "orders": [{**o, "code": "000530"}, {**o, "code": "600002"}]}, ctx)
    assert [x["code"] for x in r["orders"]] == ["600002"]
    assert r["dropped"][0]["code"] == "000530" and "高位不买" in r["dropped"][0]["why"]

    mctx = {"snapshotId": "MGRT-20261112-1030", "account": {"equity": 10000.0, "cash": 10000.0, "positions": []},
            "pool": {"000530": {"from": ["x"], **hi, "amount亿": 14.8, "sealed": False}}}
    m = mt.validate({"snapshotId": "MGRT-20261112-1030", "think": "", "plan": "", "orders": [
        {"action": "买入", "code": "000530", "stop": 10.62, "invalid": "跌破均价线", "ref_price": 11.35, "reason": "双人印证"}]}, mctx)
    assert m["orders"] == [] and "高位不买" in m["dropped"][0]["why"]


def test_prompts_tell_the_traders_and_the_manager_about_the_line():
    for p in (ft.SYSTEM_PROMPT, ft.TREND_PROMPT, mt.SYSTEM_PROMPT):
        assert "高位不买" in p and f"{config.FLEX_MAX_RISE20:.0f}%" in p and "rise20" in p
