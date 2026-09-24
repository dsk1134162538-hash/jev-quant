import re
from dataclasses import dataclass, field
from datetime import time

NUM, BOOL, ENUM, TIME = "num", "bool", "enum", "time"
SELECT, ENTRY, EXIT = "select", "entry", "exit"
ALL_SCOPES = (SELECT, ENTRY, EXIT)
MAX_NAME = 20


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str
    unit: str = ""
    scopes: tuple = ALL_SCOPES
    lo: float | None = None
    hi: float | None = None
    params: tuple = ()
    choices: tuple = ()
    group: str = ""
    default: dict | None = None


FIELDS: dict[str, Field] = {f.key: f for f in [
    Field("pct", "当前涨幅", NUM, "%", lo=-20, hi=20, group="价格",
          default={"op": "between", "value": [-3, 0]}),
    Field("open_pct", "开盘涨幅", NUM, "%", lo=-20, hi=20, group="价格",
          default={"op": "between", "value": [-3, 3]}),
    Field("high_pct", "最高涨幅", NUM, "%", lo=-20, hi=20, group="价格",
          default={"op": ">=", "value": 3}),
    Field("low_pct", "最低涨幅", NUM, "%", lo=-20, hi=20, group="价格",
          default={"op": ">=", "value": -5}),
    Field("drawdown", "从最高回落", NUM, "点", lo=0, hi=40, group="价格",
          default={"op": "<=", "value": 5}),
    Field("sealed", "封死涨停", BOOL, group="价格", default={"op": "is", "value": True}),
    Field("broke", "今日炸过板", BOOL, group="价格", default={"op": "is", "value": False}),
    Field("amount", "成交额", NUM, "亿", lo=0, hi=10000, group="量能",
          default={"op": ">=", "value": 1}),
    Field("amount_rank", "成交额名次", NUM, "名", lo=1, hi=6000, group="量能",
          default={"op": "<=", "value": 1000}),
    Field("turnover", "换手率", NUM, "%", lo=0, hi=100, group="量能",
          default={"op": "between", "value": [3, 15]}),
    Field("volume_ratio", "量比", NUM, "", lo=0, hi=100, group="量能",
          default={"op": ">=", "value": 2}),
    Field("float_mcap", "流通市值", NUM, "亿", lo=0, hi=100000, group="估值",
          default={"op": "between", "value": [20, 200]}),
    Field("total_mcap", "总市值", NUM, "亿", lo=0, hi=100000, group="估值",
          default={"op": "between", "value": [20, 300]}),
    Field("ret_n", "近N日涨幅", NUM, "%", lo=-90, hi=500, params=(3, 5, 10, 20), group="历史",
          default={"op": ">", "value": 10, "param": {"n": 5}}),
    Field("limit_up_in_n", "近N日涨停过", BOOL, params=(3, 5, 10, 20), group="历史",
          default={"op": "is", "value": True, "param": {"n": 5}}),
    Field("boards", "连板数", NUM, "板", lo=0, hi=30, group="历史",
          default={"op": ">=", "value": 1}),
    Field("ma_pos_n", "相对N日均线", NUM, "%", lo=-90, hi=500, params=(5, 10, 20), group="历史",
          default={"op": ">", "value": 0, "param": {"n": 5}}),
    Field("concept_heat", "题材热度", NUM, "只", lo=0, hi=500, group="题材",
          default={"op": ">=", "value": 10}),
    Field("leader_rank", "题材内龙头名次", NUM, "名", lo=1, hi=500, group="题材",
          default={"op": "<=", "value": 3}),
    Field("hold_days", "持有天数", NUM, "天", scopes=(EXIT,), lo=0, hi=60, group="持仓",
          default={"op": "<=", "value": 5}),
    Field("pnl_pct", "持仓盈亏", NUM, "%", scopes=(EXIT,), lo=-100, hi=500, group="持仓",
          default={"op": "<=", "value": -3}),
    Field("time", "时间", TIME, group="环境",
          default={"op": "between", "value": ["14:30", "14:55"]}),
    Field("sentiment", "市场情绪", ENUM, choices=("冰点", "偏弱", "正常", "火热"), group="环境",
          default={"op": "in", "value": ["正常", "火热"]}),
    Field("overseas", "外围", ENUM, choices=("偏弱", "中性", "偏强"), group="环境",
          default={"op": "in", "value": ["中性", "偏强"]}),
    Field("amplitude", "振幅", NUM, "%", lo=0, hi=50, group="价格",
          default={"op": ">=", "value": 5}),
    Field("committee", "委比", NUM, "%", lo=-100, hi=100, group="量能",
          default={"op": ">=", "value": 10}),
    Field("outer_ratio", "外盘占比", NUM, "%", lo=0, hi=100, group="量能",
          default={"op": ">=", "value": 55}),
    Field("pe_dyn", "市盈(动)", NUM, "倍", lo=-1000, hi=1000, group="估值",
          default={"op": "between", "value": [0, 80]}),
    Field("pe_static", "市盈(静)", NUM, "倍", lo=-1000, hi=1000, group="估值",
          default={"op": "between", "value": [0, 80]}),
    Field("pe_ttm", "市盈(TTM)", NUM, "倍", lo=-1000, hi=1000, group="估值",
          default={"op": "between", "value": [0, 80]}),
    Field("pb", "市净率", NUM, "倍", lo=0, hi=100, group="估值",
          default={"op": "between", "value": [0, 10]}),
    Field("avg_pct", "均价%", NUM, "%", lo=-20, hi=20, group="价格",
          default={"op": ">=", "value": 0}),
    Field("body_pct", "实体%", NUM, "%", lo=-20, hi=20, group="价格",
          default={"op": ">=", "value": 3}),
    Field("seal_amount", "封单额", NUM, "亿", lo=0, hi=1000, group="量能",
          default={"op": ">=", "value": 1}),
    Field("seal_ratio", "封成比", NUM, "", lo=0, hi=50, group="量能",
          default={"op": ">=", "value": 0.5}),
    Field("ytd_pct", "今年来", NUM, "%", lo=-100, hi=1000, group="价格",
          default={"op": ">=", "value": 0}),
    Field("up_streak", "连涨天", NUM, "天", lo=0, hi=30, group="历史",
          default={"op": ">=", "value": 2}),
    Field("float_shares", "流通股本", NUM, "亿股", lo=0, hi=1000, group="估值",
          default={"op": "between", "value": [1, 20]}),
    Field("total_shares", "总股本", NUM, "亿股", lo=0, hi=1000, group="估值",
          default={"op": "between", "value": [1, 30]}),
]}

