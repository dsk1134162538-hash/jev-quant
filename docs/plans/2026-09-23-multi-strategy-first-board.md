# 多策略 + 扫首板 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变低吸策略行为的前提下，新增「只做龙头、扫首板」策略（独立 3 万主账户 + 3 万影子账户），与低吸并排对比；并记录首板封板时段统计用于验证经验。

**Architecture:** 低吸继续由现有 `Engine` 执行，但只处理自己的账户（`main`/`shadow`）。扫首板实现为独立的 `BoardStrategy`，与 `Engine` 共享账本、Jev 判断器（含当日缓存）和每分钟全市场行情；它额外维护一个 ≤5 只的观察池，在扫板窗口内每 3 秒只请求观察池报价并按「涨幅 ≥9%、未封死」触发买入。题材数据来自新浪概念板块，每周刷新存本地。

**Tech Stack:** 同前（Python 3.12、requests、akshare、sqlite3、pytest、pywebview）

**Spec:** `docs/specs/2026-09-23-multi-strategy-first-board-design.md`

**关于代码粒度：** 纯函数任务（Task 2–4）给出完整代码与测试；集成任务（Task 1、5–8）给出精确接口、行为与测试用例，实现由执行者在读取现有代码后编写——现有代码经过多轮修复，计划里硬抄旧片段反而容易覆盖掉修复。

## Global Constraints

- 设计文档（spec）为准；前一份低吸设计文档中的全部规则对低吸策略继续有效。
- **低吸行为不得改变**：除因账户数变化必须调整的期望值外，现有测试不得削弱。
- 账户：`main`/`shadow`（低吸）、`board_main`/`board_shadow`（扫首板），每个初始 30000，全仓单票（最多 1 只，买入用全部可用现金）。
- 扫首板参数：观察池进入涨幅 ≥ 7%、移出 < 6%、上限 5 只；热点题材 = 同题材股票池内当日行情涨幅 ≥ 5% 的成员 ≥ 3；龙头分 = 题材内「当前涨幅、成交额、近 5 日涨幅」三项降序名次之和（小者优）；扫板触发涨幅 ≥ 9.0% 且价格 < 涨停价；扫板窗口默认 09:30–10:30；快速循环间隔 3 秒。
- 首板：昨日收盘 < round(前日收盘 × 1.1, 2)（主板 10% 涨停）。涨停价 = round(昨收 × 1.1, 2)。
- 扫首板卖出优先级：止损（≤ 昨收 × 0.97）→ 封死涨停持有 → 炸板卖 → 冲高回落 2 个点卖 → 14:55 未封死清仓 → 持有；14:55 封死可隔夜；隔夜超期（错过交易日）开盘即卖，除非封死涨停。
- 卖出永远不依赖 Jev；行情非当日（`ts.date() < today`）不用于任何买卖。
- 不绕过任何网站的反爬/WAF；外部请求串行且有间隔与连续失败熔断。
- 测试一律在临时目录运行（`tests/conftest.py` 已有 autouse `chdir(tmp_path)`），不得触碰真实 `data/`、`reports/`。
- 命令在 `D:\claude\jev-quant` 下执行，Python 用 `.venv/Scripts/python`；每次提交带 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。

---

### Task 1: 账户与策略归属

**Files:**
- Modify: `jevq/config.py`, `jevq/db.py`, `jevq/engine.py`, `jevq/report.py`, `jevq/app/api.py`, `run.py`
- Test: `tests/test_engine.py`, `tests/test_ledger.py`, `tests/test_report.py`, `tests/test_api.py`, `tests/test_db.py`

