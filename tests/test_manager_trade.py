"""经理的一万元账户（dj 2026-11-11）：像经理自己在炒股——单票全仓，票只能从交易员给的数据里挑；这个账户的盈亏就是对经理的考核。"""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from dashboard import manager_trade as mt

NOW = datetime(2026, 11, 11, 10, 31)


def q(price=10.0, pct=3.0, sealed=False, name="甲"):
    return {"name": name, "price": price, "pct": pct, "sealed": sealed, "amount亿": 5.0}


def rows():
    return {"aiflex": [{"ts": "2026-11-11T09:27:00", "slot": "09:26", "orders": [{"action": "买入", "code": "600001", "reason": "旧的一轮"}], "picks": []},
                       {"ts": "2026-11-11T10:01:00", "slot": "10:00", "analysis": {"stage": "发酵"},
                        "orders": [{"action": "买入", "code": "002086", "pattern": "接力", "reason": "6 板高开转强", "stop": 9.4}],
                        "picks": [{"code": "600111", "verdict": "推荐", "why": "回封"}, {"code": "600222", "verdict": "不推荐", "why": "滞涨"}],
                        "dropped": [{"action": "买入", "code": "600333", "why": "经理今天关了开仓（混沌日）"}, {"action": "买入", "code": "600444", "why": "不在候选里"}]}],
            "aiflex2": [{"ts": "2026-11-11T10:02:00", "slot": "10:00", "orders": [{"action": "卖出", "code": "600555", "reason": "破位"}],
                         "picks": [{"code": "300001", "verdict": "推荐", "why": "创业板不做"}]}]}


def subs():
    return {"date": "2026-11-11", "slot": "10:30", "traders": [{"label": "补涨官", "picks": [{"code": "600666", "name": "丙", "why": "同题材首板"}]}],
            "final": {"items": [{"code": "600666", "do": True, "why": "两家推"}]}}


def accounts():
    return {"aiflex": {"positions": [{"code": "600777", "pnlPct": 2.0}]}, "aiflex2": {"positions": []},
            "mgr": {"equity": 10000.0, "cash": 10000.0, "positions": []}}


def test_pool_only_holds_what_the_traders_brought_today_with_who_brought_it():
    pool = mt.build_pool(rows(), subs(), accounts())
    assert set(pool) == {"002086", "600111", "600333", "600666", "600777"}        # 上一轮的、不推荐的、编出来的、卖单、创业板都不进
    assert pool["002086"][0].startswith("AI灵活交易 下单") and "6 板高开转强" in pool["002086"][0]
    assert pool["600333"][0].startswith("AI灵活交易 想买") and any("经理点了要做" in s for s in pool["600666"])
    assert pool["600777"] == ["AI灵活交易 持仓 +2.0%"]
    assert mt.build_pool({}, None, {}) == {}


def ctx(**kw):
    base = {"snapshotId": "MGRT-20261111-1030", "pool": {"002086": {"from": ["AI灵活交易 下单"], **q()}, "600111": {"from": ["x"], **q(sealed=True)},
                                                        "600888": {"from": ["x"], **q(price=150.0)}},
            "account": {"equity": 10000.0, "cash": 10000.0, "positions": [{"code": "600999", "canSellToday": True}]}}
    base.update(kw)
    return base


def buy(code, **kw):
    return {"action": "买入", "code": code, "stop": 9.5, "invalid": "跌破 9.5 说明接力失败", "ref_price": 10.0, "reason": "最强的一只", **kw}


def test_orders_are_all_in_and_only_from_the_pool_and_only_what_ten_thousand_can_buy():
    ans = {"snapshotId": "MGRT-20261111-1030", "think": "t", "plan": "p", "orders": [
        {"action": "卖出", "code": "600999", "reason": "换股"}, buy("002086", pct=0.2), buy("600111"), buy("600888", stop=140, ref_price=150),
        buy("600000"), {"action": "卖出", "code": "600001", "reason": "没有"}]}
    r = mt.validate(ans, ctx())
    assert [(o["action"], o["code"]) for o in r["orders"]] == [("卖出", "600999"), ("买入", "002086")]
    o = r["orders"][1]
    assert o["pct"] == 1.0 and o["mode"] == "模式内" and o["id"] == "MGRT-20261111-1030-2" and o["pattern"]
    why = {d["code"]: d["why"] for d in r["dropped"]}
    assert "封板" in why["600111"] and "买不起一手" in why["600888"] and "不在交易员给的票里" in why["600000"] and "没有这只持仓" in why["600001"]
    with pytest.raises(ValueError):
        mt.validate({**ans, "snapshotId": "X"}, ctx())