OPS = {NUM: (">", ">=", "<", "<=", "between"), BOOL: ("is",), ENUM: ("in",), TIME: (">=", "<", "between")}
OP_LABELS = {">": "大于", ">=": "不低于", "<": "小于", "<=": "不高于", "between": "介于", "is": "是", "in": "属于"}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _param(c: dict):
    return (c.get("param") or {}).get("n")


def eval_cond(c: dict, get) -> bool:
    """get(field_key, n) -> 值或 None。缺数据或类型不符 → False。"""
    f = FIELDS.get(c.get("field"))
    if f is None:
        return False
    v = get(f.key, _param(c))
    if v is None:
        return False
    op, val = c.get("op"), c.get("value")
    try:
        if f.kind == TIME:
            val = [_t(x) for x in val] if op == "between" else _t(val)
        if op == ">":
            return v > val
        if op == ">=":
            return v >= val
        if op == "<":
            return v < val
        if op == "<=":
            return v <= val
        if op == "between":
            lo, hi = val
            return lo <= v <= hi
        if op == "is":
            return bool(v) == bool(val)
        if op == "in":
            return v in val
    except (TypeError, ValueError):
        return False
    return False


def eval_group(g: dict | None, get, empty: bool = True) -> bool:
    conds = (g or {}).get("conditions") or []
    if not conds:
        return empty
    results = (eval_cond(c, get) for c in conds)
    return any(results) if (g or {}).get("mode") == "any" else all(results)