**Interfaces（Produces）:**
- `config.STRATEGIES: dict[str, dict]` = `{"lowbuy": {"name": "低吸", "accounts": ("main", "shadow")}, "board": {"name": "扫首板", "accounts": ("board_main", "board_shadow")}}`
- `config.ACCOUNTS` = 四个账户（按上面顺序）；`config.ACCOUNT_NAMES` = `{"main": "低吸·主(Jev)", "shadow": "低吸·影子", "board_main": "首板·主(Jev)", "board_shadow": "首板·影子"}`；`config.ACCOUNT_STRATEGY: dict[str, str]`（账户 → 策略 id）
- `db`：迁移给 `decisions` 增加列 `strategy TEXT`（旧行为 NULL，读取时视为 `lowbuy`）
- `Engine(..., accounts: tuple[str, str] = STRATEGIES["lowbuy"]["accounts"])`：`_sells`、`_buy_shadow`、`_buy_main` 只使用 `self.accounts`；`_fetch` 仍对**全部**账户的持仓代码取行情（保证扫首板持仓每分钟有报价）；`_log` 写入 `strategy='lowbuy'`，并在候选 trace 中加 `"strategy": "lowbuy"`
- `report.write_report` 与 `Api` 改用 `ACCOUNT_NAMES`；报告净值表按策略分组（每策略一个小标题 + 两行）
- `Api.status()["accounts"]` 每项加 `"strategy"`、`"strategy_name"`；`Api.decisions(day, strategy: str | None = None)` 可按策略过滤（NULL 视为 lowbuy）；`equity_curve()` 返回四个账户
- `run.py`：所有 `ACCOUNTS` 用法保持覆盖四个账户（持仓代码、日志）

**Tests（新增/调整）:**
- `test_engine.py`：`board_main` 持有一只满足低吸卖出条件的票 → `Engine.tick` 不卖它；但 `Engine.last_quotes` 含该代码。低吸候选的 `decisions.strategy == "lowbuy"`，trace 含 `"strategy": "lowbuy"`。
- `test_ledger.py`：新库初始化后四个账户现金均为 30000。
- `test_report.py`：equity 行数由 2 改为 4（说明原因）；报告含「低吸」「扫首板」两个分组标题。
- `test_api.py`：`status()` 四个账户且带 `strategy`；`decisions(day, "board")` 只返回 board 行；旧行（strategy NULL）归入 lowbuy。
- `test_db.py`：旧库迁移后 `decisions` 有 `strategy` 列。

- [ ] Step 1: 写上述失败测试 → Step 2: 运行确认失败 → Step 3: 实现 → Step 4: 全量测试通过 → Step 5: Commit `feat: four accounts across two strategies; engine limited to low-buy accounts`

---

### Task 2: 题材映射（新浪概念板块）

**Files:**
- Create: `jevq/data/concepts.py`
- Modify: `jevq/db.py`（`SCHEMA` 增加 `concepts` 表）
- Test: `tests/test_concepts.py`

**Interfaces:**
- 表：`CREATE TABLE IF NOT EXISTS concepts(code TEXT, concept TEXT, updated TEXT, PRIMARY KEY(code, concept));`
- `fetch_concept_list() -> list[tuple[str, str]]`：`ak.stock_sector_spot(indicator="概念")` → `[(label, 板块名)]`，经 `call_with_timeout`
- `fetch_concept_members(label: str) -> list[str]`：`ak.stock_sector_detail(sector=label)` 的 `code` 列（6 位字符串），经 `call_with_timeout`
- `refresh_concepts(conn, today: date, fetch_list=fetch_concept_list, fetch_members=fetch_concept_members, pause: float = 0.5, max_consecutive_failures: int = 10, log=print) -> int`：列表拿到后逐个串行取成分，成功的板块先收集；**全部结束且至少一半板块成功**时才用事务整体替换 `concepts` 表（`DELETE` + 批量 `INSERT`，`updated=today`），否则保留旧表并 log；返回写入的 (code, concept) 行数（未替换返回 0）
- `needs_refresh(conn, today: date, max_age_days: int = 7) -> bool`：表为空或最新 `updated` 距今 ≥ 7 天
- `load_concepts(conn) -> dict[str, list[str]]`：code → 所属题材名列表

