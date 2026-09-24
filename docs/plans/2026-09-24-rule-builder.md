# 规则搭建器（自定义策略）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 面板里用表单搭建自定义策略（选股/排序/买点/卖出/Jev 开关/资金），体检 + 试跑 + 次日生效 + 版本记录，每个自定义策略配主/影子账户，与内置策略并排运行对比。

**Architecture:** 纯函数规则引擎（字段注册表 + 条件求值 + 体检 + 排序）独立于运行时；运行时 `CustomStrategy` 用一个「取值函数」把实时行情、历史特征、题材、持仓、环境喂给引擎；策略定义以 JSON 版本化存库；面板通过 Api 读写与试跑；账户列表由「内置 4 个 + 今日生效的自定义策略」动态组成。

**Tech Stack:** 同前。

**Spec:** `docs/specs/2026-09-24-rule-builder-design.md`

**代码粒度：** 规则引擎（Task 3）给完整代码与测试；其余任务给接口、行为、测试要点——现有模块经多轮修复，执行者须先读现有代码再改。

## Global Constraints
- 两份旧设计文档仍有效；**低吸、扫首板行为不得改变**；现有测试不得削弱。
- 数据缺失 → 条件不满足；卖出不依赖 Jev；非当日行情不用于任何买卖；每策略每分钟最多检查 10 只、Jev 错误熔断 3 次。
- 自定义策略：id `c1`…，账户 `c{n}_main`/`c{n}_shadow`，全仓单票，最多同时启用 5 个；新版本次日生效；必须有止损才能保存。
- 前端所有外部字符串经 `esc()`；Api 方法不得跨桥抛异常（坏输入返回错误结构）。
- 测试在临时目录（conftest 已有），推送全局拦截（conftest 已有）；无网络调用；不触碰 `data/`、`reports/`、运行中的进程。
- 每次提交带 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。

---

### Task 1: 行情扩展字段
**Files:** `jevq/models.py`, `jevq/data/tencent.py`, `tests/factories.py`, `tests/test_tencent.py`
- `Quote` 新增 `turnover: float = 0.0`（字段 38，%）、`volume_ratio: float = 0.0`（字段 49）、`float_mcap: float = 0.0`（字段 44，亿）、`total_mcap: float = 0.0`（字段 45，亿），均有默认值以兼容现有构造。
- `parse_quotes` 解析这四个字段（解析失败置 0.0，不丢整条报价）；`factories.quote()` 增加同名可选参数。
- 测试：在现有 `tencent_line` 帮助函数中填充 38/44/45/49，断言解析值；字段为空字符串时为 0.0 且报价仍被解析。
- Commit `feat: quote turnover, volume ratio, market caps`

### Task 2: 历史特征
**Files:** Create `jevq/data/features.py`; Test `tests/test_features.py`
- `history_features(conn, codes, today: date) -> dict[str, dict]`：从 `closes`（`date < today`）一次性读取最近 21 个不同日期，按代码计算：
  - `ret_n`（n ∈ 3,5,10,20）：`round((c[-1]/c[-1-n]-1)*100, 2)`，数据不足则缺失；
  - `limit_up_in_n`（n ∈ 3,5,10,20）：最近 n 个相邻日对中是否有 `c_d >= round(c_{d-1}*1.1, 2)`（缺日不跨比）；数据不足 n 对时按已有判断；
  - `boards`：截至昨日的连续涨停天数（0 起）；
  - `ma_n`（n ∈ 5,10,20）：最近 n 个收盘均值（不足缺失）。
- 返回结构：`{code: {"ret": {3: x, 5: x, ...}, "limit_up": {3: bool, ...}, "boards": k, "ma": {5: x, ...}}}`
- 测试：构造 21 天数据验证各值；缺日不跨比；不足天数时缺失键。
- Commit `feat: history features (returns, limit-ups, boards, moving averages)`

### Task 3: 规则引擎（纯函数）
**Files:** Create `jevq/rules/custom.py`; Test `tests/test_custom_rules.py`