def test_only_one_buy_per_round_because_there_is_only_one_slot():
    empty = ctx(account={"equity": 10000.0, "cash": 10000.0, "positions": []})
    r = mt.validate({"snapshotId": "MGRT-20261111-1030", "think": "", "plan": "", "orders": [buy("002086"), buy("002086")]}, empty)
    assert len(r["orders"]) == 1 and "只有一个仓位" in r["dropped"][0]["why"]
    held = mt.validate({"snapshotId": "MGRT-20261111-1030", "think": "", "plan": "", "orders": [buy("002086")]}, ctx())   # 手里还有 600999 没卖
    assert held["orders"] == [] and "先卖掉才能换" in held["dropped"][0]["why"]
    locked = ctx(account={"equity": 10000.0, "cash": 100.0, "positions": [{"code": "600999", "canSellToday": False}]})
    r = mt.validate({"snapshotId": "MGRT-20261111-1030", "think": "", "plan": "", "orders": [{"action": "卖出", "code": "600999", "reason": "x"}]}, locked)
    assert r["orders"] == [] and "T+1" in r["dropped"][0]["why"]


def scene(pool=None, positions=None):
    return {"pool": {"002086": {"from": ["AI灵活交易 下单"], **q()}} if pool is None else pool,
            "account": {"equity": 10000.0, "cash": 10000.0, "positions": positions or []},
            "directive": {"regime": "打板日", "read": "r"}, "score": {"items": [{"key": "day", "target": 0.5, "actual": 0.0, "gapYuan": 50}]}}


def paths(tmp_path):
    return dict(orders_path=tmp_path / "o.json", log_path=tmp_path / "l.jsonl", state_path=tmp_path / "s.json")


def test_run_writes_orders_for_the_executor_logs_the_round_and_pushes_when_it_trades(tmp_path):
    sent = []
    notifier = SimpleNamespace(send=lambda t, b: sent.append((t, b)), flush=lambda timeout: None)

    def ask(system, prompt, schema):
        assert "002086" in prompt and "gapYuan" in prompt
        return {"snapshotId": schema["properties"]["snapshotId"]["const"], "think": "接力最强", "plan": "明天冲高走", "orders": [buy("002086")]}
    out = mt.run(NOW, "10:30", gather=lambda n, s: scene(), ask=ask, notifier=notifier, clock=lambda: NOW, **paths(tmp_path))
    assert out == {"slot": "10:30", "orders": 1}
    o = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))
    assert o["ts"] == "2026-11-11T10:31:00" and o["orders"][0]["code"] == "002086" and o["orders"][0]["pct"] == 1.0
    assert json.loads((tmp_path / "l.jsonl").read_text(encoding="utf-8").splitlines()[-1])["think"] == "接力最强"
    assert len(sent) == 1 and "经理账户" in sent[0][0] and "002086" in sent[0][1] and "全仓" in sent[0][1]
    assert mt.due_slot(NOW, tmp_path / "s.json") is None


def test_run_does_not_call_the_model_when_there_is_nothing_to_buy_and_nothing_held(tmp_path):
    def must_not(*a):
        raise AssertionError("没票可挑、也没持仓，不该花一次调用")
    out = mt.run(NOW, "10:30", gather=lambda n, s: scene(pool={}), ask=must_not, notifier=None, **paths(tmp_path))
    assert out == {"slot": "10:30", "orders": 0, "skipped": "交易员今天还没给票，账户空仓"}
    assert json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))["orders"] == []


def test_run_stays_quiet_when_the_manager_decides_to_do_nothing(tmp_path):
    sent = []
    notifier = SimpleNamespace(send=lambda t, b: sent.append(t), flush=lambda timeout: None)
    ask = lambda s, p, sc: {"snapshotId": sc["properties"]["snapshotId"]["const"], "think": "都不够好", "plan": "等", "orders": []}   # noqa: E731
    assert mt.run(NOW, "10:30", gather=lambda n, s: scene(), ask=ask, notifier=notifier, **paths(tmp_path))["orders"] == 0
    assert sent == []


def test_due_slot_has_a_late_session_round_and_skips_weekends(tmp_path):
    st = tmp_path / "s.json"
    assert mt.due_slot(datetime(2026, 11, 11, 14, 36), st) == "14:35" and mt.due_slot(datetime(2026, 11, 11, 9, 27), st) == "09:26"
    assert mt.due_slot(datetime(2026, 11, 11, 9, 50), st) is None and mt.due_slot(datetime(2026, 11, 14, 10, 31), st) is None


def test_prompt_tells_the_manager_the_real_constraints():
    for text in ("一万", "全仓", "T+1", "100 股", "止损", "目标"):
        assert text in mt.SYSTEM_PROMPT, text


def test_module_never_touches_real_orders():
    import pathlib
    text = pathlib.Path(mt.__file__).read_text(encoding="utf-8")
    for bad in ("GatewayBroker", "make_broker", "live_intents", "live_orders"):
        assert bad not in text