**完整代码：**
```python
import time as _time
from datetime import date

import akshare as ak

from jevq.data.timeout import call_with_timeout


def fetch_concept_list() -> list[tuple[str, str]]:
    df = call_with_timeout(ak.stock_sector_spot, indicator="概念")
    return [(str(r["label"]), str(r["板块"])) for _, r in df.iterrows()]


def fetch_concept_members(label: str) -> list[str]:
    df = call_with_timeout(ak.stock_sector_detail, sector=label)
    return [str(c).zfill(6) for c in df["code"]]


def needs_refresh(conn, today: date, max_age_days: int = 7) -> bool:
    row = conn.execute("SELECT MAX(updated) FROM concepts").fetchone()
    if not row or not row[0]:
        return True
    return (today - date.fromisoformat(row[0])).days >= max_age_days


def refresh_concepts(conn, today: date, fetch_list=fetch_concept_list, fetch_members=fetch_concept_members,
                     pause: float = 0.5, max_consecutive_failures: int = 10, log=print) -> int:
    try:
        boards = fetch_list()
    except Exception as e:
        log(f"题材列表获取失败：{e!r}")
        return 0
    rows, ok, fails = [], 0, 0
    for label, name in boards:
        try:
            members = fetch_members(label)
            rows += [(code, name) for code in members]
            ok += 1
            fails = 0
        except Exception as e:
            fails += 1
            log(f"题材 {name} 成分获取失败：{e!r}")
            if fails >= max_consecutive_failures:
                log(f"题材成分连续失败 {fails} 次，停止刷新")
                break
        _time.sleep(pause)
    if not boards or ok * 2 < len(boards):
        log(f"题材刷新成功 {ok}/{len(boards)}，不足一半，保留旧数据")
        return 0
    with conn:
        conn.execute("DELETE FROM concepts")
        conn.executemany("INSERT OR IGNORE INTO concepts(code, concept, updated) VALUES (?, ?, ?)",
                         [(c, n, today.isoformat()) for c, n in rows])
    log(f"题材刷新完成：{ok} 个题材，{len(rows)} 条成分")
    return len(rows)


def load_concepts(conn) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for code, concept in conn.execute("SELECT code, concept FROM concepts ORDER BY concept"):
        out.setdefault(code, []).append(concept)
    return out
```

**测试（完整）：**
```python
from datetime import date

from jevq import db
from jevq.data.concepts import load_concepts, needs_refresh, refresh_concepts

TODAY = date(2026, 9, 28)


def test_refresh_replaces_table_and_loads():
    conn = db.connect(":memory:")
    n = refresh_concepts(conn, TODAY, fetch_list=lambda: [("gn_a", "机器人"), ("gn_b", "华为汽车")],
                         fetch_members=lambda label: {"gn_a": ["600001", "600002"], "gn_b": ["600001"]}[label],
                         pause=0)
    assert n == 3
    assert load_concepts(conn) == {"600001": ["华为汽车", "机器人"], "600002": ["机器人"]}
    assert not needs_refresh(conn, TODAY)
    assert needs_refresh(conn, date(2026, 10, 5))


def test_refresh_keeps_old_data_when_most_boards_fail():
    conn = db.connect(":memory:")
    refresh_concepts(conn, TODAY, fetch_list=lambda: [("gn_a", "机器人")], fetch_members=lambda l: ["600001"], pause=0)

    def boom(label):
        raise RuntimeError("blocked")
    n = refresh_concepts(conn, TODAY, fetch_list=lambda: [("gn_a", "机器人"), ("gn_b", "芯片")], fetch_members=boom,
                         pause=0, max_consecutive_failures=10)
    assert n == 0
    assert load_concepts(conn) == {"600001": ["机器人"]}


def test_refresh_stops_after_consecutive_failures():
    conn = db.connect(":memory:")
    calls = []

    def boom(label):
        calls.append(label)
        raise RuntimeError("x")
    refresh_concepts(conn, TODAY, fetch_list=lambda: [(f"gn_{i}", f"c{i}") for i in range(20)], fetch_members=boom,
                     pause=0, max_consecutive_failures=3)
    assert len(calls) == 3


def test_list_failure_returns_zero():
    conn = db.connect(":memory:")

    def boom():
        raise RuntimeError("x")
    assert refresh_concepts(conn, TODAY, fetch_list=boom, pause=0) == 0
    assert needs_refresh(conn, TODAY)
```

- [ ] Step 1–5 同 TDD 流程；Commit `feat: sina concept map with weekly refresh`

---

### Task 3: 首板选股纯函数

**Files:**
- Create: `jevq/rules/board.py`
- Test: `tests/test_board_rules.py`