def first_exit(exits: list, get) -> str | None:
    for e in exits or []:
        if eval_group(e.get("when"), get, empty=False):
            return e.get("reason") or "规则卖出"
    return None


def rank_codes(codes, get, rank: dict | None) -> list[str]:
    if not rank or rank.get("field") not in FIELDS:
        return list(codes)
    key, n = rank["field"], (rank.get("param") or {}).get("n")
    scored = [(c, get(c, key, n)) for c in codes]
    scored = [(c, v) for c, v in scored if v is not None]
    scored.sort(key=lambda cv: cv[1], reverse=rank.get("order", "desc") != "asc")
    return [c for c, _ in scored]


def _interval(c: dict):
    op, v = c.get("op"), c.get("val")
    if op in (">", ">="):
        return (v, float("inf"), op == ">", False)
    if op in ("<", "<="):
        return (float("-inf"), v, False, op == "<")
    if op == "between":
        return (v[0], v[1], False, False)
    return None


def _check_cond(c: dict, scope: str, where: str, errors: list):
    f = FIELDS.get(c.get("field"))
    if f is None:
        errors.append(f"{where}：未知字段 {c.get('field')}")
        return
    if scope not in f.scopes:
        errors.append(f"{where}：「{f.label}」不能用在这里")
    op, val = c.get("op"), c.get("value")
    if op not in OPS[f.kind]:
        errors.append(f"{where}：「{f.label}」不支持运算「{op}」")
        return
    if f.params and _param(c) not in f.params:
        errors.append(f"{where}：「{f.label}」的 N 只能是 {'/'.join(map(str, f.params))}")
    if f.kind == NUM:
        vals = val if op == "between" else [val]
        if op == "between" and (not isinstance(val, list) or len(val) != 2):
            errors.append(f"{where}：「{f.label}」介于需要两个数")
            return
        for x in vals:
            if not isinstance(x, (int, float)) or isinstance(x, bool):
                errors.append(f"{where}：「{f.label}」需要数字")
                return
            if (f.lo is not None and x < f.lo) or (f.hi is not None and x > f.hi):
                errors.append(f"{where}：「{f.label}」取值超出范围 {f.lo}~{f.hi}")
        if op == "between" and val[0] > val[1]:
            errors.append(f"{where}：「{f.label}」介于的下限大于上限")
    elif f.kind == BOOL:
        if not isinstance(val, bool):
            errors.append(f"{where}：「{f.label}」需要 是/否")
    elif f.kind == ENUM:
        if not isinstance(val, list) or not val or any(x not in f.choices for x in val):
            errors.append(f"{where}：「{f.label}」只能选 {'/'.join(f.choices)}")
    elif f.kind == TIME:
        vals = val if op == "between" else [val]
        if op == "between" and (not isinstance(val, list) or len(val) != 2):
            errors.append(f"{where}：时间介于需要两个时刻")
            return
        if any(not isinstance(x, str) or not _TIME_RE.match(x) for x in vals):
            errors.append(f"{where}：时间格式应为 HH:MM")
        elif op == "between" and _t(val[0]) > _t(val[1]):
            errors.append(f"{where}：时间介于的开始晚于结束")


