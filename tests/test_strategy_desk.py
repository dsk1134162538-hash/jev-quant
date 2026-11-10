"""策略交易员 → 经理 → dj（dj 2026-11-10）：规则把关、交易员只能否决、经理只能从递上来的票里挑、全程不下单。"""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from dashboard import strategy_desk as sd

NOW = datetime(2026, 11, 10, 9, 27)


def q(code, open_pct, *, name=None, pct=None, day=NOW):
    return SimpleNamespace(name=name or f"票{code}", price=10.0, open=10.0, open_pct=open_pct, pct=open_pct if pct is None else pct,
                           amount=5e8, turnover=8.0, volume_ratio=2.0, limit_up=11.0, ts=day)


def write_targets(path, **kw):
    path.write_text(json.dumps({"date": "2026-11-10", **kw}), encoding="utf-8")


def test_read_candidates_only_today_and_main_board(tmp_path):
    p = tmp_path / "t.json"
    write_targets(p, relay={"002086": 3, "300001": 4}, wts={"002388": 3}, bz={"600111": {"theme": "机器人", "leader": 5}})
    got = sd.read_candidates(NOW, p)
    assert set(got["relay"]) == {"002086"} and got["wts"] == {"002388": {"n": 3}} and got["bz"]["600111"]["theme"] == "机器人"
    p.write_text(json.dumps({"date": "2026-11-09", "relay": {"002086": 3}}), encoding="utf-8")
    assert sd.read_candidates(NOW, p) == {}
    assert sd.read_candidates(NOW, tmp_path / "nope.json") == {}


def test_screen_lets_only_the_gap_window_through_and_never_guesses_without_auction_data():
    s = sd.STRATS["relay"]                                    # 3~6%
    quotes = {"002086": q("002086", 4.0), "002578": q("002578", 7.5), "002111": q("002111", 1.0),
              "002222": q("002222", 4.0, day=datetime(2026, 11, 9, 15, 0))}
    passed, rejected = sd.screen(s, {c: {"n": 5} for c in ("002086", "002578", "002111", "002222", "002333")}, quotes, {}, NOW)
    assert [p["code"] for p in passed] == ["002086"] and passed[0]["连板数"] == 5 and passed[0]["今日开盘涨幅%"] == 4.0
    why = {r["code"]: r["why"] for r in rejected}
    assert "不在 3~6%" in why["002578"] and "不在 3~6%" in why["002111"] and "没有今天" in why["002222"] and "没有今天" in why["002333"]


def test_validate_verdicts_ignores_unknown_codes_and_treats_silence_as_no():
    passed = [{"code": "002086", "name": "甲"}, {"code": "002578", "name": "乙"}]
    ans = {"snapshotId": "S", "verdicts": [{"code": "002086", "buy": True, "why": "w", "falsify": "f", "confidence": "高"},
                                           {"code": "999999", "buy": True, "why": "加的票", "falsify": "", "confidence": "高"}]}
    out = sd.validate_verdicts(ans, "S", passed)
    assert [(v["code"], v["buy"]) for v in out] == [("002086", True), ("002578", False)] and out[1]["why"] == "交易员没表态"
    with pytest.raises(ValueError):
        sd.validate_verdicts({**ans, "snapshotId": "X"}, "S", passed)


def scene(cands, quotes):
    return {"ctx": {"market": {"breadth": {"up": 3000}}}, "cands": cands, "quotes": quotes}


def fake_ask_factory(buy_codes, calls):
    def ask(system, prompt, schema):
        calls.append(system[:20])
        codes = schema["properties"]["verdicts"]["items"]["properties"]["code"]["enum"]
        return {"snapshotId": schema["properties"]["snapshotId"]["const"],
                "verdicts": [{"code": c, "buy": c in buy_codes, "why": "量比 2", "falsify": "跌破昨收", "confidence": "中"} for c in codes]}
    return ask


def test_trader_asks_only_when_rule_passed_something_and_keeps_vetoes():
    calls = []
    s = sd.STRATS["relay"]
    sub = sd.run_trader(s, NOW, "09:26", scene({"relay": {"002086": {"n": 4}, "002578": {"n": 3}}},
                                              {"002086": q("002086", 4.0), "002578": q("002578", 4.5)}), fake_ask_factory({"002086"}, calls))
    assert [p["code"] for p in sub["picks"]] == ["002086"] and [v["code"] for v in sub["vetoed"]] == ["002578"] and len(calls) == 1
    calls.clear()                                              # 规则一只都没放行：不花额度
    none = sd.run_trader(s, NOW, "09:26", scene({"relay": {"002086": {"n": 4}}}, {"002086": q("002086", 9.0)}), fake_ask_factory(set(), calls))
    assert calls == [] and none["picks"] == [] and none["rejected"][0]["code"] == "002086"
    assert sd.run_trader(sd.STRATS["wts"], NOW, "09:26", scene({}, {}), fake_ask_factory(set(), calls))["candidates"] == 0 and calls == []


def test_trader_failure_stays_in_its_own_submission():
    def boom(*a):
        raise TimeoutError("超时")
    sub = sd.run_trader(sd.STRATS["relay"], NOW, "09:26", scene({"relay": {"002086": {"n": 4}}}, {"002086": q("002086", 4.0)}), boom)
    assert "TimeoutError" in sub["error"] and sub["picks"] == []