**完整代码：**
```python
from dataclasses import dataclass, field
from datetime import datetime, time

from jevq.models import Quote

ENTER_PCT = 7.0
EXIT_PCT = 6.0
HOT_PCT = 5.0
HOT_MIN = 3
WATCH_MAX = 5
TRIGGER_PCT = 9.0
SCAN_WINDOWS: list[tuple[time, time]] = [(time(9, 30), time(10, 30))]


def limit_price(pre_close: float) -> float:
    return round(pre_close * 1.1, 2)


def is_first_board(prev_close: float | None, prev_prev_close: float | None) -> bool:
    """昨日未涨停才算首板；缺数据时不算（保守）。"""
    if not prev_close or not prev_prev_close:
        return False
    return prev_close < limit_price(prev_prev_close)


def hot_concepts(quotes: dict[str, Quote], concept_map: dict[str, list[str]], today) -> dict[str, list[str]]:
    """题材 → 该题材中当日涨幅 ≥ HOT_PCT 的成员（仅当成员数 ≥ HOT_MIN）。quotes 应只含股票池内代码。"""
    strong: dict[str, list[str]] = {}
    for code, q in quotes.items():
        if q.ts.date() < today or q.is_st or q.suspended or q.pct < HOT_PCT:
            continue
        for c in concept_map.get(code, []):
            strong.setdefault(c, []).append(code)
    return {c: codes for c, codes in strong.items() if len(codes) >= HOT_MIN}


@dataclass(frozen=True)
class Candidate:
    code: str
    concept: str
    heat: int
    score: int
    ranks: dict = field(default_factory=dict)     # {"pct": n, "amount": n, "ret5": n}


def _rank(codes: list[str], key) -> dict[str, int]:
    ordered = sorted(codes, key=key, reverse=True)
    return {c: i + 1 for i, c in enumerate(ordered)}


def leader(concept: str, members: list[str], quotes: dict[str, Quote], ret5: dict[str, float]) -> Candidate | None:
    """在热点题材的强势成员中按三项名次之和选龙头；缺 ret5 的成员不参与。"""
    pool = [c for c in members if c in ret5]
    if not pool:
        return None
    rp = _rank(pool, lambda c: quotes[c].pct)
    ra = _rank(pool, lambda c: quotes[c].amount)
    rr = _rank(pool, lambda c: ret5[c])
    best = min(pool, key=lambda c: (rp[c] + ra[c] + rr[c], rp[c]))
    return Candidate(best, concept, len(members), rp[best] + ra[best] + rr[best],
                     {"pct": rp[best], "amount": ra[best], "ret5": rr[best]})


def watch_candidates(quotes: dict[str, Quote], concept_map: dict[str, list[str]], ret5: dict[str, float],
                     prev_closes: dict[str, tuple[float | None, float | None]], today) -> list[Candidate]:
    """每个热点题材取龙头；满足：涨幅 ≥ ENTER_PCT、未封死、首板。按热度降序、龙头分升序，去重后最多 WATCH_MAX。
    prev_closes: code → (昨收, 前日收)。"""
    out: dict[str, Candidate] = {}
    hot = hot_concepts(quotes, concept_map, today)
    for concept, members in sorted(hot.items(), key=lambda kv: -len(kv[1])):
        cand = leader(concept, members, quotes, ret5)
        if cand is None or cand.code in out:
            continue
        q = quotes[cand.code]
        pc, ppc = prev_closes.get(cand.code, (None, None))
        if q.pct < ENTER_PCT or q.price >= q.limit_up or not is_first_board(pc, ppc):
            continue
        out[cand.code] = cand
    return sorted(out.values(), key=lambda c: (-c.heat, c.score))[:WATCH_MAX]


def in_scan_window(t: time, windows: list[tuple[time, time]] = SCAN_WINDOWS) -> bool:
    return any(a <= t < b for a, b in windows)


def scan_trigger(q: Quote, now: datetime, windows: list[tuple[time, time]] = SCAN_WINDOWS) -> bool:
    """扫板买点：窗口内、行情当日、涨幅 ≥ TRIGGER_PCT、价格未到涨停价（已封死视为排队，不成交）。"""
    return (in_scan_window(now.time(), windows) and q.ts.date() >= now.date() and not q.suspended
            and q.pct >= TRIGGER_PCT and q.price < q.limit_up)
```