**完整代码：**
```python
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


FIELDS: dict[str, Field] = {f.key: f for f in [
    Field("pct", "当前涨幅", NUM, "%", lo=-20, hi=20),
    Field("open_pct", "开盘涨幅", NUM, "%", lo=-20, hi=20),
    Field("high_pct", "最高涨幅", NUM, "%", lo=-20, hi=20),
    Field("low_pct", "最低涨幅", NUM, "%", lo=-20, hi=20),
    Field("drawdown", "从最高回落", NUM, "点", lo=0, hi=40),
    Field("sealed", "封死涨停", BOOL),
    Field("broke", "今日炸过板", BOOL),
    Field("amount", "成交额", NUM, "亿", lo=0, hi=10000),
    Field("amount_rank", "成交额名次", NUM, "名", lo=1, hi=6000),
    Field("turnover", "换手率", NUM, "%", lo=0, hi=100),
    Field("volume_ratio", "量比", NUM, "", lo=0, hi=100),
    Field("float_mcap", "流通市值", NUM, "亿", lo=0, hi=100000),
    Field("total_mcap", "总市值", NUM, "亿", lo=0, hi=100000),
    Field("ret_n", "近N日涨幅", NUM, "%", lo=-90, hi=500, params=(3, 5, 10, 20)),
    Field("limit_up_in_n", "近N日涨停过", BOOL, params=(3, 5, 10, 20)),
    Field("boards", "连板数", NUM, "板", lo=0, hi=30),
    Field("ma_pos_n", "相对N日均线", NUM, "%", lo=-90, hi=500, params=(5, 10, 20)),
    Field("concept_heat", "题材热度", NUM, "只", lo=0, hi=500),
    Field("leader_rank", "题材内龙头名次", NUM, "名", lo=1, hi=500),
    Field("hold_days", "持有天数", NUM, "天", scopes=(EXIT,), lo=0, hi=60),
    Field("pnl_pct", "持仓盈亏", NUM, "%", scopes=(EXIT,), lo=-100, hi=500),
    Field("time", "时间", TIME),
    Field("sentiment", "市场情绪", ENUM, choices=("冰点", "偏弱", "正常", "火热")),
    Field("overseas", "外围", ENUM, choices=("偏弱", "中性", "偏强")),
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
    op, v = c.get("op"), c.get("value")
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
    return any(c.get("op") in ("<", "<=") and isinstance(c.get("value"), (int, float)) and c["value"] <= 0
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
             "ops": [{"op": o, "label": OP_LABELS[o]} for o in OPS[f.kind]]} for f in FIELDS.values()]
```
注：`rank_codes` 的 `get` 签名为 `get(code, key, n)`（带代码），而 `eval_cond/eval_group/first_exit` 的 `get` 为 `get(key, n)`（已绑定某只股票）。

**测试（完整）：**
```python
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
```
- Commit `feat: custom rule engine (fields, evaluation, validation, ranking)`

### Task 3b: 扩展盘口指标（用户截图要求，2026-09-24）
**Files:** `jevq/models.py`, `jevq/data/tencent.py`, `jevq/data/features.py`, `jevq/rules/custom.py`, tests
- 腾讯字段（2026-09-24 用中南文化与行情 App 截图逐项核对）：`39` 市盈(TTM)、`52` 市盈(动)、`53` 市盈(静)、`46` 市净率、`43` 振幅%、`7` 外盘（手）、`8` 内盘（手）、`51` 均价、`62` 今年来涨幅%、`72` 流通股本（股）、`73` 总股本（股）、五档：买量 `10/12/14/16/18`、卖量 `20/22/24/26/28`、买一价 `9`。
- `Quote` 新增（均带默认值）：`pe_ttm, pe_dyn, pe_static, pb, amplitude, outer_vol, inner_vol, avg_price, ytd_pct, float_shares, total_shares, bid_vols: tuple, ask_vols: tuple, bid1: float`；解析失败置默认值，不丢报价。
- 派生值（在取值函数里计算，缺数据返回 None）：`committee` 委比% = (Σ买量−Σ卖量)/(Σ买量+Σ卖量)×100；`outer_ratio` 外盘占比% = 外/(外+内)×100；`avg_pct` 均价% = (均价/昨收−1)×100；`body_pct` 实体% = (现价−开盘)/昨收×100；`seal_amount` 封单额（亿，仅封死涨停时 = 买一量×100×涨停价/1e8，否则 None）；`seal_ratio` 封成比 = 封单额/成交额；`up_streak` 连涨天数（features 里按收盘价计算，截至昨日，再看今日是否上涨）。
- 字段注册表新增（名称按行情 App 习惯）：振幅、委比、外盘占比、市盈(动)、市盈(静)、市盈(TTM)、市净率、均价%、实体%、封单额、封成比、今年来、连涨天、流通股本（亿股）、总股本（亿股）；每个字段有合理的 `lo/hi`；并为每个字段加 `default: dict`（该字段被选中时的默认条件，例如涨幅 `between [-3,0]`、量比 `>= 2`、换手 `between [3,15]`、市盈(动) `between [0,80]`、封单额 `>= 1`），`fields_meta()` 输出 `default` 与分组 `group`（价格/量能/估值/历史/题材/持仓/环境）。
- Tests：解析新字段；各派生值计算（含缺数据 → None、未封死封单额 None）；注册表与 `fields_meta` 含 default/group；`validate` 接受新字段的默认条件。
- Commit `feat: extended quote metrics for rule builder (PE/PB/amplitude/committee/seal...)`