def test_final_only_picks_from_submitted_and_clamps_pct():
    subs = [{"label": "高标接力官", "picks": [{"code": "002086", "name": "甲", "why": "a", "falsify": "b", "confidence": "高", "evidence": {}}]},
            {"label": "补涨官", "picks": [{"code": "002086", "name": "甲", "why": "c", "falsify": "d", "confidence": "中", "evidence": {}}]}]
    seen = {}

    def ask(system, prompt, schema):
        seen["codes"] = schema["properties"]["items"]["items"]["properties"]["code"]["enum"]
        return {"snapshotId": schema["properties"]["snapshotId"]["const"], "read": "r", "items": [
            {"code": "002086", "do": True, "pct": 0.9, "why": "两家推", "falsify": "炸板"},
            {"code": "600000", "do": True, "pct": 0.1, "why": "加的票", "falsify": ""}]}
    out = sd.run_final(NOW, "09:26", subs, {}, {"regime": "打板日"}, ask)
    assert seen["codes"] == ["002086"] and [i["code"] for i in out["items"]] == ["002086"]
    assert out["items"][0]["pct"] == sd.MAX_PCT and out["items"][0]["strategies"] == ["高标接力官", "补涨官"] and not out["fallback"]


def test_final_without_picks_does_not_call_and_failure_falls_back_to_raw_picks():
    def must_not(*a):
        raise AssertionError("没有票不该调用")
    assert sd.run_final(NOW, "09:26", [{"label": "x", "picks": []}], {}, None, must_not)["items"] == []

    def boom(*a):
        raise RuntimeError("坏了")
    subs = [{"label": "弱转强官", "picks": [{"code": "002388", "name": "丙", "why": "a", "falsify": "b", "confidence": "中", "evidence": {}}]}]
    out = sd.run_final(NOW, "09:26", subs, {}, None, boom)
    assert out["fallback"] and out["items"][0]["do"] and "经理汇总失败" in out["read"]
    assert "仓位未定" in sd.push_text(NOW, "09:26", [], out)


def run(tmp_path, slot, now, cands, quotes, buy, sent, directive=None):
    paths = dict(submit_path=tmp_path / "s.json", log_path=tmp_path / "l.jsonl", state_path=tmp_path / "st.json")
    notifier = SimpleNamespace(send=lambda t, c: sent.append((t, c)), flush=lambda timeout: None)

    def ask(system, prompt, schema):
        props = schema["properties"]
        if "verdicts" in props:
            return fake_ask_factory(buy, [])(system, prompt, schema)
        codes = props["items"]["items"]["properties"]["code"]["enum"]
        return {"snapshotId": props["snapshotId"]["const"], "read": "r",
                "items": [{"code": c, "do": True, "pct": 0.1, "why": "过", "falsify": "跌破"} for c in codes]}
    return sd.run_desk(now, slot, gather=lambda n, s: scene(cands, quotes), ask=ask, notifier=notifier, directive=directive, **paths)


def test_run_desk_pushes_morning_even_when_empty_then_only_on_new_picks(tmp_path):
    sent = []
    out = run(tmp_path, "09:26", NOW, {}, {}, set(), sent)                      # 今天没候选：09:26 照样推一条
    assert sent and "本时点没有" in sent[0][1] and out["do"] == []
    sent.clear()
    cands = {"relay": {"002086": {"n": 4}}}
    quotes = {"002086": q("002086", 4.0)}
    out = run(tmp_path, "10:30", datetime(2026, 11, 10, 10, 31), cands, quotes, {"002086"}, sent)
    assert out["do"] == ["002086"] and len(sent) == 1 and "仓位 10%" in sent[0][1] and "002086" in sent[0][0] + sent[0][1]
    sent.clear()
    run(tmp_path, "13:20", datetime(2026, 11, 10, 13, 21), cands, quotes, {"002086"}, sent)   # 同一只已经推过：不刷屏
    assert sent == []
    record = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert record["slot"] == "13:20" and json.loads((tmp_path / "st.json").read_text(encoding="utf-8"))["done"] == ["09:26", "10:30", "13:20"]
    assert len((tmp_path / "l.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def test_due_slot_window_and_done(tmp_path):
    st = tmp_path / "st.json"
    assert sd.due_slot(datetime(2026, 11, 10, 9, 26), st) == "09:26"
    assert sd.due_slot(datetime(2026, 11, 10, 9, 50), st) is None             # 过了 20 分钟窗口不补
    assert sd.due_slot(datetime(2026, 11, 14, 9, 30), st) is None             # 周六
    st.write_text(json.dumps({"date": "2026-11-10", "done": ["09:26"]}), encoding="utf-8")
    assert sd.due_slot(datetime(2026, 11, 10, 9, 30), st) is None


def test_module_never_touches_orders_or_accounts():
    import pathlib
    text = pathlib.Path(sd.__file__).read_text(encoding="utf-8")
    for bad in ("orders_path", "ORDERS_PATH", "GatewayBroker", "make_broker", "ledger", "live_intents"):
        assert bad not in text


def test_daemon_writes_bz_pool_so_the_bz_trader_can_read_it(tmp_path):
    import run
    from datetime import date
    p = tmp_path / "t.json"
    run._write_strategy_targets(date(2026, 11, 10), {"002086": 4, "000001": 1}, {"002388": 3}, lambda m: None, path=p,
                                bz={"600111": {"theme": "机器人", "leader": 6}})
    got = sd.read_candidates(NOW, p)
    assert got["relay"] == {"002086": {"n": 4}} and got["wts"] == {"002388": {"n": 3}} and got["bz"]["600111"]["leader"] == 6