**测试（完整）：**
```python
from datetime import date, datetime, time

from factories import quote
from jevq.rules.board import (Candidate, hot_concepts, in_scan_window, is_first_board, leader, limit_price,
                              scan_trigger, watch_candidates)

TODAY = date(2026, 9, 24)
TS = datetime(2026, 9, 24, 10, 0)


def q(code, pct, amount=1e8, **kw):
    return quote(code, pct=pct, amount=amount, ts=TS, **kw)


def test_limit_and_first_board():
    assert limit_price(10.0) == 11.0
    assert is_first_board(10.5, 10.0)          # 昨日 +5%
    assert not is_first_board(11.0, 10.0)      # 昨日涨停
    assert not is_first_board(None, 10.0)


def test_hot_concepts_needs_three_strong_members():
    quotes = {"600001": q("600001", 6), "600002": q("600002", 5.5), "600003": q("600003", 5.0),
              "600004": q("600004", 4.9), "000001": q("000001", 8)}
    cmap = {"600001": ["机器人"], "600002": ["机器人"], "600003": ["机器人"], "600004": ["机器人"],
            "000001": ["芯片"]}
    assert hot_concepts(quotes, cmap, TODAY) == {"机器人": ["600001", "600002", "600003"]}


def test_hot_concepts_ignores_stale_and_st():
    stale = quote("600001", pct=8, ts=datetime(2026, 9, 23, 15, 0))
    quotes = {"600001": stale, "600002": q("600002", 6, name="ST甲"), "600003": q("600003", 6),
              "600004": q("600004", 6)}
    cmap = {c: ["机器人"] for c in quotes}
    assert hot_concepts(quotes, cmap, TODAY) == {}


def test_leader_sums_three_ranks():
    quotes = {"600001": q("600001", 9, amount=3e8), "600002": q("600002", 8, amount=5e8),
              "600003": q("600003", 6, amount=1e8)}
    ret5 = {"600001": 10.0, "600002": 20.0, "600003": 30.0}
    c = leader("机器人", list(quotes), quotes, ret5)
    # 600001: pct1+amt2+ret3=6；600002: 2+1+2=5；600003: 3+3+1=7
    assert c.code == "600002" and c.score == 5 and c.ranks == {"pct": 2, "amount": 1, "ret5": 2}


def test_watch_candidates_filters():
    quotes = {"600001": q("600001", 8.5, amount=9e8), "600002": q("600002", 6), "600003": q("600003", 5.5)}
    cmap = {c: ["机器人"] for c in quotes}
    ret5 = {c: 1.0 for c in quotes}
    prev = {"600001": (10.5, 10.0)}
    got = watch_candidates(quotes, cmap, ret5, prev, TODAY)
    assert [c.code for c in got] == ["600001"] and got[0].heat == 3
    prev_limit = {"600001": (11.0, 10.0)}                      # 昨日涨停 → 不是首板
    assert watch_candidates(quotes, cmap, ret5, prev_limit, TODAY) == []
    sealed = dict(quotes, **{"600001": q("600001", 10.0, amount=9e8)})   # 已封死
    assert watch_candidates(sealed, cmap, ret5, prev, TODAY) == []


def test_scan_trigger_window_and_sealed():
    assert in_scan_window(time(9, 30)) and in_scan_window(time(10, 29)) and not in_scan_window(time(10, 30))
    assert scan_trigger(q("600001", 9.2), datetime(2026, 9, 24, 9, 45))
    assert not scan_trigger(q("600001", 8.9), datetime(2026, 9, 24, 9, 45))
    assert not scan_trigger(q("600001", 10.0), datetime(2026, 9, 24, 9, 45))   # 已封死
    assert not scan_trigger(q("600001", 9.5), datetime(2026, 9, 24, 13, 30))   # 窗口外
```
（`tests/factories.py` 的 `quote()` 若尚无 `amount`、`ts` 参数则在本任务中补上，默认值保持不变。）

- [ ] TDD；Commit `feat: first-board selection rules (hot concepts, leader score, scan trigger)`

---

### Task 4: 首板卖出规则

**Files:**
- Create: `jevq/rules/board_sell.py`
- Test: `tests/test_board_sell.py`

**完整代码：**
```python
from datetime import date, datetime

from jevq.config import EOD_SELL, MARKET_OPEN
from jevq.models import Position, Quote
from jevq.rules.sell import SellDecision

HOLD = SellDecision(False)
STOP = -3.0
PULLBACK = 2.0


def decide_board_sell(pos: Position, q: Quote, now: datetime, prev_trade_day: date | None = None) -> SellDecision:
    if pos.buy_date >= now.date() or now.time() < MARKET_OPEN:
        return HOLD
    sealed = q.price >= q.limit_up
    if prev_trade_day is not None and pos.buy_date < prev_trade_day and not sealed:
        return SellDecision(True, "隔夜超期")
    if q.pct <= STOP:
        return SellDecision(True, "止损")
    if sealed:
        return HOLD
    high_pct = round((q.high / q.pre_close - 1) * 100, 2)
    if q.high >= q.limit_up:
        return SellDecision(True, "炸板")
    if high_pct > 0 and round(high_pct - q.pct, 2) >= PULLBACK:
        return SellDecision(True, "冲高回落")
    if now.time() >= EOD_SELL:
        return SellDecision(True, "尾盘清仓")
    return HOLD
```

