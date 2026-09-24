from datetime import time

from jevq.rules.custom import (eval_cond, eval_group, fields_meta, first_exit, rank_codes, validate)

VALS = {"pct": -1.5, "ret_n": {5: 12.0}, "sealed": False, "sentiment": "正常", "time": time(14, 40),
        "hold_days": 1, "pnl_pct": -3.2, "amount": 5.0}


def get(key, n=None):
    v = VALS.get(key)
    return v.get(n) if isinstance(v, dict) else v


def C(field, op, value, n=None):
    c = {"field": field, "op": op, "value": value}
    if n is not None:
        c["param"] = {"n": n}
    return c


def test_eval_basic_ops():
    assert eval_cond(C("pct", "between", [-2, 0]), get)
    assert not eval_cond(C("pct", ">", 0), get)
    assert eval_cond(C("ret_n", ">=", 10, n=5), get)
    assert not eval_cond(C("ret_n", ">=", 10, n=10), get)          # 缺数据 → False
    assert eval_cond(C("sealed", "is", False), get)
    assert eval_cond(C("sentiment", "in", ["正常", "火热"]), get)
    assert eval_cond(C("time", "between", ["14:30", "14:55"]), get)
    assert not eval_cond(C("time", "<", "14:30"), get)
    assert not eval_cond(C("nope", ">", 1), get)


def test_groups_and_exits():
    g_all = {"mode": "all", "conditions": [C("pct", "<", 0), C("amount", ">", 10)]}
    g_any = {"mode": "any", "conditions": [C("pct", "<", 0), C("amount", ">", 10)]}
    assert not eval_group(g_all, get) and eval_group(g_any, get)
    assert eval_group({"conditions": []}, get) and not eval_group({"conditions": []}, get, empty=False)
    exits = [{"when": {"conditions": [C("pnl_pct", ">=", 2)]}, "reason": "止盈"},
             {"when": {"conditions": [C("pnl_pct", "<=", -3)]}, "reason": "止损"}]
    assert first_exit(exits, get) == "止损"


def test_rank_drops_missing_and_orders():
    data = {"a": 3.0, "b": None, "c": 9.0}
    assert rank_codes(["a", "b", "c"], lambda code, k, n: data[code], {"field": "amount", "order": "desc"}) == ["c", "a"]
    assert rank_codes(["a", "b", "c"], lambda code, k, n: data[code], {"field": "amount", "order": "asc"}) == ["a", "c"]


def good():
    return {"name": "尾盘弱转强", "capital": 30000,
            "select": {"mode": "all", "conditions": [C("pct", "between", [-2, 0]), C("ret_n", ">", 10, n=5)]},
            "rank": {"field": "amount", "order": "desc"},
            "entry": {"mode": "all", "conditions": [C("time", "between", ["14:30", "14:55"])]},
            "exits": [{"when": {"conditions": [C("pct", "<=", -3)]}, "reason": "止损"},
                      {"when": {"conditions": [C("time", ">=", "14:55")]}, "reason": "清仓"}],
            "jev": {"notices": True, "news": True}}


def test_validate_good_spec():
    assert validate(good()) == ([], [])


def test_validate_requires_stop_loss_and_select():
    s = good()
    s["exits"] = [{"when": {"conditions": [C("time", ">=", "14:55")]}, "reason": "清仓"}]
    s["select"] = {"conditions": []}
    errors, _ = validate(s)
    assert any("止损" in e for e in errors) and any("选股条件不能为空" in e for e in errors)


def test_validate_contradiction_and_scope_and_ranges():
    s = good()
    s["select"]["conditions"] = [C("pct", ">", 5), C("pct", "<", 3), C("pnl_pct", ">", 0)]
    s["entry"]["conditions"] = [C("time", "between", ["14:55", "14:30"])]
    s["exits"][0]["when"]["conditions"] = [C("pct", "<=", -30)]
    errors, _ = validate(s)
    text = "\n".join(errors)
    assert "互相矛盾" in text and "不能用在这里" in text and "开始晚于结束" in text and "超出范围" in text


def test_validate_warns_without_time_exit():
    s = good()
    s["exits"] = s["exits"][:1]
    errors, warnings = validate(s)
    assert errors == [] and any("长期持有" in w for w in warnings)


def test_validate_param_and_enum():
    s = good()
    s["select"]["conditions"] = [C("ret_n", ">", 1, n=7), C("sentiment", "in", ["大涨"])]
    errors, _ = validate(s)
    assert any("N 只能是" in e for e in errors) and any("只能选" in e for e in errors)


def test_fields_meta_shape():
    meta = {m["key"]: m for m in fields_meta()}
    assert meta["hold_days"]["scopes"] == ["exit"] and meta["ret_n"]["params"] == [3, 5, 10, 20]
    assert {o["op"] for o in meta["pct"]["ops"]} == {">", ">=", "<", "<=", "between"}


def test_fields_meta_has_group_and_default_for_every_field():
    meta = fields_meta()
    assert meta  # 非空
    for m in meta:
        assert m["group"], f"{m['key']} 缺少 group"
        assert m["default"] is not None, f"{m['key']} 缺少 default"
        assert "op" in m["default"] and "value" in m["default"]


def test_fields_meta_new_metrics_present_with_expected_group():
    meta = {m["key"]: m for m in fields_meta()}
    assert meta["pe_dyn"]["group"] == "估值" and meta["pe_dyn"]["default"] == {"op": "between", "value": [0, 80]}
    assert meta["volume_ratio"]["default"] == {"op": ">=", "value": 2}
    assert meta["turnover"]["default"] == {"op": "between", "value": [3, 15]}
    assert meta["pct"]["default"] == {"op": "between", "value": [-3, 0]}
    assert meta["seal_amount"]["default"] == {"op": ">=", "value": 1}
    for key in ("amplitude", "committee", "outer_ratio", "pe_dyn", "pe_static", "pe_ttm", "pb",
                "avg_pct", "body_pct", "seal_amount", "seal_ratio", "ytd_pct", "up_streak",
                "float_shares", "total_shares"):
        assert key in meta, f"缺少新字段 {key}"


def test_validate_accepts_new_field_default_conditions():
    s = good()
    s["select"]["conditions"] = [C("pe_dyn", "between", [0, 80]), C("committee", ">=", 10)]
    s["entry"]["conditions"] = [C("seal_amount", ">=", 1)]
    errors, _ = validate(s)
    assert errors == []