### Task 4: 存储、版本与动态账户
**Files:** Create `jevq/data/custom_store.py`; Modify `jevq/db.py`, `jevq/config.py`, `jevq/ledger.py`, `jevq/engine.py`, `run.py`, `jevq/report.py`, `jevq/app/api.py`; Tests.
- 表 `custom_strategies(id, version, name, spec, created, active_from, enabled, PRIMARY KEY(id, version))`；迁移给 `fills`、`decisions` 加 `version INTEGER`。
- `custom_store`：`next_id(conn) -> "c{n}"`；`save(conn, spec, today, trade_days, sid=None) -> (sid, version)`（校验通过才保存；`active_from` = today 之后第一个交易日；新 id 时 version=1）；`disable(conn, sid, today)`（写一个 enabled=0 的新版本，次日生效）；`active_for(conn, day) -> list[(sid, version, spec)]`（每个 id 取 `active_from <= day` 的最新版本，且 enabled=1；最多返回 5 个，按 id 排序）；`holders(conn, day)`：已停用但仍有持仓的策略也需返回其最后启用版本以便卖出（`active_for` 返回结构加 `can_buy: bool`）；`history(conn, sid)`。
- 账户动态化：`config.builtin_accounts()` = 现有四个；新增 `jevq/accounts.py`：`all_accounts(conn, day) -> tuple`（内置 + 今日运行的自定义策略的 `c{n}_main/_shadow`），`account_names(conn, day) -> dict`（自定义用「{策略名}·主(Jev)」「{策略名}·影子」），`account_strategy`。`Ledger` 初始化仅内置四个（保持兼容），新增 `ensure_account(account, capital)`。`Engine._fetch` 的持仓代码改为使用构造时注入的 `accounts_provider()`（默认返回 `config.ACCOUNTS`，保证低吸测试不变）。报告、Api 使用动态账户与名称。
- Tests：保存/版本/次日生效/停用/最多 5 个/停用后仍有持仓可卖；动态账户列表；Engine 默认行为不变；报告与 status 含自定义账户。
- Commit `feat: versioned custom strategy store and dynamic accounts`

### Task 5: CustomStrategy 运行时
**Files:** Create `jevq/strategies/custom.py`; Test `tests/test_custom_strategy.py`
- `CustomStrategy(conn, ledger, sid, version, spec, notice_judge, news_judge, universe, features, concept_map, env_provider, prev_day, can_buy=True)`；`accounts = (f"{sid}_main", f"{sid}_shadow")`；构造时 `ledger.ensure_account(...)` 按 `spec.capital`。
- `on_minute(quotes, now)`：
  1. 卖出：对两个账户持仓，用绑定该股的取值函数 `first_exit(spec["exits"], get)`；命中 → `ledger.sell`（原因写 `规则名`）；持仓字段 `hold_days`（按交易日计，prev_day 相关；最简：`len` of trade days between buy_date and today，可用 closes 日期或注入 trade_days）与 `pnl_pct`（现价/成本价）；T+1（买入当天不卖）、非当日行情跳过、逐持仓异常隔离。
  2. 买入（`can_buy` 且有空位）：候选 = 股票池内当日行情中 `eval_group(select)` 为真者 → `rank_codes` → 逐只：`eval_group(entry, empty=True)` → 主账户：Jev（按 spec.jev 开关，复用 `NoticeJudge`/`NewsJudge` 与当日否决集，异常=error=当日否决，每分钟最多检查 10 只、错误 3 次熔断、可买性预检）→ 买入；影子：同候选同 entry、不走 Jev、买排名第一个满足 entry 的。
  3. 决策留痕：`decisions` 写 `strategy=sid, version`，trace 含 `{"strategy": sid, "version", "candidate": {name, price, pct, rank, of, hits: [命中的选股与买点条件的中文描述+当时取值]}, "notices", "news", "outcome"}`；卖出成交 reason 为规则名。
  4. 取值函数 `make_getter(code, q, pos=None)` 覆盖全部字段：`pct/open_pct/high_pct/low_pct/drawdown(=high_pct-pct)/sealed(price>=limit_up)/broke(high>=limit_up and price<limit_up)/amount(亿)/amount_rank/turnover/volume_ratio/float_mcap/total_mcap/ret_n/limit_up_in_n/boards/ma_pos_n(=(price/ma_n-1)*100)/concept_heat(该股所属题材中当日涨超 5% 成员数最大值)/leader_rank(该股在其最热题材内按 board.leader 同口径的名次)/hold_days/pnl_pct/time/sentiment/overseas`；`amount_rank` 与题材热度每分钟预计算一次。