**测试（完整）：**
```python
from datetime import date, datetime

from factories import position, quote
from jevq.rules.board_sell import decide_board_sell

BUY = date(2026, 9, 23)


def at(h, m):
    return datetime(2026, 9, 24, h, m)


def qq(pct, high_pct=None, **kw):
    x = quote(pct=pct, ts=datetime(2026, 9, 24, 10, 0), **kw)
    if high_pct is not None:
        from dataclasses import replace
        x = replace(x, high=round(x.pre_close * (1 + high_pct / 100), 2))
    return x


def d(q_, now=at(10, 0), buy=BUY, prev=None):
    return decide_board_sell(position(buy_date=buy), q_, now, prev)


def test_same_day_and_preopen_hold():
    assert not d(qq(-5), buy=date(2026, 9, 24)).sell
    assert not d(qq(-5), now=at(9, 25)).sell


def test_stop_loss():
    r = d(qq(-3.0, high_pct=1))
    assert r.sell and r.reason == "止损"


def test_sealed_limit_up_holds_even_at_eod():
    assert not d(qq(10.0, high_pct=10.0), now=at(14, 56)).sell


def test_broken_board_sells():
    r = d(qq(8.0, high_pct=10.0))
    assert r.sell and r.reason == "炸板"


def test_pullback_two_points():
    assert not d(qq(4.5, high_pct=6.0)).sell
    r = d(qq(4.0, high_pct=6.0))
    assert r.sell and r.reason == "冲高回落"


def test_eod_liquidation_when_not_sealed():
    r = d(qq(1.0, high_pct=1.5), now=at(14, 55))
    assert r.sell and r.reason == "尾盘清仓"


def test_stale_position_sells_unless_sealed():
    r = d(qq(2.0, high_pct=2.0), buy=date(2026, 9, 21), prev=date(2026, 9, 23))
    assert r.sell and r.reason == "隔夜超期"
    assert not d(qq(10.0, high_pct=10.0), buy=date(2026, 9, 21), prev=date(2026, 9, 23)).sell
```

- [ ] TDD；Commit `feat: first-board sell rules`

---

### Task 5: BoardStrategy（观察池 + 快速买点 + 卖出 + 留痕）

**Files:**
- Create: `jevq/strategies/__init__.py`（空）, `jevq/strategies/board.py`
- Test: `tests/test_board_strategy.py`

**Interfaces（Consumes）:** `Ledger`、`NoticeJudge.check(code, today)`/`NewsJudge.check(code, name, now)`（含 `last_trace`）、`watch_candidates`、`scan_trigger`、`decide_board_sell`、`closes` 表（昨收/前日收，经 `prev_closes_from_db`）、`concepts.load_concepts`、`tencent.fetch_quotes`

**Interfaces（Produces）:**
```python
class BoardStrategy:
    id = "board"
    def __init__(self, conn, ledger, notice_judge, news_judge, fetch_quotes, universe: dict[str, str],
                 ret5: dict[str, float], concept_map: dict[str, list[str]],
                 prev_closes: dict[str, tuple[float | None, float | None]], prev_day: date,
                 accounts=("board_main", "board_shadow"), windows=SCAN_WINDOWS, enabled: bool = True): ...
    watch: dict[str, Candidate]            # 当前观察池
    verdicts: dict[str, tuple[str, str, dict]]   # code → (verdict, reason, trace)，当日缓存
    stale_before: date | None              # 同 Engine，默认 prev_day；日历回退时由 run.py 置 None
    def on_minute(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]
    def on_fast(self, now: datetime) -> list[Fill]
    def has_watch(self) -> bool
```
另在 `jevq/data/closes.py` 增加 `prev_closes_from_db(conn, codes, today) -> dict[str, tuple[float | None, float | None]]`：最近两个 `date < today` 的不同日期的收盘价（昨收、前日收）。