def _check_group(g: dict | None, scope: str, where: str, errors: list):
    g = g or {}
    if g.get("mode", "all") not in ("all", "any"):
        errors.append(f"{where}：组合方式只能是 全部/任一")
    for i, c in enumerate(g.get("conditions") or [], 1):
        _check_cond(c, scope, f"{where}第{i}条", errors)
    if g.get("mode", "all") == "all":
        bounds: dict = {}
        for c in g.get("conditions") or []:
            f = FIELDS.get(c.get("field"))
            if f is None or f.kind != NUM:
                continue
            iv = _interval(c)
            if iv is None or not all(isinstance(x, (int, float)) for x in iv[:2]):
                continue
            k = (f.key, _param(c))
            lo, hi, lo_open, hi_open = bounds.get(k, (float("-inf"), float("inf"), False, False))
            if iv[0] > lo or (iv[0] == lo and iv[2]):
                lo, lo_open = iv[0], iv[2]
            if iv[1] < hi or (iv[1] == hi and iv[3]):
                hi, hi_open = iv[1], iv[3]
            bounds[k] = (lo, hi, lo_open, hi_open)
            if lo > hi or (lo == hi and (lo_open or hi_open)):
                errors.append(f"{where}：「{f.label}」的条件互相矛盾，不可能同时满足")


def _is_stop(e: dict) -> bool:
    conds = (e.get("when") or {}).get("conditions") or []
    if not conds or any(c.get("field") not in ("pct", "pnl_pct") for c in conds):
        return False
    return any(c.get("op") in ("<", "<=") and isinstance(c.get("val"), (int, float)) and c["val"] <= 0
               for c in conds)


def validate(spec: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(spec, dict):
        return ["策略格式错误"], []
    name = str(spec.get("name") or "").strip()
    if not name or len(name) > MAX_NAME:
        errors.append(f"策略名称不能为空且不超过 {MAX_NAME} 个字")
    cap = spec.get("capital", 30000)
    if not isinstance(cap, (int, float)) or not 1000 <= cap <= 10_000_000:
        errors.append("资金需在 1000 ~ 10,000,000 之间")
    if not (spec.get("select") or {}).get("conditions"):
        errors.append("选股条件不能为空")
    _check_group(spec.get("select"), SELECT, "选股", errors)
    _check_group(spec.get("entry"), ENTRY, "买点", errors)
    rank = spec.get("rank")
    if rank:
        f = FIELDS.get(rank.get("field"))
        if f is None or f.kind != NUM or SELECT not in f.scopes:
            errors.append("排序字段必须是可用于选股的数值字段")
        elif f.params and (rank.get("param") or {}).get("n") not in f.params:
            errors.append(f"排序字段「{f.label}」的 N 只能是 {'/'.join(map(str, f.params))}")
        if rank.get("order", "desc") not in ("asc", "desc"):
            errors.append("排序方向只能是 从高到低/从低到高")
    exits = spec.get("exits") or []
    if not exits:
        errors.append("卖出规则不能为空")
    for i, e in enumerate(exits, 1):
        _check_group(e.get("when"), EXIT, f"卖出规则{i}", errors)
        if not (e.get("when") or {}).get("conditions"):
            errors.append(f"卖出规则{i}：条件不能为空")
    if exits and not any(_is_stop(e) for e in exits):
        errors.append("必须有止损：至少一条卖出规则只看「当前涨幅」或「持仓盈亏」，且在跌到某个不高于 0 的值时卖出")
    if exits and not any(c.get("field") in ("time", "hold_days")
                         for e in exits for c in (e.get("when") or {}).get("conditions") or []):
        warnings.append("没有按时间或持有天数退出的规则，持仓可能被长期持有")
    jev = spec.get("jev") or {}
    if not isinstance(jev.get("notices", True), bool) or not isinstance(jev.get("news", True), bool):
        errors.append("Jev 开关格式错误")
    return errors, warnings


def fields_meta() -> list[dict]:
    """供面板渲染下拉框。"""
    return [{"key": f.key, "label": f.label, "kind": f.kind, "unit": f.unit, "scopes": list(f.scopes),
             "lo": f.lo, "hi": f.hi, "params": list(f.params), "choices": list(f.choices),
             "group": f.group, "default": f.default,
             "ops": [{"op": o, "label": OP_LABELS[o]} for o in OPS[f.kind]]} for f in FIELDS.values()]