- Tests：选股/排序/买点/卖出各至少一例；Jev 否决只影响主账户；缺数据字段不命中；T+1；非当日行情；异常隔离；trace 含 hits；`can_buy=False` 只卖。
- Commit `feat: CustomStrategy runtime`

### Task 6: 运行循环接入
**Files:** `run.py`, `tests/test_run.py`
- 盘前：`active_for(conn, today)` 加载；为每个构造 `CustomStrategy`（共享 judges、features=`history_features(conn, universe ∪ 持仓, today)`、concept_map、env_provider 返回 `{"sentiment": engine.sentiment_label, "overseas": overseas}`）；只卖日/数据不足时 `can_buy=False`。
- 每分钟：低吸 tick → 扫首板 → 各自定义策略 `on_minute(fresh_quotes, t)`，逐策略 try/except + rollback，异常不影响其他。
- 成交推送与收盘小结覆盖自定义账户（沿用 `_notify_fills`，主账户判断改为以 `_main` 结尾）。
- Tests：两个自定义策略一个抛异常另一个照常；停用但有持仓的策略只卖；推送覆盖自定义主账户。
- Commit `feat: run loop drives custom strategies`

### Task 7: Api
**Files:** `jevq/app/api.py`, `tests/test_api.py`
- `rule_fields()` → `fields_meta()`；`rules()` → 内置（只读摘要）+ 自定义（id、名称、当前版本、生效日、启用状态、是否有持仓）；`rule_get(sid, version=None)`；`rule_validate(spec)` → `{"errors", "warnings"}`；`rule_preview(spec)` → 用最近一次全市场报价（运行中取 `_state["prices"]` 不够，需完整行情：调用 `fetch_quotes(universe)`，60 秒内缓存）+ 历史特征 + 题材，返回 `{"total": n, "rows": [{"code","name","rank", "values": {字段中文名: 值}}...前 20]}`；不调用 Jev；`rule_save(spec, sid=None)` → 校验失败返回 errors，不保存；成功返回 `{sid, version, active_from}`；`rule_disable(sid)`；`rule_history(sid)`。所有方法对坏输入返回 `{"errors": [...]}` 不抛异常。写操作使用写连接（与只读查询分开），注意与交易进程并发（WAL，timeout）。
- Commit `feat: rule builder api`

### Task 8: 面板「规则」页与动态展示
**Files:** `jevq/app/ui.html`
- 新标签「规则」：左侧策略列表（内置只读、自定义可编辑/复制/停用/历史）；右侧编辑器顶部：名称、资金、Jev 两个开关、排序（字段 + N + 方向）。
- **盘口指标墙**（用户截图要求，仿行情 App「盘口数据」）：选股、买点、卖出三个区域各一面指标墙，指标按 `group` 分组、每组网格排列（每行 3 个，窄窗口 2 个）。每个方块：未选中灰色显示指标名；点击 → 选中高亮并弹出小框（运算符下拉 + 值控件 + N 选择，预填 `default`），确定后方块显示条件摘要（如「涨幅 -3%~0%」「量比 ≥ 2」）；再次点击 → 编辑；弹框内「取消选择」清除。每个区域顶部切换「全部满足 / 任一满足」。只显示该区域可用的字段（持仓类只在卖出区出现）。
- 卖出区：可添加多条卖出规则，每条是一面折叠的小指标墙 + 规则名，可上移下移调整优先级。
- 右侧：实时体检（`rule_validate`，防抖 500ms，错误红、警告黄）；「试跑」预览表（命中总数 + 前 20 只及所选指标当前值）；每次选中/修改方块后自动刷新命中数。「保存（明日生效）」在有错误时禁用。
- 账户卡片、净值曲线、决策筛选、成交表按动态策略生成（颜色按序分配，图例）；决策明细对自定义策略显示「命中条件」列表（hits）。
- 所有外部字符串经 `esc()`；表单值只经 JSON 发送，不拼 HTML。
- 控制者做可视化验收。
- Commit `feat: rule builder panel`

### Task 9: 最终审查与数据准备（controller）
- 全分支最终审查（最强模型）+ 一次修复波次。
- 用新浪补足本地收盘价到 ≥ 21 个交易日（`start_date=20260801`）。
- 收盘后重启后台，推送 GitHub。