**行为：**
- `on_minute`：
  1. 卖出：对 `accounts` 的持仓，用 `quotes` 中当日报价调用 `decide_board_sell(pos, q, now, self.stale_before)`，满足则 `ledger.sell`。非当日报价跳过（同 Engine）。
  2. `enabled` 为假（题材数据缺失）时到此为止。
  3. 观察池维护：移出涨幅 < `EXIT_PCT` 或行情非当日的；用 `watch_candidates(...)`（`quotes` 只取 `universe` 内）得到新候选，加入观察池（总数 ≤ `WATCH_MAX`，已在当日否决集中的跳过）。
  4. 新进观察池的票立即做 Jev 判断：先 `notice_judge.check`，通过再 `news_judge.check`（异常视为 error，与 Engine 的 `_check` 一致）；把 `(verdict, reason, trace)` 存入 `verdicts`；`reject`/`error` 立即移出观察池并记入当日否决集；每次判断写一条 `decisions`（`strategy='board'`，trace 见下）。
- `on_fast`：观察池为空直接返回；否则只请求观察池代码的报价（一次 `fetch_quotes`，异常只打日志返回 []）。对每只：
  - 主账户：`verdicts[code][0] == "pass"`、有空位、未持有、`scan_trigger(q, now, windows)` → `ledger.buy`；成交后把 trace 的 `outcome` 更新为买入并补写一条 `decisions`（verdict `pass`、detail `扫板 {pct}%`）。
  - 影子账户：同样的 `scan_trigger` 与空位条件，跳过 Jev。
  - 已封死（价格 ≥ 涨停价）的快照在 trace 中记 `"sealed_at_snapshot": true`（仅用于留痕，不成交）。
- trace 结构：`{"strategy": "board", "candidate": {"name","price","pct","ret5","concept","heat","score","ranks"}, "notices": ..., "news": ..., "outcome": {...}}`
- 账户满仓时不再做新的 Jev 判断（节省调用），但观察池仍维护。

**Tests（用假 judge、假 fetch_quotes、内存库）:**
- 热点题材龙头进入观察池并触发一次 Jev 判断（judge 调用 1 次）；第二分钟不重复判断。
- 否决的票移出观察池且当天不再进入。
- `on_fast`：9.2% 未封死 → 主、影子各买一笔；10.0% 已封死 → 不买；窗口外（13:30）→ 不买；主账户 verdict 为 reject → 只有影子买。
- 卖出：`board_main` 持仓次日炸板 → `on_minute` 卖出，原因「炸板」；`main`（低吸账户）的持仓不被 BoardStrategy 触碰。
- `enabled=False`：只卖不选。
- `on_fast` 行情抓取抛异常 → 返回 []，不影响下一次。
- `decisions.strategy == "board"`，trace 含 concept、heat、ranks。
- `prev_closes_from_db`：closes 有 3 天数据时返回最近两天。

- [ ] TDD；Commit `feat: BoardStrategy with watchlist, 3s scan trigger, board sells and traces`

---

### Task 6: 运行循环接入（run_day）

**Files:**
- Modify: `run.py`
- Test: `tests/test_run.py`

**行为：**
- 盘前：`needs_refresh` 为真时 `refresh_concepts`（异常只记日志）；`concept_map = load_concepts(conn)`；为空时扫首板 `enabled=False`，`state["warning"]` 追加「题材数据缺失，扫首板今日只卖不买」。
- `prev_closes = closes.prev_closes_from_db(conn, universe, today)`。
- 构造 `BoardStrategy(...)`（日历回退时 `stale_before=None`，与 Engine 同步）。
- 盘中每分钟：`_safe_tick(engine, ...)`（现有）之后，用 `engine.last_quotes` 调 `board.on_minute(...)`，同样包 `try/except`（异常记日志，不影响下一步）。
- 同一分钟剩余时间：若 `board.has_watch()` 且在交易时段，按 3 秒节拍调用 `board.on_fast(now)`（包 `try/except`），直到下一分钟边界或 stop；否则休眠到下一分钟（现有 `seconds_to_next_minute`）。节拍函数写成可测的纯函数 `next_wake(now, has_watch, fast_interval=3) -> float`（返回应等待秒数，不超过到下一分钟的秒数）。
- 持仓代码、日志与报告覆盖四个账户（Task 1 已改 `ACCOUNTS`）。
- `state` 增加 `board_watch`（观察池代码与名称列表）供面板显示。

**Tests:**
- `next_wake`：无观察池 → 到下一分钟的秒数；有观察池 → 3 秒；距下一分钟 1 秒时 → 1 秒。
- 假时钟整日运行：题材表为空 → `warning` 含题材提示且 BoardStrategy `enabled=False`；`board.on_minute` 抛异常不影响 `engine.tick` 与后续分钟；观察池非空时同一分钟内 `on_fast` 被调用多次。

- [ ] TDD；Commit `feat: run loop drives board strategy (minute + 3s scan)`

---

### Task 7: 封板时段统计

**Files:**
- Create: `jevq/data/board_events.py`
- Modify: `jevq/db.py`（`board_events` 表）, `jevq/data/akdata.py`（`fetch_zt_pool(d) -> DataFrame`，经 `call_with_timeout`）, `run.py`（收盘写报告前调用）
- Test: `tests/test_board_events.py`

**Interfaces:**
- 表：`board_events(date TEXT, code TEXT, name TEXT, first_seal TEXT, bucket TEXT, concept TEXT, next_open_pct REAL, next_close_pct REAL, next_board INTEGER, PRIMARY KEY(date, code))`
- `bucket_of(first_seal: str) -> str`：`"HHMMSS"` → `早盘`(09:25–10:30) / `上午`(10:30–11:30) / `午盘`(11:30–13:30，另把 11:00–11:30 归「上午」、13:00–13:30 归「午盘」) / `下午`(13:30–14:00) / `尾盘`(14:00–15:00)
- `record_day(conn, day, zt_rows: list[dict], concept_map) -> int`：只取 `连板数 == 1` 的首板，写入（concept 取该股题材列表第一个或空）
- `fill_next_day(conn, prev_day, quotes: dict[str, Quote], today) -> int`：对 `date == prev_day` 且 `next_close_pct IS NULL` 的记录，用当日报价（`ts.date() == today`）计算 `next_open_pct = (open/pre_close-1)*100`、`next_close_pct = pct`、`next_board = price >= limit_up`
- `summary(conn) -> list[dict]`：按 bucket 汇总 `count, avg_next_open, avg_next_close, board_rate`（只统计已回填的）
- `run.py` 收盘段：拿到全市场报价后先 `fill_next_day(conn, prev_day, quotes, today)`，再 `record_day(conn, today, fetch_zt_pool(today) 转 dict 行, concept_map)`，均 `try/except` 记日志。

**Tests:** bucket 边界（092500→早盘、103000→上午、113000→午盘、133000→下午、140000→尾盘）；record_day 只收首板；fill_next_day 计算正确且忽略非当日报价；summary 聚合正确。

- [ ] TDD；Commit `feat: first-board seal-time statistics`

---

### Task 8: 面板与报告

**Files:**
- Modify: `jevq/app/api.py`, `jevq/app/ui.html`, `jevq/report.py`
- Test: `tests/test_api.py`, `tests/test_report.py`

**行为：**
- 面板账户卡片按策略分两行（低吸 / 扫首板），每行两张卡；净值曲线四条线（低吸主、低吸影子、首板主、首板影子，颜色区分并有图例）。
- 「Jev 判断」标签页加策略筛选（全部 / 低吸 / 扫首板），调用 `api.decisions(day, strategy)`。
- 决策明细：`strategy == "board"` 时，「为什么被选中」改为显示：所属题材、题材热度（涨超 5% 成员数）、龙头分及三项名次（涨幅/成交额/5 日）、当前涨幅、是否在快照时已封死。所有外部字符串经 `esc()`。
- 顶栏：若 `state["board_watch"]` 非空显示「观察池：名称…」（经 `esc()`）。
- 新标签页「封板统计」：`api.board_stats()` → `board_events.summary(conn)`，表格列：时段、样本数、次日平均高开、次日平均收盘、次日连板率。
- 报告：净值按策略分组（Task 1 已做）；新增「今日首板时段分布」小表（各 bucket 数量）。

**Tests:** `api.board_stats()` 返回 summary；`status()` 带 `board_watch`；报告含首板时段分布表。

- [ ] TDD；Commit `feat: panel and report for two strategies and seal-time stats`

---

### Task 9: 实盘验证（controller 执行）

- 下一个交易日盘中打开面板确认四个账户、观察池、决策明细显示正常；收盘后检查 `board_events` 写入、`concepts` 表规模（应 > 5000 行）。
- 将结果记入 ledger。
