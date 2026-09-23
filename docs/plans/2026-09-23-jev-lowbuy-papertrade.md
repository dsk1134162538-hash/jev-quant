# Jev 低吸模拟盘 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭一套 A 股主板 T+1 低吸模拟盘，主账户用 Jev 做公告排雷和消息面过滤，影子账户不做过滤，每天对比两者净值。

**Architecture:** 数据层（腾讯行情 + AKShare）→ 判断层（Jev HTTP API）→ 规则层（纯函数）→ 账本（SQLite）→ 引擎每分钟跑一轮 → 收盘写复盘报告。规则和账本不依赖网络，全部用单元测试覆盖；数据层和判断层通过注入的 fetch 函数或 session 做离线测试。

**Tech Stack:** Python 3.12、requests、pandas、akshare 1.18.x、sqlite3（标准库）、pytest、pywebview（桌面窗口）

**Spec:** `docs/specs/2026-09-23-jev-lowbuy-papertrade-design.md`

## Global Constraints

- 股票池：代码以 `60` / `00` 开头；剔除名称含 `ST`；剔除上市不满 60 个交易日
- 所有涨跌幅相对**昨日收盘价**，单位 %，保留两位小数
- 初始资金 30000 元/账户；最多同时持有 2 只；单笔预算 `min(可用现金, 15000)`；100 股整手
- 费用：佣金 `max(5, 金额×0.00025)`；印花税仅卖出 `金额×0.0005`；过户费双向 `金额×0.00001`
- 买入区间：`-3.00 ≤ 涨跌幅 ≤ 0.00`
- 卖出：止损 `≤ -3%`；回到 0 轴 `≥ -0.2%`；反弹 `当前 - 日内最低 ≥ 1.5` 个点；`14:55` 尾盘清仓
- 尾盘买入窗口 `14:50–14:57`；盘中买入窗口 `09:30–14:57`
- 公告回看 90 天；新闻回看 24 小时；**公告排雷**中 Jev 置信度 `< 0.6` 视为拿不准（设计文档 §5 第 3 步；消息面/情绪/外围不做置信度门槛，见 ledger R12）
- Jev：`POST https://api.typesafe.ai/v1/systemone`，模型 `jev-latest`，API Key 从环境变量 `TYPESAFE_API_KEY` 读取；或经 Cloudflare Workers AI（`typesafe/jev`，`CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`，Task 14）
- 出问题宁可不买，但不能卖不掉：卖出路径不调用 Jev
- 所有命令在 `D:\claude\jev-quant` 下执行，Python 用 `.venv/Scripts/python`

## File Structure

```
jev-quant/
  requirements.txt
  pytest.ini
  .gitignore
  run.py                     # 每日调度入口（run_day）
  desktop.py                 # 桌面窗口入口（pythonw）
  install_shortcut.py        # 创建桌面快捷方式
  jevq/
    __init__.py
    config.py                # 全部常量
    models.py                # Quote / Position / Fill
    db.py                    # SQLite 连接与表结构
    ledger.py                # 模拟盘账本、费用、整手
    engine.py                # 每分钟一轮的编排
    report.py                # 复盘报告
    app/
      __init__.py
      api.py                 # 暴露给窗口前端的 Python 方法
      ui.html                # 面板页面
    rules/
      __init__.py
      sell.py                # decide_sell
      buy.py                 # in_buy_zone / buy_mode / buy_window_open / rank_candidates
    data/
      __init__.py
      tencent.py             # 实时行情、日 K、外围
      akdata.py              # 交易日、股票池、公告、新闻、涨跌停池
      notice_store.py        # 公告落库与查询
    judge/
      __init__.py
      jev.py                 # JevClient、score_label
      notices.py             # NoticeJudge
      news.py                # NewsJudge
      market.py              # MarketJudge
  tests/
    factories.py
    test_models.py
    test_sell.py
    test_buy.py
    test_ledger.py
    test_tencent.py
    test_akdata.py
    test_jev.py
    test_notice_judge.py
    test_news_market.py
    test_engine.py
    test_report.py
    test_api.py
```

判断层 `check()` 统一返回 `(verdict, reason)`，其中 verdict ∈ `{"pass", "reject", "error"}`：`reject` 当日不再检查，`error`（Jev 或数据失败）下一轮重试。

---

### Task 1: 项目骨架、配置、数据模型

**Files:**
- Create: `requirements.txt`, `pytest.ini`, `.gitignore`, `jevq/__init__.py`, `jevq/config.py`, `jevq/models.py`, `jevq/rules/__init__.py`, `jevq/data/__init__.py`, `jevq/judge/__init__.py`, `tests/factories.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `jevq.config` 下全部常量；`Quote`（属性 `pct`、`open_pct`、`low_pct`、`suspended`、`is_st`）、`Position`、`Fill`；测试工厂 `factories.quote(...)`、`factories.position(...)`

- [ ] **Step 1: 初始化 git 与基础文件**

```bash
git init
```

`requirements.txt`:
```
akshare>=1.18
pandas
requests
pytest
```

`pytest.ini`:
```ini
[pytest]
pythonpath = . tests
testpaths = tests
```

`.gitignore`:
```
.venv/
__pycache__/
/data/
/reports/
*.sqlite
.superpowers/
```

`jevq/__init__.py`、`jevq/rules/__init__.py`、`jevq/data/__init__.py`、`jevq/judge/__init__.py`：空文件。

- [ ] **Step 2: 写配置**

`jevq/config.py`:
```python
from datetime import time

# 资金与仓位
INITIAL_CASH = 30_000.0
MAX_PER_TRADE = 15_000.0
MAX_POSITIONS = 2
LOT = 100

# 费用
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_RATE = 0.0005          # 仅卖出
TRANSFER_RATE = 0.00001      # 双向

# 买入
BUY_ZONE_LOW = -3.0
BUY_ZONE_HIGH = 0.0
MAX_CHECKS_PER_TICK = 10

# 卖出
STOP_LOSS = -3.0
ZERO_LINE = -0.2
REBOUND = 1.5
TAKE_PROFIT = 1.0
LOW_OPEN = -1.0

# 时间
MARKET_OPEN = time(9, 30)
TAIL_BUY_START = time(14, 50)
BUY_END = time(14, 57)
EOD_SELL = time(14, 55)
SENTIMENT_REFRESH_MIN = 30

# 过滤
NOTICE_LOOKBACK_DAYS = 90
NEWS_LOOKBACK_HOURS = 24
MIN_LISTED_TRADING_DAYS = 60
LOW_CONFIDENCE = 0.6

# Jev
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
JEV_TIMEOUT = 5.0

DB_PATH = "data/jevq.sqlite"
REPORT_DIR = "reports"
ACCOUNTS = ("main", "shadow")
```

- [ ] **Step 3: 写失败的模型测试**

`tests/factories.py`:
```python
from datetime import date, datetime

from jevq.models import Position, Quote


def _p(pre_close: float, pct: float) -> float:
    return round(pre_close * (1 + pct / 100), 2)


def quote(code="600001", pct=0.0, open_pct=None, low_pct=None, pre_close=10.0,
          name="测试股份", volume=1000, amount=1e7, ts=datetime(2026, 9, 24, 10, 0)):
    price = _p(pre_close, pct)
    op = price if open_pct is None else _p(pre_close, open_pct)
    low = min(price, op) if low_pct is None else _p(pre_close, low_pct)
    return Quote(code=code, name=name, price=price, pre_close=pre_close, open=op,
                 high=max(price, op), low=low, volume=volume, amount=amount,
                 limit_up=_p(pre_close, 10), limit_down=_p(pre_close, -10), ts=ts)


def position(code="600001", account="main", shares=1000, buy_price=10.0,
             buy_date=date(2026, 9, 23)):
    return Position(account=account, code=code, shares=shares,
                    cost=shares * buy_price + 5.1, buy_price=buy_price, buy_date=buy_date)
```

`tests/test_models.py`:
```python
from factories import quote


def test_pct_relative_to_pre_close():
    q = quote(pct=-3.0, open_pct=-1.5, low_pct=-3.2)
    assert q.pct == -3.0
    assert q.open_pct == -1.5
    assert q.low_pct == -3.2


def test_suspended_when_no_volume():
    assert quote(volume=0).suspended
    assert not quote().suspended


def test_is_st():
    assert quote(name="*ST海润").is_st
    assert quote(name="ST康美").is_st
    assert not quote(name="贵州茅台").is_st
```

- [ ] **Step 4: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_models.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jevq.models'`

- [ ] **Step 5: 实现模型**

`jevq/models.py`:
```python
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: float
    pre_close: float
    open: float
    high: float
    low: float
    volume: int        # 手
    amount: float      # 元
    limit_up: float
    limit_down: float
    ts: datetime

    def _pct(self, p: float) -> float:
        return round((p / self.pre_close - 1) * 100, 2)

    @property
    def pct(self) -> float:
        return self._pct(self.price)

    @property
    def open_pct(self) -> float:
        return self._pct(self.open)

    @property
    def low_pct(self) -> float:
        return self._pct(self.low)

    @property
    def suspended(self) -> bool:
        return self.volume <= 0 or self.price <= 0

    @property
    def is_st(self) -> bool:
        return "ST" in self.name.upper()


@dataclass(frozen=True)
class Position:
    account: str
    code: str
    shares: int
    cost: float        # 含买入费用
    buy_price: float
    buy_date: date


@dataclass(frozen=True)
class Fill:
    account: str
    code: str
    side: str          # "buy" / "sell"
    price: float
    shares: int
    fee: float
    ts: datetime
    reason: str
```

- [ ] **Step 6: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_models.py -v`
Expected: 3 passed

- [ ] **Step 7: Commit**

```bash
git add requirements.txt pytest.ini .gitignore jevq tests docs
git commit -m "feat: project skeleton, config and models"
```

---

### Task 2: 卖出规则

**Files:**
- Create: `jevq/rules/sell.py`
- Test: `tests/test_sell.py`

**Interfaces:**
- Consumes: `Quote`、`Position`、config 常量
- Produces: `SellDecision(sell: bool, reason: str)`；`decide_sell(pos: Position, q: Quote, now: datetime) -> SellDecision`

- [ ] **Step 1: 写失败的测试（逐行对应设计文档 3.2）**

`tests/test_sell.py`:
```python
from datetime import date, datetime

import pytest

from factories import position, quote
from jevq.rules.sell import decide_sell

NEXT = date(2026, 9, 24)


def at(h, m):
    return datetime(2026, 9, 24, h, m)


def run(pct, open_pct, low_pct=None, when=at(9, 31), buy_date=date(2026, 9, 23)):
    return decide_sell(position(buy_date=buy_date), quote(pct=pct, open_pct=open_pct, low_pct=low_pct), when)


def test_t_plus_one_same_day_never_sells():
    assert not run(-5.0, -5.0, buy_date=NEXT).sell


def test_before_open_does_nothing():
    assert not run(2.0, 2.0, when=at(9, 25)).sell


@pytest.mark.parametrize("open_pct", [1.0, 2.5, 3.0, 4.5])
def test_high_open_sells_immediately(open_pct):
    d = run(open_pct, open_pct)
    assert d.sell and d.reason == "高开止盈"


def test_flat_open_holds_until_plus_one():
    assert not run(0.8, 0.5).sell
    d = run(1.0, 0.5)
    assert d.sell and d.reason == "摸到+1%"


def test_low_open_holds_then_sells_back_to_zero():
    assert not run(-1.0, -1.5, low_pct=-1.5).sell
    d = run(-0.2, -1.5, low_pct=-1.5)
    assert d.sell and d.reason == "回到0轴"


def test_low_open_sells_on_rebound():
    d = run(-1.3, -1.5, low_pct=-2.8)
    assert d.sell and d.reason == "反弹"


def test_low_open_minus_2_5_same_group():
    assert not run(-2.0, -2.5, low_pct=-2.5).sell


def test_open_exactly_minus_one_is_low_open_group():
    assert not run(-0.5, -1.0, low_pct=-1.0).sell
    assert run(-0.2, -1.0, low_pct=-1.0).sell


@pytest.mark.parametrize("pct,open_pct", [(-3.0, -1.5), (-3.5, 0.5), (-2.9, -3.2)])
def test_stop_loss(pct, open_pct):
    d = run(pct, open_pct, low_pct=min(pct, open_pct))
    assert d.sell and d.reason == "止损"


def test_eod_liquidation():
    d = run(-0.8, -1.5, low_pct=-1.5, when=at(14, 55))
    assert d.sell and d.reason == "尾盘清仓"
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_sell.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jevq.rules.sell'`

- [ ] **Step 3: 实现**

`jevq/rules/sell.py`:
```python
from dataclasses import dataclass
from datetime import datetime

from jevq.config import (EOD_SELL, LOW_OPEN, MARKET_OPEN, REBOUND, STOP_LOSS,
                         TAKE_PROFIT, ZERO_LINE)
from jevq.models import Position, Quote


@dataclass(frozen=True)
class SellDecision:
    sell: bool
    reason: str = ""


HOLD = SellDecision(False)


def decide_sell(pos: Position, q: Quote, now: datetime) -> SellDecision:
    if pos.buy_date >= now.date() or now.time() < MARKET_OPEN:
        return HOLD
    pct, op = q.pct, q.open_pct
    if pct <= STOP_LOSS or op <= STOP_LOSS:
        return SellDecision(True, "止损")
    if op >= TAKE_PROFIT:
        return SellDecision(True, "高开止盈")
    if op > LOW_OPEN:
        if pct >= TAKE_PROFIT:
            return SellDecision(True, "摸到+1%")
    elif pct >= ZERO_LINE:
        return SellDecision(True, "回到0轴")
    elif round(pct - q.low_pct, 2) >= REBOUND:
        return SellDecision(True, "反弹")
    # 兜底：具体条件都没触发时，14:55 清仓，不隔第二夜（见 ledger R9）
    if now.time() >= EOD_SELL:
        return SellDecision(True, "尾盘清仓")
    return HOLD
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_sell.py -v`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add jevq/rules/sell.py tests/test_sell.py
git commit -m "feat: sell rules"
```

---

### Task 3: 买入规则与候选排序

**Files:**
- Create: `jevq/rules/buy.py`
- Test: `tests/test_buy.py`

**Interfaces:**
- Consumes: `Quote`、config 常量
- Produces:
  - `in_buy_zone(q: Quote) -> bool`
  - `buy_mode(sentiment: str | None, overseas: str | None) -> str`，返回 `"tail"` 或 `"intraday"`
  - `buy_window_open(mode: str, t: time) -> bool`
  - `rank_candidates(quotes: Iterable[Quote], ret5: dict[str, float]) -> list[Quote]`

- [ ] **Step 1: 写失败的测试**

`tests/test_buy.py`:
```python
from datetime import time

import pytest

from factories import quote
from jevq.rules.buy import buy_mode, buy_window_open, in_buy_zone, rank_candidates


@pytest.mark.parametrize("pct,ok", [(0.0, True), (-1.5, True), (-3.0, True), (0.1, False), (-3.1, False)])
def test_zone_bounds(pct, ok):
    assert in_buy_zone(quote(pct=pct)) is ok


def test_zone_excludes_st_and_suspended():
    assert not in_buy_zone(quote(pct=-1, name="ST测试"))
    assert not in_buy_zone(quote(pct=-1, volume=0))


@pytest.mark.parametrize("sentiment,overseas,mode", [
    ("正常", "中性", "intraday"),
    ("火热", "偏强", "intraday"),
    ("偏弱", "偏强", "tail"),
    ("冰点", "中性", "tail"),
    ("火热", "偏弱", "tail"),
    (None, "中性", "tail"),
    ("正常", None, "tail"),
])
def test_buy_mode(sentiment, overseas, mode):
    assert buy_mode(sentiment, overseas) == mode


def test_windows():
    assert buy_window_open("intraday", time(9, 30))
    assert buy_window_open("intraday", time(14, 56))
    assert not buy_window_open("intraday", time(14, 57))
    assert not buy_window_open("tail", time(10, 0))
    assert buy_window_open("tail", time(14, 50))
    assert not buy_window_open("tail", time(14, 57))


def test_rank_by_5d_return_desc_and_filters():
    qs = [quote("600001", pct=-1), quote("600002", pct=-2), quote("600003", pct=1),
          quote("600004", pct=-0.5)]
    ret5 = {"600001": 3.0, "600002": 12.0, "600003": 20.0}
    ranked = [q.code for q in rank_candidates(qs, ret5)]
    assert ranked == ["600002", "600001"]   # 600003 不在区间；600004 无 5 日数据
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_buy.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现**

`jevq/rules/buy.py`:
```python
from collections.abc import Iterable
from datetime import time

from jevq.config import BUY_END, BUY_ZONE_HIGH, BUY_ZONE_LOW, MARKET_OPEN, TAIL_BUY_START
from jevq.models import Quote

WEAK_SENTIMENT = {"冰点", "偏弱"}


def in_buy_zone(q: Quote) -> bool:
    return (not q.suspended and not q.is_st
            and BUY_ZONE_LOW <= q.pct <= BUY_ZONE_HIGH)


def buy_mode(sentiment: str | None, overseas: str | None) -> str:
    if sentiment is None or overseas is None:
        return "tail"
    if sentiment in WEAK_SENTIMENT or overseas == "偏弱":
        return "tail"
    return "intraday"


def buy_window_open(mode: str, t: time) -> bool:
    start = TAIL_BUY_START if mode == "tail" else MARKET_OPEN
    return start <= t < BUY_END


def rank_candidates(quotes: Iterable[Quote], ret5: dict[str, float]) -> list[Quote]:
    pool = [q for q in quotes if q.code in ret5 and in_buy_zone(q)]
    return sorted(pool, key=lambda q: ret5[q.code], reverse=True)
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_buy.py -v`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add jevq/rules/buy.py tests/test_buy.py
git commit -m "feat: buy rules and candidate ranking"
```

---

### Task 4: 数据库与模拟盘账本

**Files:**
- Create: `jevq/db.py`, `jevq/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Consumes: `Quote`、`Position`、`Fill`、config
- Produces:
  - `db.connect(path: str) -> sqlite3.Connection`（自动建表；`":memory:"` 可用）
  - `buy_fee(amount) -> float`、`sell_fee(amount) -> float`、`affordable_shares(budget, price) -> int`
  - `Ledger(conn)`：`cash(account)`、`positions(account) -> list[Position]`、`holds(account, code) -> bool`、`free_slots(account) -> int`、`buy(account, q, ts, reason) -> Fill | None`、`sell(account, pos, q, ts, reason) -> Fill | None`、`equity(account, prices: dict[str, float]) -> float`、`fills_on(day: date) -> list[Fill]`

- [ ] **Step 1: 写数据库模块**

`jevq/db.py`:
```python
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cash(account TEXT PRIMARY KEY, amount REAL);
CREATE TABLE IF NOT EXISTS positions(
  account TEXT, code TEXT, shares INTEGER, cost REAL, buy_price REAL, buy_date TEXT,
  PRIMARY KEY(account, code));
CREATE TABLE IF NOT EXISTS fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, code TEXT, side TEXT,
  price REAL, shares INTEGER, fee REAL, ts TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY, code TEXT, title TEXT, type TEXT, date TEXT);
CREATE INDEX IF NOT EXISTS idx_notices_code ON notices(code, date);
CREATE TABLE IF NOT EXISTS notice_fetch(date TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS notice_verdicts(id TEXT PRIMARY KEY, label TEXT, confidence REAL, source TEXT);
CREATE TABLE IF NOT EXISTS day_cache(date TEXT, kind TEXT, key TEXT, value TEXT,
  PRIMARY KEY(date, kind, key));
CREATE TABLE IF NOT EXISTS jev_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, request TEXT, response TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, code TEXT, verdict TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS turnover(ts TEXT PRIMARY KEY, amount REAL);
CREATE TABLE IF NOT EXISTS equity(date TEXT, account TEXT, value REAL, PRIMARY KEY(date, account));
"""


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
    conn.executescript(SCHEMA)
    return conn
```

- [ ] **Step 2: 写失败的账本测试**

`tests/test_ledger.py`:
```python
from datetime import date, datetime

import pytest

from factories import quote
from jevq import db
from jevq.ledger import Ledger, affordable_shares, buy_fee, sell_fee

T = datetime(2026, 9, 23, 14, 50)


@pytest.fixture
def ledger():
    return Ledger(db.connect(":memory:"))


def test_fees():
    assert buy_fee(15000) == 5.15            # 佣金 3.75 不足 5 元按 5 元 + 过户费 0.15
    assert sell_fee(15000) == 12.65          # 5 + 印花税 7.5 + 过户费 0.15
    assert buy_fee(40000) == 10.4            # 佣金 10 + 过户费 0.4


def test_affordable_shares_leaves_room_for_fee():
    assert affordable_shares(15000, 10.0) == 1400
    assert affordable_shares(15000, 9.99) == 1500
    assert affordable_shares(15000, 151.0) == 0


def test_buy_updates_cash_and_position(ledger):
    f = ledger.buy("main", quote(pct=-1.0), T, "test")
    assert f.shares == 1500 and f.price == 9.9
    assert ledger.cash("main") == pytest.approx(30000 - 14850 - buy_fee(14850))
    assert ledger.holds("main", "600001")
    assert ledger.cash("shadow") == 30000


def test_max_two_positions_and_no_duplicate(ledger):
    assert ledger.buy("main", quote("600001", pct=-1), T, "")
    assert ledger.buy("main", quote("600001", pct=-1), T, "") is None
    assert ledger.buy("main", quote("600002", pct=-1), T, "")
    assert ledger.free_slots("main") == 0
    assert ledger.buy("main", quote("600003", pct=-1), T, "") is None


def test_cannot_buy_at_limit_up_or_suspended(ledger):
    assert ledger.buy("main", quote(pct=10.0), T, "") is None
    assert ledger.buy("main", quote(pct=-1, volume=0), T, "") is None


def test_sell_round_trip(ledger):
    ledger.buy("main", quote(pct=-1.0), T, "")
    pos = ledger.positions("main")[0]
    f = ledger.sell("main", pos, quote(pct=1.0), datetime(2026, 9, 24, 9, 31), "高开止盈")
    assert f.side == "sell" and f.price == 10.1
    assert not ledger.holds("main", "600001")
    expected = 30000 - 14850 - buy_fee(14850) + 15150 - sell_fee(15150)
    assert ledger.cash("main") == pytest.approx(expected)


def test_cannot_sell_at_limit_down(ledger):
    ledger.buy("main", quote(pct=-1.0), T, "")
    pos = ledger.positions("main")[0]
    assert ledger.sell("main", pos, quote(pct=-10.0), T, "止损") is None
    assert ledger.holds("main", "600001")


def test_equity_and_fills_on(ledger):
    ledger.buy("main", quote(pct=-1.0), T, "")
    value = ledger.equity("main", {"600001": 10.0})
    assert value == pytest.approx(ledger.cash("main") + 15000)
    assert len(ledger.fills_on(date(2026, 9, 23))) == 1
    assert ledger.fills_on(date(2026, 9, 24)) == []
```

- [ ] **Step 3: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_ledger.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jevq.ledger'`

- [ ] **Step 4: 实现**

`jevq/ledger.py`:
```python
import sqlite3
from datetime import date, datetime

from jevq.config import (ACCOUNTS, COMMISSION_MIN, COMMISSION_RATE, INITIAL_CASH, LOT,
                         MAX_PER_TRADE, MAX_POSITIONS, STAMP_RATE, TRANSFER_RATE)
from jevq.models import Fill, Position, Quote


def buy_fee(amount: float) -> float:
    return round(max(COMMISSION_MIN, amount * COMMISSION_RATE) + amount * TRANSFER_RATE, 2)


def sell_fee(amount: float) -> float:
    return round(max(COMMISSION_MIN, amount * COMMISSION_RATE)
                 + amount * (STAMP_RATE + TRANSFER_RATE), 2)


def affordable_shares(budget: float, price: float) -> int:
    lots = int(budget // (price * LOT))
    while lots > 0 and lots * LOT * price + buy_fee(lots * LOT * price) > budget:
        lots -= 1
    return lots * LOT


class Ledger:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        for a in ACCOUNTS:
            conn.execute("INSERT OR IGNORE INTO cash(account, amount) VALUES (?, ?)",
                         (a, INITIAL_CASH))
        conn.commit()

    def cash(self, account: str) -> float:
        return self.conn.execute("SELECT amount FROM cash WHERE account=?", (account,)).fetchone()[0]

    def positions(self, account: str) -> list[Position]:
        rows = self.conn.execute(
            "SELECT account, code, shares, cost, buy_price, buy_date FROM positions "
            "WHERE account=? ORDER BY buy_date, code", (account,)).fetchall()
        return [Position(a, c, s, cost, bp, date.fromisoformat(bd)) for a, c, s, cost, bp, bd in rows]

    def holds(self, account: str, code: str) -> bool:
        return self.conn.execute("SELECT 1 FROM positions WHERE account=? AND code=?",
                                 (account, code)).fetchone() is not None

    def free_slots(self, account: str) -> int:
        return MAX_POSITIONS - len(self.positions(account))

    def buy(self, account: str, q: Quote, ts: datetime, reason: str) -> Fill | None:
        if self.free_slots(account) <= 0 or self.holds(account, q.code):
            return None
        if q.suspended or q.price >= q.limit_up:
            return None
        shares = affordable_shares(min(self.cash(account), MAX_PER_TRADE), q.price)
        if shares == 0:
            return None
        amount = shares * q.price
        fee = buy_fee(amount)
        self.conn.execute("UPDATE cash SET amount = amount - ? WHERE account=?", (amount + fee, account))
        self.conn.execute("INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?)",
                          (account, q.code, shares, amount + fee, q.price, ts.date().isoformat()))
        return self._record(Fill(account, q.code, "buy", q.price, shares, fee, ts, reason))

    def sell(self, account: str, pos: Position, q: Quote, ts: datetime, reason: str) -> Fill | None:
        if q.suspended or q.price <= q.limit_down:
            return None
        amount = pos.shares * q.price
        fee = sell_fee(amount)
        self.conn.execute("UPDATE cash SET amount = amount + ? WHERE account=?", (amount - fee, account))
        self.conn.execute("DELETE FROM positions WHERE account=? AND code=?", (account, pos.code))
        return self._record(Fill(account, pos.code, "sell", q.price, pos.shares, fee, ts, reason))

    def equity(self, account: str, prices: dict[str, float]) -> float:
        held = sum(p.shares * prices.get(p.code, p.buy_price) for p in self.positions(account))
        return round(self.cash(account) + held, 2)

    def fills_on(self, day: date) -> list[Fill]:
        rows = self.conn.execute(
            "SELECT account, code, side, price, shares, fee, ts, reason FROM fills "
            "WHERE substr(ts, 1, 10)=? ORDER BY id", (day.isoformat(),)).fetchall()
        return [Fill(a, c, s, p, n, f, datetime.fromisoformat(t), r) for a, c, s, p, n, f, t, r in rows]

    def _record(self, f: Fill) -> Fill:
        self.conn.execute(
            "INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f.account, f.code, f.side, f.price, f.shares, f.fee, f.ts.isoformat(), f.reason))
        self.conn.commit()
        return f
```

- [ ] **Step 5: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_ledger.py -v`
Expected: 全部 passed

- [ ] **Step 6: Commit**

```bash
git add jevq/db.py jevq/ledger.py tests/test_ledger.py
git commit -m "feat: sqlite schema and paper-trading ledger"
```

---

### Task 5: 腾讯行情、日 K、外围

**Files:**
- Create: `jevq/data/tencent.py`
- Test: `tests/test_tencent.py`

**Interfaces:**
- Consumes: `Quote`
- Produces:
  - `symbol(code: str) -> str`（`600519` → `sh600519`）
  - `parse_quotes(text: str) -> dict[str, Quote]`
  - `fetch_quotes(codes: list[str], session=None) -> dict[str, Quote]`
  - `five_day_return(rows: list[tuple[str, float]], today: date) -> float | None`
  - `fetch_5d_returns(codes: list[str], today: date, workers=16) -> dict[str, float]`
  - `parse_index_pcts(text: str) -> dict[str, float]`、`parse_sina_a50(text: str) -> float`
  - `fetch_overseas(session=None) -> dict[str, float]`，key 为中文指数名

已核实的腾讯字段下标（2026-09-23）：1 名称、2 代码、3 现价、4 昨收、5 今开、6 成交量(手)、30 时间 `YYYYMMDDHHMMSS`、32 涨跌幅、33 最高、34 最低、37 成交额(万)、47 涨停价、48 跌停价。新浪 A50：`f[0]` 最新、`f[7]` 昨收。日 K 行：`[日期, 开, 收, 高, 低, 量]`。

- [ ] **Step 1: 写失败的测试**

`tests/test_tencent.py`:
```python
from datetime import date

from jevq.data.tencent import (five_day_return, parse_index_pcts, parse_quotes,
                               parse_sina_a50, symbol)


def tencent_line(sym, name, code, price, pre, op, high, low, vol, amount_wan, up, down,
                 pct="0.00", ts="20260923100547"):
    f = [""] * 50
    f[0], f[1], f[2], f[3], f[4], f[5], f[6] = "1", name, code, price, pre, op, vol
    f[30], f[32], f[33], f[34], f[37], f[47], f[48] = ts, pct, high, low, amount_wan, up, down
    return f'v_{sym}="{"~".join(f)}";'


def test_symbol():
    assert symbol("600519") == "sh600519"
    assert symbol("000001") == "sz000001"


def test_parse_quotes():
    text = (tencent_line("sh603136", "天目湖", "603136", "9.36", "9.32", "9.33", "9.40",
                         "9.21", "53106", "4972", "10.25", "8.39")
            + "\n" + tencent_line("sz000001", "平安银行", "000001", "11.59", "11.71", "11.68",
                                  "11.71", "11.58", "97618", "11302.5", "12.88", "10.54"))
    qs = parse_quotes(text)
    q = qs["603136"]
    assert q.name == "天目湖" and q.price == 9.36 and q.pre_close == 9.32
    assert q.volume == 53106 and q.amount == 49_720_000.0
    assert q.limit_up == 10.25 and q.limit_down == 8.39
    assert q.ts.hour == 10 and q.ts.minute == 5
    assert qs["000001"].pct == -1.02


def test_parse_quotes_skips_garbage():
    assert parse_quotes('v_pv_none_match="1";') == {}


def test_five_day_return_excludes_today():
    rows = [("2026-09-15", 10.0), ("2026-09-16", 10.2), ("2026-09-17", 10.4),
            ("2026-09-18", 10.6), ("2026-09-21", 10.8), ("2026-09-22", 11.0),
            ("2026-09-23", 20.0)]
    assert five_day_return(rows, date(2026, 9, 23)) == 10.0
    assert five_day_return(rows[:5], date(2026, 9, 23)) is None


def test_parse_index_pcts():
    text = (tencent_line("usDJI", "道琼斯", "DJI", "51863.69", "52048.83", "0", "0", "0",
                         "0", "0", "0", "0", pct="-0.36")
            + tencent_line("hkHSI", "恒生指数", "HSI", "24850.9", "25087.7", "0", "0", "0",
                           "0", "0", "0", "0", pct="-0.94"))
    assert parse_index_pcts(text) == {"usDJI": -0.36, "hkHSI": -0.94}


def test_parse_sina_a50():
    text = 'var hq_str_hf_CHA50CFD="14573.730,,14573.000,14574.000,14714.000,14571.000,10:05:50,14654.000,14659.000,765012";'
    assert parse_sina_a50(text) == -0.55
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_tencent.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现**

`jevq/data/tencent.py`:
```python
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import requests

from jevq.models import Quote

QT_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_A50_URL = "https://hq.sinajs.cn/list=hf_CHA50CFD"
OVERSEAS = {"usDJI": "道琼斯", "usIXIC": "纳斯达克", "usINX": "标普500",
            "usHXC": "中概金龙", "hkHSI": "恒生指数"}


def symbol(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def _records(text: str):
    for line in text.split(";"):
        line = line.strip()
        if '="' not in line:
            continue
        key, body = line.split('="', 1)
        yield key.replace("v_", "", 1).strip(), body.rstrip('"').split("~")


def parse_quotes(text: str) -> dict[str, Quote]:
    out = {}
    for _, f in _records(text):
        if len(f) < 49:
            continue
        try:
            q = Quote(code=f[2], name=f[1], price=float(f[3]), pre_close=float(f[4]),
                      open=float(f[5]), high=float(f[33]), low=float(f[34]),
                      volume=int(float(f[6])), amount=float(f[37]) * 10000,
                      limit_up=float(f[47]), limit_down=float(f[48]),
                      ts=datetime.strptime(f[30], "%Y%m%d%H%M%S"))
        except (ValueError, IndexError):
            continue
        if q.pre_close > 0:
            out[q.code] = q
    return out


def fetch_quotes(codes: list[str], session=None, chunk: int = 500) -> dict[str, Quote]:
    s = session or requests.Session()
    out: dict[str, Quote] = {}
    for i in range(0, len(codes), chunk):
        r = s.get(QT_URL + ",".join(symbol(c) for c in codes[i:i + chunk]), timeout=10)
        r.raise_for_status()
        out.update(parse_quotes(r.content.decode("gbk", errors="ignore")))
    return out


def five_day_return(rows: list[tuple[str, float]], today: date) -> float | None:
    past = [c for d, c in rows if d < today.isoformat()]
    if len(past) < 6 or past[-6] <= 0:
        return None
    return round((past[-1] / past[-6] - 1) * 100, 2)


def _kline(code: str, session) -> list[tuple[str, float]]:
    sym = symbol(code)
    r = session.get(KLINE_URL, params={"param": f"{sym},day,,,10,qfq"}, timeout=10)
    data = r.json()["data"][sym]
    rows = data.get("qfqday") or data.get("day") or []
    return [(row[0], float(row[2])) for row in rows]


def fetch_5d_returns(codes: list[str], today: date, workers: int = 16) -> dict[str, float]:
    s = requests.Session()

    def one(code):
        try:
            return code, five_day_return(_kline(code, s), today)
        except Exception:
            return code, None

    with ThreadPoolExecutor(workers) as pool:
        return {c: v for c, v in pool.map(one, codes) if v is not None}


def parse_index_pcts(text: str) -> dict[str, float]:
    out = {}
    for key, f in _records(text):
        try:
            out[key] = float(f[32])
        except (ValueError, IndexError):
            continue
    return out


def parse_sina_a50(text: str) -> float:
    f = text.split('"')[1].split(",")
    return round((float(f[0]) / float(f[7]) - 1) * 100, 2)


def fetch_overseas(session=None) -> dict[str, float]:
    s = session or requests.Session()
    out = {}
    r = s.get(QT_URL + ",".join(OVERSEAS), timeout=10)
    for key, pct in parse_index_pcts(r.content.decode("gbk", errors="ignore")).items():
        out[OVERSEAS.get(key, key)] = pct
    try:
        r = s.get(SINA_A50_URL, headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
        out["富时A50期货"] = parse_sina_a50(r.text)
    except Exception:
        pass
    return out
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_tencent.py -v`
Expected: 全部 passed

- [ ] **Step 5: 联网冒烟（不进测试集）**

Run:
```bash
.venv/Scripts/python -c "from jevq.data.tencent import *; from datetime import date; q=fetch_quotes(['600519','000001']); print({k:(v.price,v.pct) for k,v in q.items()}); print(fetch_overseas()); print(fetch_5d_returns(['600519','000001'], date.today()))"
```
Expected: 打印两只股票的价格和涨跌幅、6 个外围指数涨跌幅、两只股票的 5 日涨幅

- [ ] **Step 6: Commit**

```bash
git add jevq/data/tencent.py tests/test_tencent.py
git commit -m "feat: tencent quotes, kline and overseas indices"
```

---

### Task 6: AKShare 封装与公告库

**Files:**
- Create: `jevq/data/akdata.py`, `jevq/data/notice_store.py`
- Test: `tests/test_akdata.py`

**Interfaces:**
- Produces:
  - `akdata.trade_days() -> list[date]`
  - `akdata.filter_universe(rows: Iterable[tuple[str, str, date]], today: date, days: list[date]) -> dict[str, str]`
  - `akdata.load_universe(today, days) -> dict[str, str]`（code → name）
  - `akdata.Notice(id, code, title, type, date)`；`akdata.notices_from_frame(df) -> list[Notice]`；`akdata.fetch_notices_for_date(d: date) -> list[Notice]`
  - `akdata.News(title, content, published: datetime)`；`akdata.fetch_news(code) -> list[News]`
  - `akdata.summarize_pools(zt: int, dt: int, zb: int, max_board: int) -> dict`；`akdata.fetch_pool_stats(d: date) -> dict`
  - `notice_store.sync_notices(conn, today, fetch=fetch_notices_for_date, lookback=90) -> int`
  - `notice_store.notices_for(conn, code, today, lookback=90) -> list[Notice]`

已核实的 AKShare 列名（1.18.97）：
- `stock_info_sh_name_code(symbol="主板A股")`：`证券代码`、`证券简称`、`上市日期`
- `stock_info_sz_name_code(symbol="A股列表")`：`板块`、`A股代码`、`A股简称`、`A股上市日期`
- `stock_notice_report(symbol="全部", date="YYYYMMDD")`：`代码`、`名称`、`公告标题`、`公告类型`、`公告日期`、`网址`
- `stock_news_em(symbol=code)`：`新闻标题`、`新闻内容`、`发布时间`
- `stock_zt_pool_em(date=)` 含 `连板数`；`stock_zt_pool_dtgc_em`、`stock_zt_pool_zbgc_em`

- [ ] **Step 1: 写失败的测试**

`tests/test_akdata.py`:
```python
from datetime import date, timedelta

import pandas as pd

from jevq import db
from jevq.data.akdata import Notice, filter_universe, notices_from_frame, summarize_pools
from jevq.data.notice_store import notices_for, sync_notices

DAYS = [date(2026, 6, 1) + timedelta(days=i) for i in range(120)]


def test_filter_universe():
    today = DAYS[-1]
    rows = [
        ("600001", "老股份", date(2010, 1, 1)),
        ("000002", "*ST退市", date(2010, 1, 1)),
        ("300001", "创业板", date(2010, 1, 1)),
        ("688001", "科创板", date(2010, 1, 1)),
        ("600003", "次新股", DAYS[-59]),     # 含今天 59 个交易日
        ("600004", "够60天", DAYS[-60]),
    ]
    assert filter_universe(rows, today, DAYS) == {"600001": "老股份", "600004": "够60天"}


def test_notices_from_frame():
    df = pd.DataFrame([
        {"代码": "600001", "名称": "甲", "公告标题": "甲:关于股东减持计划的公告", "公告类型": "股东减持",
         "公告日期": "2026-09-22", "网址": "https://data.eastmoney.com/notices/detail/600001/AN202609221.html"},
        {"代码": "300001", "名称": "乙", "公告标题": "乙:年报", "公告类型": "年报",
         "公告日期": "2026-09-22", "网址": "https://x/AN202609222.html"},
    ])
    assert notices_from_frame(df) == [
        Notice("AN202609221", "600001", "甲:关于股东减持计划的公告", "股东减持", "2026-09-22")]


def test_summarize_pools():
    assert summarize_pools(60, 3, 20, 6) == {"涨停家数": 60, "跌停家数": 3, "炸板率%": 25.0, "最高连板": 6}
    assert summarize_pools(0, 0, 0, 0)["炸板率%"] == 0.0


def test_sync_and_query_notices():
    conn = db.connect(":memory:")
    today = date(2026, 9, 23)
    calls = []

    def fake_fetch(d):
        calls.append(d)
        return [Notice(f"AN{d:%Y%m%d}", "600001", f"公告{d}", "其他", d.isoformat())]

    assert sync_notices(conn, today, fetch=fake_fetch, lookback=3) == 4
    assert len(calls) == 4
    calls.clear()
    sync_notices(conn, today, fetch=fake_fetch, lookback=3)
    assert calls == [today]                           # 历史日期已拉过，只重拉今天
    got = notices_for(conn, "600001", today, lookback=2)
    assert [n.date for n in got] == ["2026-09-23", "2026-09-22", "2026-09-21"]
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_akdata.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现 akdata**

`jevq/data/akdata.py`:
```python
import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

import akshare as ak
import pandas as pd

from jevq.config import MIN_LISTED_TRADING_DAYS

MAIN_BOARD = ("60", "00")
NOTICE_ID = re.compile(r"(AN\d+)")


@dataclass(frozen=True)
class Notice:
    id: str
    code: str
    title: str
    type: str
    date: str


@dataclass(frozen=True)
class News:
    title: str
    content: str
    published: datetime


def trade_days() -> list[date]:
    df = ak.tool_trade_date_hist_sina()
    return sorted(pd.Timestamp(d).date() for d in df["trade_date"])


def filter_universe(rows: Iterable[tuple[str, str, date]], today: date,
                    days: list[date]) -> dict[str, str]:
    out = {}
    for code, name, listed in rows:
        if not code.startswith(MAIN_BOARD) or "ST" in name.upper():
            continue
        if bisect_right(days, today) - bisect_left(days, listed) < MIN_LISTED_TRADING_DAYS:
            continue
        out[code] = name
    return out


def load_universe(today: date, days: list[date]) -> dict[str, str]:
    sh = ak.stock_info_sh_name_code(symbol="主板A股")
    sz = ak.stock_info_sz_name_code(symbol="A股列表")
    sz = sz[sz["板块"] == "主板"]
    rows = [(str(r["证券代码"]).zfill(6), str(r["证券简称"]), pd.Timestamp(r["上市日期"]).date())
            for _, r in sh.iterrows()]
    rows += [(str(r["A股代码"]).zfill(6), str(r["A股简称"]).replace(" ", ""),
              pd.Timestamp(r["A股上市日期"]).date()) for _, r in sz.iterrows()]
    return filter_universe(rows, today, days)


def notices_from_frame(df: pd.DataFrame) -> list[Notice]:
    out = []
    for _, r in df.iterrows():
        code = str(r["代码"]).zfill(6)
        if not code.startswith(MAIN_BOARD):
            continue
        title = str(r["公告标题"])
        m = NOTICE_ID.search(str(r["网址"]))
        nid = m.group(1) if m else f"{code}:{title}"
        out.append(Notice(nid, code, title, str(r["公告类型"]), str(r["公告日期"])[:10]))
    return out


def fetch_notices_for_date(d: date) -> list[Notice]:
    return notices_from_frame(ak.stock_notice_report(symbol="全部", date=d.strftime("%Y%m%d")))


def fetch_news(code: str) -> list[News]:
    df = ak.stock_news_em(symbol=code)
    return [News(str(r["新闻标题"]), str(r["新闻内容"]),
                 pd.Timestamp(r["发布时间"]).to_pydatetime()) for _, r in df.iterrows()]


def summarize_pools(zt: int, dt: int, zb: int, max_board: int) -> dict:
    rate = round(zb / (zt + zb) * 100, 1) if zt + zb else 0.0
    return {"涨停家数": zt, "跌停家数": dt, "炸板率%": rate, "最高连板": max_board}


def fetch_pool_stats(d: date) -> dict:
    ds = d.strftime("%Y%m%d")
    zt = ak.stock_zt_pool_em(date=ds)
    dt = ak.stock_zt_pool_dtgc_em(date=ds)
    zb = ak.stock_zt_pool_zbgc_em(date=ds)
    max_board = int(zt["连板数"].max()) if len(zt) else 0
    return summarize_pools(len(zt), len(dt), len(zb), max_board)
```

- [ ] **Step 4: 实现 notice_store**

`jevq/data/notice_store.py`:
```python
import sqlite3
from collections.abc import Callable
from datetime import date, timedelta

from jevq.config import NOTICE_LOOKBACK_DAYS
from jevq.data.akdata import Notice, fetch_notices_for_date


def sync_notices(conn: sqlite3.Connection, today: date,
                 fetch: Callable[[date], list[Notice]] = fetch_notices_for_date,
                 lookback: int = NOTICE_LOOKBACK_DAYS) -> int:
    done = {r[0] for r in conn.execute("SELECT date FROM notice_fetch")}
    total = 0
    for i in range(lookback, -1, -1):
        d = today - timedelta(days=i)
        if d != today and d.isoformat() in done:
            continue
        try:
            items = fetch(d)
        except Exception as e:
            print(f"[notices] {d} 拉取失败: {e!r}")
            continue
        conn.executemany("INSERT OR IGNORE INTO notices(id, code, title, type, date) VALUES (?, ?, ?, ?, ?)",
                         [(n.id, n.code, n.title, n.type, n.date) for n in items])
        if d != today:
            conn.execute("INSERT OR IGNORE INTO notice_fetch(date) VALUES (?)", (d.isoformat(),))
        conn.commit()
        total += len(items)
    return total


def notices_for(conn: sqlite3.Connection, code: str, today: date,
                lookback: int = NOTICE_LOOKBACK_DAYS) -> list[Notice]:
    since = (today - timedelta(days=lookback)).isoformat()
    rows = conn.execute("SELECT id, code, title, type, date FROM notices "
                        "WHERE code=? AND date>=? ORDER BY date DESC, id", (code, since)).fetchall()
    return [Notice(*r) for r in rows]
```

- [ ] **Step 5: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_akdata.py -v`
Expected: 全部 passed

- [ ] **Step 6: 联网冒烟（不进测试集）**

Run:
```bash
.venv/Scripts/python -c "from jevq.data.akdata import *; from datetime import date, timedelta; days=trade_days(); u=load_universe(date.today(), days); print(len(u)); print(fetch_notices_for_date(date.today()-timedelta(days=1))[:2]); print(fetch_news('600519')[:1]); print(fetch_pool_stats(max(d for d in days if d < date.today())))"
```
Expected: 股票池 2800–3300 只；打印公告、新闻各一条；打印涨跌停统计 dict

- [ ] **Step 7: Commit**

```bash
git add jevq/data/akdata.py jevq/data/notice_store.py tests/test_akdata.py
git commit -m "feat: akshare wrappers and local notice store"
```

---

### Task 7: Jev 客户端

**Files:**
- Create: `jevq/judge/jev.py`
- Test: `tests/test_jev.py`

**Interfaces:**
- Produces:
  - `JevError(Exception)`
  - `JevClient(api_key: str, session=None, conn=None)`；`ask(state, questions: dict) -> dict`，返回 `answers`；429/529/5xx/网络异常重试 1 次，其他非 200 直接抛错；传了 `conn` 时每次调用写入 `jev_calls`
  - `score_label(answer: dict) -> str`：取概率最高的档位名称

- [ ] **Step 1: 写失败的测试**

`tests/test_jev.py`:
```python
import json

import pytest
import requests

from jevq import db
from jevq.judge.jev import JevClient, JevError, score_label


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


OK = Resp(200, {"model": "jev-1.13.0", "answers": {"q": {"type": "noul", "noul": 0.9}}})
Q = {"q": {"type": "noul", "instructions": "?"}}


def test_ask_sends_body_and_auth():
    s = FakeSession(OK)
    assert JevClient("k", session=s).ask("state", Q) == {"q": {"type": "noul", "noul": 0.9}}
    url, body, headers = s.calls[0]
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert body == {"state": "state", "model": "jev-latest", "questions": Q}
    assert headers["Authorization"] == "Bearer k"


@pytest.mark.parametrize("first", [Resp(529, {}), Resp(429, {}), requests.Timeout("t")])
def test_retries_once(first):
    s = FakeSession(first, OK)
    assert JevClient("k", session=s).ask("s", Q)["q"]["noul"] == 0.9
    assert len(s.calls) == 2


def test_gives_up_after_two_failures():
    with pytest.raises(JevError):
        JevClient("k", session=FakeSession(Resp(529, {}), Resp(529, {}))).ask("s", Q)


def test_no_retry_on_auth_error():
    s = FakeSession(Resp(401, {"error": "bad key"}), OK)
    with pytest.raises(JevError):
        JevClient("k", session=s).ask("s", Q)
    assert len(s.calls) == 1


def test_logs_calls():
    conn = db.connect(":memory:")
    JevClient("k", session=FakeSession(OK), conn=conn).ask("s", Q)
    req, resp, err = conn.execute("SELECT request, response, error FROM jev_calls").fetchone()
    assert json.loads(req)["state"] == "s" and "noul" in resp and err is None


def test_score_label():
    ans = {"type": "score", "score": 1.05, "legend": {"0": "偏空", "1": "中性", "2": "偏多"},
           "probabilities": {"0": 0.0, "1": 0.95, "2": 0.05}, "confidence": 0.92}
    assert score_label(ans) == "中性"
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_jev.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现**

`jevq/judge/jev.py`:
```python
import json
import sqlite3
import threading
from datetime import datetime

import requests

from jevq.config import JEV_MODEL, JEV_TIMEOUT, JEV_URL

RETRYABLE = {429, 529}
_LOG_LOCK = threading.Lock()


class JevError(Exception):
    pass


class JevClient:
    def __init__(self, api_key: str, session=None, conn: sqlite3.Connection | None = None):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.conn = conn

    def ask(self, state, questions: dict) -> dict:
        body = {"state": state, "model": JEV_MODEL, "questions": questions}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        error = "unknown"
        for _ in range(2):
            try:
                r = self.session.post(JEV_URL, json=body, headers=headers, timeout=JEV_TIMEOUT)
            except requests.RequestException as e:
                error = repr(e)
                continue
            if r.status_code == 200:
                answers = r.json()["answers"]
                self._log(body, answers, None)
                return answers
            error = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in RETRYABLE and r.status_code < 500:
                break
        self._log(body, None, error)
        raise JevError(error)

    def _log(self, body, answers, error):
        if self.conn is None:
            return
        with _LOG_LOCK:
            self.conn.execute("INSERT INTO jev_calls(ts, request, response, error) VALUES (?, ?, ?, ?)",
                              (datetime.now().isoformat(timespec="seconds"),
                               json.dumps(body, ensure_ascii=False),
                               None if answers is None else json.dumps(answers, ensure_ascii=False),
                               error))
            self.conn.commit()


def score_label(answer: dict) -> str:
    probs = answer["probabilities"]
    return answer["legend"][max(probs, key=probs.get)]
```

注意：`JevClient` 会被线程池并发调用（Task 8），`db.connect` 已设 `check_same_thread=False`，日志写入再用 `_LOG_LOCK` 串行化，避免多线程共用一个连接时出错。

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_jev.py -v`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add jevq/judge/jev.py tests/test_jev.py
git commit -m "feat: jev http client with retry and call log"
```

---

### Task 8: 公告排雷

**Files:**
- Create: `jevq/judge/notices.py`
- Test: `tests/test_notice_judge.py`

**Interfaces:**
- Consumes: `notice_store.notices_for`、`JevClient.ask`、`JevError`
- Produces:
  - `MINE_LABELS: set[str]`、`CRITERIA: dict[str, str]`
  - `classify_by_keyword(title: str) -> str | None`：标题命中雷词且不含「解除 / 完成」类安全词时返回雷的类别，否则返回 `None`（交给 Jev）
  - `NoticeJudge(conn, jev, workers=8)`；`check(code: str, today: date) -> tuple[str, str]`，verdict ∈ `pass / reject / error`

- [ ] **Step 1: 写失败的测试**

`tests/test_notice_judge.py`:
```python
from datetime import date

import pytest

from jevq import db
from jevq.data.akdata import Notice
from jevq.judge.jev import JevError
from jevq.judge.notices import NoticeJudge, classify_by_keyword

TODAY = date(2026, 9, 23)


@pytest.mark.parametrize("title,label", [
    ("甲:关于持股5%以上股东减持股份计划的预披露公告", "新增减持"),
    ("甲:关于控股股东部分股份质押的公告", "新增质押"),
    ("甲:关于回购公司股份的进展公告", "回购"),
    ("甲:关于公司涉及诉讼的公告", "诉讼仲裁"),
    ("甲:关于股东部分股份解除质押的公告", None),
    ("甲:关于股东减持计划实施完毕的公告", None),
    ("甲:关于控股股东权益变动的提示性公告", None),
    ("甲:2026年半年度报告", None),
])
def test_keyword(title, label):
    assert classify_by_keyword(title) == label


class FakeJev:
    def __init__(self, labels, confidence=0.9, fail=False):
        self.labels, self.confidence, self.fail, self.calls = labels, confidence, fail, 0

    def ask(self, state, questions):
        self.calls += 1
        if self.fail:
            raise JevError("down")
        label = self.labels[state["公告标题"]]
        return {"kind": {"type": "choice", "choice": label,
                         "probabilities": {label: self.confidence}, "confidence": self.confidence}}


def setup(*titles):
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO notices VALUES (?, ?, ?, ?, ?)",
                     [(f"AN{i}", "600001", t, "", "2026-09-01") for i, t in enumerate(titles)])
    return conn


def test_keyword_mine_rejects_without_jev():
    conn = setup("甲:关于股东减持计划的公告")
    jev = FakeJev({})
    verdict, why = NoticeJudge(conn, jev).check("600001", TODAY)
    assert verdict == "reject" and "减持" in why and jev.calls == 0


def test_jev_classifies_ambiguous_and_caches():
    title = "甲:关于控股股东权益变动的提示性公告"
    conn = setup(title)
    jev = FakeJev({title: "新增减持"})
    judge = NoticeJudge(conn, jev)
    assert judge.check("600001", TODAY)[0] == "reject"
    assert judge.check("600001", TODAY)[0] == "reject"
    assert jev.calls == 1                                  # 第二次走缓存


def test_clean_and_loss_ignored():
    titles = ["甲:2026年半年度报告", "甲:2026年半年度业绩预亏公告"]
    conn = setup(*titles)
    jev = FakeJev({titles[0]: "其他", titles[1]: "亏损"})
    assert NoticeJudge(conn, jev).check("600001", TODAY) == ("pass", "公告无雷")


def test_low_confidence_counts_as_mine():
    title = "甲:关于重大事项的公告"
    conn = setup(title)
    verdict, why = NoticeJudge(conn, FakeJev({title: "其他"}, confidence=0.5)).check("600001", TODAY)
    assert verdict == "reject" and "拿不准" in why


def test_jev_failure_is_error_and_not_cached():
    title = "甲:关于重大事项的公告"
    conn = setup(title)
    assert NoticeJudge(conn, FakeJev({}, fail=True)).check("600001", TODAY)[0] == "error"
    assert conn.execute("SELECT COUNT(*) FROM notice_verdicts").fetchone()[0] == 0


def test_no_notices_passes():
    assert NoticeJudge(db.connect(":memory:"), FakeJev({})).check("600001", TODAY)[0] == "pass"
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_notice_judge.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现**

`jevq/judge/notices.py`:
```python
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from jevq.config import LOW_CONFIDENCE
from jevq.data.notice_store import notices_for
from jevq.judge.jev import JevError

MINE_LABELS = {"新增减持", "新增质押", "回购", "诉讼仲裁"}

CRITERIA = {
    "新增减持": "股东、董监高的减持计划、减持进展、被动减持、权益变动导致持股减少",
    "新增质押": "股东新增股份质押、补充质押、质押展期",
    "回购": "公司股份回购方案、回购进展、回购结果",
    "诉讼仲裁": "公司涉及诉讼、仲裁、被起诉、判决、立案调查",
    "亏损": "业绩预亏、净利润亏损、业绩预告下修",
    "解除质押或减持完成": "解除质押、减持计划实施完毕、减持期限届满、终止减持",
    "其他": "与以上各类都无关的公告",
}

MINE_KEYWORDS = {"新增减持": ["减持"], "新增质押": ["质押"], "回购": ["回购"],
                 "诉讼仲裁": ["诉讼", "仲裁", "起诉"]}
SAFE_KEYWORDS = ["解除", "解押", "实施完毕", "期限届满", "终止", "完成"]


def classify_by_keyword(title: str) -> str | None:
    if any(k in title for k in SAFE_KEYWORDS):
        return None
    for label, words in MINE_KEYWORDS.items():
        if any(w in title for w in words):
            return label
    return None


class NoticeJudge:
    def __init__(self, conn: sqlite3.Connection, jev, workers: int = 8):
        self.conn, self.jev, self.workers = conn, jev, workers

    def check(self, code: str, today: date) -> tuple[str, str]:
        notices = notices_for(self.conn, code, today)
        verdicts: dict[str, tuple[str, float]] = {}
        pending = []
        for n in notices:
            cached = self.conn.execute("SELECT label, confidence FROM notice_verdicts WHERE id=?",
                                       (n.id,)).fetchone()
            if cached:
                verdicts[n.id] = cached
            elif (label := classify_by_keyword(n.title)) is not None:
                verdicts[n.id] = (label, 1.0)
                self._save(n.id, label, 1.0, "keyword")
            else:
                pending.append(n)

        if pending:
            with ThreadPoolExecutor(self.workers) as pool:
                results = list(pool.map(self._ask, pending))
            if any(r is None for r in results):
                return "error", "Jev 不可用"
            for n, (label, conf) in zip(pending, results):
                verdicts[n.id] = (label, conf)
                self._save(n.id, label, conf, "jev")

        for n in notices:
            label, conf = verdicts[n.id]
            if label in MINE_LABELS:
                return "reject", f"{label}：{n.date} {n.title}"
            if conf < LOW_CONFIDENCE:
                return "reject", f"拿不准({conf:.2f})：{n.date} {n.title}"
        return "pass", "公告无雷"

    def _ask(self, n) -> tuple[str, float] | None:
        try:
            ans = self.jev.ask({"公告标题": n.title, "公告类型": n.type},
                               {"kind": {"type": "choice",
                                         "instructions": "这条A股上市公司公告属于哪一类？",
                                         "criteria": CRITERIA}})["kind"]
        except JevError:
            return None
        return ans["choice"], ans["confidence"]

    def _save(self, nid: str, label: str, conf: float, source: str):
        self.conn.execute("INSERT OR REPLACE INTO notice_verdicts VALUES (?, ?, ?, ?)",
                          (nid, label, conf, source))
        self.conn.commit()
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_notice_judge.py -v`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add jevq/judge/notices.py tests/test_notice_judge.py
git commit -m "feat: notice mine detection with keyword prefilter and jev"
```

---

### Task 9: 消息面与市场判断

**Files:**
- Create: `jevq/judge/news.py`, `jevq/judge/market.py`
- Test: `tests/test_news_market.py`

**Interfaces:**
- Consumes: `JevClient.ask`、`JevError`、`score_label`、`akdata.News`、`akdata.fetch_news`
- Produces:
  - `NewsJudge(conn, jev, fetch=fetch_news)`；`check(code, name, now: datetime) -> tuple[str, str]`，结果写入 `day_cache(kind="news")`，同一天只判一次
  - `MarketJudge(jev)`；`sentiment(stats: dict) -> str | None`（冰点 / 偏弱 / 正常 / 火热）；`overseas(pcts: dict) -> str | None`（偏弱 / 中性 / 偏强）；Jev 失败或输入为空时返回 `None`

- [ ] **Step 1: 写失败的测试**

`tests/test_news_market.py`:
```python
from datetime import datetime, timedelta

from jevq import db
from jevq.data.akdata import News
from jevq.judge.jev import JevError
from jevq.judge.market import MarketJudge
from jevq.judge.news import NewsJudge

NOW = datetime(2026, 9, 24, 10, 0)


class ScoreJev:
    def __init__(self, label, fail=False):
        self.label, self.fail, self.calls, self.states = label, fail, 0, []

    def ask(self, state, questions):
        self.calls += 1
        self.states.append(state)
        if self.fail:
            raise JevError("down")
        (key, q), = questions.items()
        legend = {str(i): v for i, v in enumerate(q["criteria"])}
        idx = q["criteria"].index(self.label)
        probs = {k: (0.9 if int(k) == idx else 0.1 / (len(legend) - 1)) for k in legend}
        return {key: {"type": "score", "score": idx, "legend": legend,
                      "probabilities": probs, "confidence": 0.8}}


def news(hours_ago, title="标题"):
    return News(title, "内容" * 80, NOW - timedelta(hours=hours_ago))


def test_bearish_news_rejects_and_caches():
    conn = db.connect(":memory:")
    jev = ScoreJev("偏空")
    judge = NewsJudge(conn, jev, fetch=lambda code: [news(2, "利空标题")])
    assert judge.check("600001", "甲", NOW) == ("reject", "消息面偏空")
    assert judge.check("600001", "甲", NOW)[0] == "reject"
    assert jev.calls == 1
    assert len(jev.states[0]["近24小时新闻"][0]) <= 130   # 标题 + 截断内容


def test_old_news_ignored_counts_as_neutral():
    jev = ScoreJev("偏空")
    judge = NewsJudge(db.connect(":memory:"), jev, fetch=lambda code: [news(30)])
    assert judge.check("600001", "甲", NOW) == ("pass", "消息面中性")
    assert jev.calls == 0


def test_news_errors():
    judge = NewsJudge(db.connect(":memory:"), ScoreJev("中性", fail=True), fetch=lambda c: [news(1)])
    assert judge.check("600001", "甲", NOW)[0] == "error"

    def boom(code):
        raise RuntimeError("akshare down")
    judge = NewsJudge(db.connect(":memory:"), ScoreJev("中性"), fetch=boom)
    assert judge.check("600001", "甲", NOW)[0] == "error"


def test_market_judge():
    assert MarketJudge(ScoreJev("火热")).sentiment({"涨停家数": 80}) == "火热"
    assert MarketJudge(ScoreJev("偏弱")).overseas({"道琼斯": -1.2}) == "偏弱"
    assert MarketJudge(ScoreJev("正常", fail=True)).sentiment({"涨停家数": 1}) is None
    assert MarketJudge(ScoreJev("中性")).overseas({}) is None
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_news_market.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现 NewsJudge**

`jevq/judge/news.py`:
```python
import sqlite3
from datetime import datetime, timedelta

from jevq.config import NEWS_LOOKBACK_HOURS
from jevq.data.akdata import fetch_news
from jevq.judge.jev import JevError, score_label

LEVELS = ["偏空", "中性", "偏多"]


class NewsJudge:
    def __init__(self, conn: sqlite3.Connection, jev, fetch=fetch_news):
        self.conn, self.jev, self.fetch = conn, jev, fetch

    def check(self, code: str, name: str, now: datetime) -> tuple[str, str]:
        day = now.date().isoformat()
        row = self.conn.execute("SELECT value FROM day_cache WHERE date=? AND kind='news' AND key=?",
                                (day, code)).fetchone()
        label = row[0] if row else self._judge(code, name, now)
        if label is None:
            return "error", "消息面判断失败"
        if not row:
            self.conn.execute("INSERT OR REPLACE INTO day_cache VALUES (?, 'news', ?, ?)", (day, code, label))
            self.conn.commit()
        return ("reject" if label == "偏空" else "pass"), f"消息面{label}"

    def _judge(self, code: str, name: str, now: datetime) -> str | None:
        try:
            items = [n for n in self.fetch(code)
                     if now - n.published <= timedelta(hours=NEWS_LOOKBACK_HOURS)]
        except Exception:
            return None
        if not items:
            return "中性"
        state = {"股票": f"{name}({code})",
                 "近24小时新闻": [f"{n.title}｜{n.content[:100]}" for n in items[:10]]}
        try:
            ans = self.jev.ask(state, {"sentiment": {
                "type": "score",
                "instructions": "这些新闻对该股票未来一两个交易日股价的影响偏向哪一边？",
                "criteria": LEVELS}})["sentiment"]
        except JevError:
            return None
        return score_label(ans)
```

注意：`n.title` 最长约 30 字，内容截到 100 字，所以测试断言单条不超过 130 字。

- [ ] **Step 4: 实现 MarketJudge**

`jevq/judge/market.py`:
```python
from jevq.judge.jev import JevError, score_label

SENTIMENT_LEVELS = ["冰点", "偏弱", "正常", "火热"]
OVERSEAS_LEVELS = ["偏弱", "中性", "偏强"]


class MarketJudge:
    def __init__(self, jev):
        self.jev = jev

    def sentiment(self, stats: dict) -> str | None:
        return self._score(stats, "A股短线情绪当前处于什么状态？（参考涨跌停家数、炸板率、连板高度、成交额和涨跌家数）",
                           SENTIMENT_LEVELS)

    def overseas(self, pcts: dict) -> str | None:
        return self._score(pcts, "这些隔夜外围市场涨跌幅（%）对今天A股开盘的影响偏向？", OVERSEAS_LEVELS)

    def _score(self, state: dict, instructions: str, levels: list[str]) -> str | None:
        if not state:
            return None
        try:
            ans = self.jev.ask(state, {"level": {"type": "score", "instructions": instructions,
                                                 "criteria": levels}})["level"]
        except JevError:
            return None
        return score_label(ans)
```

- [ ] **Step 5: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_news_market.py -v`
Expected: 全部 passed

- [ ] **Step 6: Commit**

```bash
git add jevq/judge/news.py jevq/judge/market.py tests/test_news_market.py
git commit -m "feat: news and market judges"
```

---

### Task 10: 引擎（每分钟一轮）

**Files:**
- Create: `jevq/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `Ledger`、`decide_sell`、`buy_mode`、`buy_window_open`、`rank_candidates`、`NoticeJudge.check`、`NewsJudge.check`、`MarketJudge.sentiment`、`akdata.fetch_pool_stats`
- Produces:
  - `Engine(conn, ledger, notice_judge, news_judge, market_judge, fetch_quotes, fetch_pools, universe: dict[str, str], ret5: dict[str, float], prev_day: date, overseas: str | None)`
  - `Engine.tick(now: datetime) -> list[Fill]`
  - 属性 `last_quotes: dict[str, Quote]`、`sentiment_label: str | None`、`halted: bool`（供 report / run 使用）

- [ ] **Step 1: 写失败的测试**

`tests/test_engine.py`:
```python
from datetime import date, datetime

import pytest

from factories import quote
from jevq import db
from jevq.engine import Engine
from jevq.ledger import Ledger

DAY = datetime(2026, 9, 23)


def at(h, m, d=DAY):
    return d.replace(hour=h, minute=m)


class Judge:
    def __init__(self, verdicts=None):
        self.verdicts, self.calls = verdicts or {}, []

    def check(self, code, *args):
        self.calls.append(code)
        return self.verdicts.get(code, ("pass", "ok"))


class Market:
    def __init__(self, label):
        self.label = label

    def sentiment(self, stats):
        return self.label


def make(quotes, notice=None, news=None, sentiment="正常", overseas="中性", ret5=None):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    feed = {"quotes": quotes}

    def fetch(codes):
        q = feed["quotes"]
        if isinstance(q, Exception):
            raise q
        return {c: x for c, x in q.items() if c in codes}

    universe = {c: "x" for c in ["600001", "600002", "600003", "600004"]}
    ret5 = ret5 or {"600001": 5.0, "600002": 15.0, "600003": 10.0, "600004": 1.0}
    eng = Engine(conn, ledger, notice or Judge(), news or Judge(), Market(sentiment),
                 fetch, lambda d: {"涨停家数": 50}, universe, ret5, date(2026, 9, 22), overseas)
    return eng, ledger, feed


def qs(**pcts):
    return {c: quote(c, pct=p) for c, p in pcts.items()}


def test_main_skips_rejected_shadow_buys_top_ranked():
    quotes = qs(**{"600001": -1.0, "600002": -2.0, "600003": -0.5, "600004": 2.0})
    notice = Judge({"600002": ("reject", "新增减持")})
    eng, ledger, _ = make(quotes, notice=notice)
    eng.tick(at(10, 0))
    assert [p.code for p in ledger.positions("shadow")] == ["600002", "600003"]
    assert sorted(p.code for p in ledger.positions("main")) == ["600001", "600003"]
    rows = eng.conn.execute("SELECT code, verdict FROM decisions").fetchall()
    assert ("600002", "reject") in rows


def test_rejected_not_rechecked_same_day():
    notice = Judge({"600002": ("reject", "x")})
    eng, _, _ = make(qs(**{"600002": -1.0}), notice=notice)
    eng.tick(at(10, 0))
    eng.tick(at(10, 1))
    assert notice.calls == ["600002"]


def test_error_verdict_retried_next_tick():
    notice = Judge({"600002": ("error", "Jev 不可用")})
    eng, ledger, _ = make(qs(**{"600002": -1.0}), notice=notice)
    eng.tick(at(10, 0))
    notice.verdicts = {}
    eng.tick(at(10, 1))
    assert notice.calls == ["600002", "600002"]
    assert ledger.holds("main", "600002")


def test_news_checked_only_after_notice_passes():
    notice = Judge({"600002": ("reject", "x")})
    news = Judge({"600003": ("reject", "消息面偏空")})
    eng, ledger, _ = make(qs(**{"600002": -1.0, "600003": -1.0}), notice=notice, news=news)
    eng.tick(at(10, 0))
    assert news.calls == ["600003"]
    assert ledger.positions("main") == []


@pytest.mark.parametrize("sentiment,overseas", [("冰点", "中性"), ("正常", "偏弱")])
def test_tail_mode_waits_until_1450(sentiment, overseas):
    eng, ledger, _ = make(qs(**{"600001": -1.0}), sentiment=sentiment, overseas=overseas)
    eng.tick(at(10, 0))
    assert ledger.positions("main") == []
    eng.tick(at(14, 50))
    assert ledger.holds("main", "600001")


def test_next_day_sell_on_high_open():
    eng, ledger, feed = make(qs(**{"600001": -1.0}))
    eng.tick(at(10, 0))
    feed["quotes"] = {"600001": quote("600001", pct=1.5, open_pct=1.5)}
    fills = eng.tick(at(9, 31, datetime(2026, 9, 24)))
    assert {(f.account, f.side, f.reason) for f in fills} == {
        ("main", "sell", "高开止盈"), ("shadow", "sell", "高开止盈")}


def test_three_fetch_failures_halt_new_buys():
    eng, ledger, feed = make(RuntimeError("down"))
    for m in range(3):
        assert eng.tick(at(10, m)) == []
    assert eng.halted
    feed["quotes"] = qs(**{"600001": -1.0})
    eng.tick(at(10, 5))
    assert ledger.positions("main") == [] and ledger.positions("shadow") == []


def test_turnover_ratio_vs_previous_day_same_minute():
    eng, _, _ = make(qs(**{"600001": -1.0}))
    eng.conn.execute("INSERT INTO turnover VALUES ('2026-09-22 10:00', 5000000)")
    stats = eng._market_stats(at(10, 0), eng._fetch(at(10, 0)))
    assert stats["成交额较昨日同期%"] == 100.0          # 1e7 vs 5e6
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现**

`jevq/engine.py`:
```python
import json
import sqlite3
from datetime import date, datetime, timedelta

from jevq.config import ACCOUNTS, MAX_CHECKS_PER_TICK, SENTIMENT_REFRESH_MIN
from jevq.ledger import Ledger
from jevq.models import Fill, Quote
from jevq.rules.buy import buy_mode, buy_window_open, rank_candidates
from jevq.rules.sell import decide_sell


class Engine:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger, notice_judge, news_judge,
                 market_judge, fetch_quotes, fetch_pools, universe: dict[str, str],
                 ret5: dict[str, float], prev_day: date, overseas: str | None):
        self.conn, self.ledger = conn, ledger
        self.notice_judge, self.news_judge, self.market_judge = notice_judge, news_judge, market_judge
        self.fetch_quotes, self.fetch_pools = fetch_quotes, fetch_pools
        self.universe, self.ret5, self.prev_day, self.overseas = universe, ret5, prev_day, overseas
        self.rejected: set[str] = set()
        self.fail_streak = 0
        self.halted = False
        self.last_quotes: dict[str, Quote] = {}
        self.sentiment_label: str | None = None
        self._sentiment_at: datetime | None = None

    def tick(self, now: datetime) -> list[Fill]:
        quotes = self._fetch(now)
        if quotes is None:
            return []
        fills = self._sells(quotes, now)
        mode = buy_mode(self._sentiment(now, quotes), self.overseas)
        if self.halted or not buy_window_open(mode, now.time()):
            return fills
        ranked = rank_candidates((q for c, q in quotes.items() if c in self.universe), self.ret5)
        fills += self._buy_shadow(ranked, now)
        fills += self._buy_main(ranked, now)
        return fills

    def _fetch(self, now: datetime) -> dict[str, Quote] | None:
        held = {p.code for a in ACCOUNTS for p in self.ledger.positions(a)}
        try:
            quotes = self.fetch_quotes(sorted(set(self.universe) | held))
        except Exception as e:
            self.fail_streak += 1
            print(f"[engine] {now:%H:%M} 行情失败({self.fail_streak}): {e!r}")
            if self.fail_streak >= 3:
                self.halted = True
            return None
        self.fail_streak = 0
        self.last_quotes = quotes
        total = sum(q.amount for c, q in quotes.items() if c in self.universe)
        self.conn.execute("INSERT OR REPLACE INTO turnover VALUES (?, ?)",
                          (now.strftime("%Y-%m-%d %H:%M"), total))
        self.conn.commit()
        return quotes

    def _sells(self, quotes: dict[str, Quote], now: datetime) -> list[Fill]:
        fills = []
        for account in ACCOUNTS:
            for pos in self.ledger.positions(account):
                q = quotes.get(pos.code)
                if q is None:
                    continue
                d = decide_sell(pos, q, now)
                if d.sell and (f := self.ledger.sell(account, pos, q, now, d.reason)):
                    fills.append(f)
        return fills

    def _sentiment(self, now: datetime, quotes: dict[str, Quote]) -> str | None:
        fresh = self._sentiment_at and now - self._sentiment_at < timedelta(minutes=SENTIMENT_REFRESH_MIN)
        if not fresh:
            stats = self._market_stats(now, quotes)
            self.sentiment_label = self.market_judge.sentiment(stats) if stats else None
            self._sentiment_at = now
            self._log(now, "", "sentiment", json.dumps(
                {"stats": stats, "label": self.sentiment_label}, ensure_ascii=False))
        return self.sentiment_label

    def _market_stats(self, now: datetime, quotes: dict[str, Quote]) -> dict:
        try:
            stats = dict(self.fetch_pools(now.date()))
        except Exception as e:
            print(f"[engine] 涨跌停池失败: {e!r}")
            return {}
        uni = [q for c, q in quotes.items() if c in self.universe]
        stats["上涨家数"] = sum(q.pct > 0 for q in uni)
        stats["下跌家数"] = sum(q.pct < 0 for q in uni)
        prev = self.conn.execute("SELECT amount FROM turnover WHERE ts=?",
                                 (f"{self.prev_day.isoformat()} {now:%H:%M}",)).fetchone()
        if prev and prev[0] > 0:
            cur = sum(q.amount for q in uni)
            stats["成交额较昨日同期%"] = round((cur / prev[0] - 1) * 100, 1)
        return stats

    def _buy_shadow(self, ranked: list[Quote], now: datetime) -> list[Fill]:
        fills = []
        for q in ranked:
            if self.ledger.free_slots("shadow") <= 0:
                break
            if f := self.ledger.buy("shadow", q, now, f"影子｜低吸{q.pct}%｜5日{self.ret5[q.code]}%"):
                fills.append(f)
        return fills

    def _buy_main(self, ranked: list[Quote], now: datetime) -> list[Fill]:
        fills, checked = [], 0
        for q in ranked:
            if self.ledger.free_slots("main") <= 0 or checked >= MAX_CHECKS_PER_TICK:
                break
            if q.code in self.rejected or self.ledger.holds("main", q.code):
                continue
            checked += 1
            verdict, why = self.notice_judge.check(q.code, now.date())
            if verdict == "pass":
                verdict, why = self.news_judge.check(q.code, q.name, now)
            self._log(now, q.code, verdict, why)
            if verdict == "reject":
                self.rejected.add(q.code)
                continue
            if verdict == "error":
                continue
            if f := self.ledger.buy("main", q, now, f"低吸{q.pct}%｜5日{self.ret5[q.code]}%｜{why}"):
                fills.append(f)
        return fills

    def _log(self, now: datetime, code: str, verdict: str, detail: str):
        self.conn.execute("INSERT INTO decisions(ts, code, verdict, detail) VALUES (?, ?, ?, ?)",
                          (now.isoformat(timespec="seconds"), code, verdict, detail))
        self.conn.commit()
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -v`
Expected: 全部 passed

- [ ] **Step 5: 全量回归**

Run: `.venv/Scripts/python -m pytest -q`
Expected: 全部 passed

- [ ] **Step 6: Commit**

```bash
git add jevq/engine.py tests/test_engine.py
git commit -m "feat: per-minute engine with main and shadow accounts"
```

---

### Task 11: 复盘报告与每日调度

**Files:**
- Create: `jevq/report.py`, `run.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `Ledger.equity`、`Ledger.fills_on`、`decisions` 表、`Engine` 及其依赖
- Produces:
  - `write_report(conn, ledger, day: date, prices: dict[str, float], overseas: str | None, sentiment: str | None, out_dir=REPORT_DIR) -> Path`：写 `equity` 表并生成 `reports/YYYY-MM-DD.md`
  - `run.py`：`run_day(once=False, stop: threading.Event | None = None, log=print, state: dict | None = None) -> Path | None`（Task 13 的桌面面板在线程里调用它）；`python run.py` 跑完整个交易日；`python run.py --once` 立即跑一轮，用于冒烟测试

- [ ] **Step 1: 写失败的报告测试**

`tests/test_report.py`:
```python
from datetime import date, datetime

from factories import quote
from jevq import db
from jevq.ledger import Ledger
from jevq.report import write_report


def test_report(tmp_path):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    t = datetime(2026, 9, 23, 14, 50)
    ledger.buy("main", quote("600001", pct=-1.0), t, "低吸-1.0%")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail) VALUES (?, ?, ?, ?)",
                 (t.isoformat(), "600002", "reject", "新增减持：2026-09-01 甲:减持公告"))
    path = write_report(conn, ledger, date(2026, 9, 23), {"600001": 10.0}, "中性", "偏弱", tmp_path)
    text = path.read_text(encoding="utf-8")
    assert path.name == "2026-09-23.md"
    assert "600001" in text and "低吸-1.0%" in text
    assert "600002" in text and "新增减持" in text
    assert "外围：中性" in text and "情绪：偏弱" in text
    assert conn.execute("SELECT COUNT(*) FROM equity").fetchone()[0] == 2
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现报告**

`jevq/report.py`:
```python
import sqlite3
from datetime import date
from pathlib import Path

from jevq.config import ACCOUNTS, INITIAL_CASH, REPORT_DIR
from jevq.ledger import Ledger

NAMES = {"main": "主账户(Jev)", "shadow": "影子账户"}


def write_report(conn: sqlite3.Connection, ledger: Ledger, day: date, prices: dict[str, float],
                 overseas: str | None, sentiment: str | None, out_dir=REPORT_DIR) -> Path:
    fills = ledger.fills_on(day)
    lines = [f"# 复盘 {day.isoformat()}", "",
             f"外围：{overseas or '未知'}　情绪：{sentiment or '未知'}", "",
             "## 净值", "", "| 账户 | 净值 | 累计收益 | 今日费用 | 持仓 |", "|---|---|---|---|---|"]
    for a in ACCOUNTS:
        value = ledger.equity(a, prices)
        conn.execute("INSERT OR REPLACE INTO equity VALUES (?, ?, ?)", (day.isoformat(), a, value))
        fee = sum(f.fee for f in fills if f.account == a)
        held = "、".join(p.code for p in ledger.positions(a)) or "空仓"
        lines.append(f"| {NAMES[a]} | {value:,.2f} | {(value / INITIAL_CASH - 1) * 100:+.2f}% "
                     f"| {fee:.2f} | {held} |")
    conn.commit()

    lines += ["", "## 成交", "", "| 时间 | 账户 | 代码 | 方向 | 价格 | 股数 | 费用 | 理由 |",
              "|---|---|---|---|---|---|---|---|"]
    for f in fills:
        side = "买" if f.side == "buy" else "卖"
        lines.append(f"| {f.ts:%H:%M} | {NAMES[f.account]} | {f.code} | {side} | {f.price:.2f} "
                     f"| {f.shares} | {f.fee:.2f} | {f.reason} |")

    rows = conn.execute("SELECT ts, code, verdict, detail FROM decisions "
                        "WHERE substr(ts, 1, 10)=? AND code != '' ORDER BY id",
                        (day.isoformat(),)).fetchall()
    lines += ["", "## 主账户检查记录", "", "| 时间 | 代码 | 结果 | 原因 |", "|---|---|---|---|"]
    lines += [f"| {ts[11:16]} | {code} | {verdict} | {detail} |" for ts, code, verdict, detail in rows]
    lines += ["", "> 成交价取分钟快照，开盘附近会比实盘略好。"]

    path = Path(out_dir) / f"{day.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`
Expected: passed

- [ ] **Step 5: 写调度入口**

`run.py`:
```python
import argparse
import os
import sys
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

from jevq import db
from jevq.config import ACCOUNTS, DB_PATH
from jevq.data import akdata, tencent
from jevq.data.notice_store import sync_notices
from jevq.engine import Engine
from jevq.judge.jev import JevClient
from jevq.judge.market import MarketJudge
from jevq.judge.news import NewsJudge
from jevq.judge.notices import NoticeJudge
from jevq.ledger import Ledger
from jevq.report import write_report

SESSIONS = [(time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))]
REPORT_AT = time(15, 5)
NOTICE_RESYNC_AT = time(13, 0)


def in_session(t: time) -> bool:
    return any(a <= t < b for a, b in SESSIONS)


def wait_until(t: time, stop: threading.Event) -> bool:
    """等到时刻 t；被 stop 打断返回 False。"""
    while datetime.now().time() < t:
        if stop.wait(5):
            return False
    return True


def seconds_to_next_minute() -> float:
    now = datetime.now()
    nxt = (now + timedelta(minutes=1)).replace(second=2, microsecond=0)
    return max(1.0, (nxt - now).total_seconds())


def run_day(once: bool = False, stop: threading.Event | None = None,
            log: Callable[[str], None] = print, state: dict | None = None) -> Path | None:
    """跑一个交易日。stop 被置位时尽快退出（不写报告）；state 供桌面面板读取运行状态。"""
    stop = stop or threading.Event()
    state = state if state is not None else {}
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("缺少环境变量 TYPESAFE_API_KEY")

    today = date.today()
    days = akdata.trade_days()
    if today not in days and not once:
        log(f"{today} 非交易日，退出")
        return None
    prev_day = max(d for d in days if d < today)

    conn = db.connect(DB_PATH)
    ledger = Ledger(conn)
    jev = JevClient(key, conn=conn)
    market = MarketJudge(jev)

    state["phase"] = "准备股票池"
    universe = akdata.load_universe(today, days)
    log(f"股票池 {len(universe)} 只")
    ret5 = tencent.fetch_5d_returns(sorted(universe), today)
    log(f"5 日涨幅 {len(ret5)} 只")

    state["phase"] = "等待 09:15"
    if not once and not wait_until(time(9, 15), stop):
        return None
    try:
        overseas_pcts = tencent.fetch_overseas()
    except Exception as e:
        log(f"外围失败: {e!r}")
        overseas_pcts = {}
    overseas = market.overseas(overseas_pcts)
    state["overseas"] = overseas
    log(f"外围 {overseas_pcts} → {overseas}")

    state["phase"] = "同步公告"
    log(f"公告同步 {sync_notices(conn, today)} 条")

    engine = Engine(conn, ledger, NoticeJudge(conn, jev), NewsJudge(conn, jev), market,
                    tencent.fetch_quotes, akdata.fetch_pool_stats, universe, ret5, prev_day, overseas)

    if once:
        fills = engine.tick(datetime.now())
        state.update(sentiment=engine.sentiment_label, prices={c: q.price for c, q in engine.last_quotes.items()})
        log(f"情绪 {engine.sentiment_label}，成交 {fills}")
        return None

    resynced = False
    while datetime.now().time() < REPORT_AT:
        now = datetime.now()
        if not resynced and now.time() >= NOTICE_RESYNC_AT:
            sync_notices(conn, today)
            resynced = True
        if in_session(now.time()):
            state["phase"] = "盘中"
            for f in engine.tick(now):
                log(f"{f.ts:%H:%M} {f.account} {'买' if f.side == 'buy' else '卖'} {f.code} "
                    f"{f.price} x{f.shares} {f.reason}")
            state.update(sentiment=engine.sentiment_label,
                         prices={c: q.price for c, q in engine.last_quotes.items()})
        else:
            state["phase"] = "休市等待"
        if stop.wait(seconds_to_next_minute()):
            log("收到停止指令，未生成当日报告")
            return None

    prices = {c: q.price for c, q in engine.last_quotes.items()}
    path = write_report(conn, ledger, today, prices, overseas, engine.sentiment_label)
    state["phase"] = "已收盘"
    log(f"复盘报告 {path}；" + "，".join(f"{a} {ledger.equity(a, prices):,.2f}" for a in ACCOUNTS))
    return path


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="立即跑一轮后退出（冒烟测试）")
    args = p.parse_args(argv)
    try:
        run_day(once=args.once)
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
```

`run.bat`（手动启动或供计划任务调用）:
```bat
@echo off
cd /d %~dp0
.venv\Scripts\python run.py >> data\run-%date:~0,4%%date:~5,2%%date:~8,2%.log 2>&1
```

- [ ] **Step 6: 全量回归**

Run: `.venv/Scripts/python -m pytest -q`
Expected: 全部 passed

- [ ] **Step 7: Commit**

```bash
git add jevq/report.py run.py run.bat tests/test_report.py
git commit -m "feat: daily report and scheduler entrypoint"
```

---

### Task 13: 桌面客户端（独立窗口面板）

在 Task 11 之后、Task 12 之前执行。

**Files:**
- Create: `jevq/app/__init__.py`, `jevq/app/api.py`, `jevq/app/ui.html`, `desktop.py`, `install_shortcut.py`
- Modify: `requirements.txt`（加 `pywebview`）
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `run.run_day(once, stop, log, state)`、`Ledger`、`db.connect`、`tencent.fetch_quotes`、表 `decisions` / `equity`、`reports/*.md`
- Produces:
  - `Api(db_path=DB_PATH, report_dir=REPORT_DIR, runner=None, fetch_quotes=tencent.fetch_quotes)`，暴露给前端的方法：`start() -> dict`、`stop() -> dict`、`is_running() -> bool`、`status() -> dict`、`fills(day: str) -> list[dict]`、`decisions(day: str) -> list[dict]`、`equity_curve() -> dict[str, list]`、`reports() -> list[str]`、`report(name: str) -> str`、`logs() -> list[str]`
  - `desktop.py`：`pythonw desktop.py` 打开窗口
  - `install_shortcut.py`：在桌面创建「Jev低吸模拟盘」快捷方式

设计要点：
- 窗口用 pywebview（Windows 下走 WebView2，本机已装 149.x），前端是本地 `ui.html`，通过 `window.pywebview.api.*` 直接调用 Python，不起 HTTP 服务。
- 交易循环跑在 `Api` 的后台线程里；「停止」置位 `threading.Event`，`run_day` 在一分钟内退出。关窗口时也会停止。
- 面板每次查询都新开 SQLite 连接，只读取数据；交易线程用自己的连接写入。`db.connect` 设了 `timeout=15`，读写冲突时会等锁，不会直接报错。
- `pythonw` 没有控制台，`sys.stdout/stderr` 是 `None`，akshare 的进度条一写就会崩，所以 `desktop.py` 启动时先重定向到 `os.devnull`。

- [ ] **Step 1: 安装依赖**

`requirements.txt` 末尾加一行：
```
pywebview
```
Run: `.venv/Scripts/python -m pip install -q pywebview`

- [ ] **Step 2: 写失败的测试**

`tests/test_api.py`:
```python
import threading
from datetime import datetime

import pytest

from factories import quote
from jevq import db
from jevq.app.api import Api
from jevq.ledger import Ledger

T = datetime(2026, 9, 23, 14, 50)


@pytest.fixture
def paths(tmp_path):
    return str(tmp_path / "t.sqlite"), tmp_path / "reports"


def make_api(paths, runner=None, fetch=None):
    return Api(db_path=paths[0], report_dir=str(paths[1]),
               runner=runner or (lambda **kw: None), fetch_quotes=fetch or (lambda codes: {}))


def test_status_marks_positions_to_fetched_prices(paths):
    conn = db.connect(paths[0])
    Ledger(conn).buy("main", quote("600001", pct=-1.0), T, "x")
    conn.close()
    api = make_api(paths, fetch=lambda codes: {"600001": quote("600001", pct=1.0)})
    s = api.status()
    main, shadow = s["accounts"]
    assert main["id"] == "main" and main["positions"][0]["price"] == 10.1
    assert main["positions"][0]["pnl_pct"] == round((10.1 / 9.9 - 1) * 100, 2)
    assert shadow["equity"] == 30000 and shadow["positions"] == []
    assert s["running"] is False and s["phase"] == "未运行"


def test_fills_decisions_curve(paths):
    conn = db.connect(paths[0])
    Ledger(conn).buy("main", quote("600001", pct=-1.0), T, "低吸")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail) "
                 "VALUES ('2026-09-23T10:00:00', '600002', 'reject', '新增减持')")
    conn.execute("INSERT INTO equity VALUES ('2026-09-23', 'main', 30100)")
    conn.commit()
    conn.close()
    api = make_api(paths)
    assert api.fills("2026-09-23")[0]["side"] == "买"
    assert api.decisions("2026-09-23") == [
        {"time": "10:00", "code": "600002", "verdict": "reject", "detail": "新增减持"}]
    assert api.equity_curve() == {"main": [["2026-09-23", 30100.0]], "shadow": []}


def test_reports_and_path_traversal(paths):
    rep = paths[1]
    rep.mkdir()
    (rep / "2026-09-23.md").write_text("# 复盘", encoding="utf-8")
    (rep / "notes.txt").write_text("x", encoding="utf-8")
    api = make_api(paths)
    assert api.reports() == ["2026-09-23.md"]
    assert api.report("2026-09-23.md") == "# 复盘"
    with pytest.raises(ValueError):
        api.report("../jevq/config.py")


def test_start_stop_runs_runner_in_thread(paths):
    started = threading.Event()

    def runner(stop, log, state):
        state["phase"] = "盘中"
        log("hello")
        started.set()
        stop.wait(5)

    api = make_api(paths, runner=runner)
    assert api.start()["ok"]
    assert started.wait(2)
    assert api.is_running() and api.status()["phase"] == "盘中"
    assert api.start()["ok"] is False
    api.stop()
    api._thread.join(2)
    assert not api.is_running()
    logs = api.logs()
    assert any("hello" in line for line in logs) and any("运行结束" in line for line in logs)


def test_runner_error_is_logged(paths):
    def runner(stop, log, state):
        raise RuntimeError("缺少环境变量 TYPESAFE_API_KEY")

    api = make_api(paths, runner=runner)
    api.start()
    api._thread.join(2)
    assert any("TYPESAFE_API_KEY" in line for line in api.logs())
```

- [ ] **Step 3: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_api.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jevq.app'`

- [ ] **Step 4: 实现 Api**

`jevq/app/__init__.py`：空文件。

`jevq/app/api.py`:
```python
import re
import threading
from collections import deque
from datetime import date, datetime
from pathlib import Path

from jevq import db
from jevq.config import ACCOUNTS, DB_PATH, INITIAL_CASH, REPORT_DIR
from jevq.data import tencent
from jevq.ledger import Ledger

REPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
NAMES = {"main": "主账户(Jev)", "shadow": "影子账户"}


class Api:
    """暴露给 pywebview 前端的方法；下划线开头的成员不会暴露给 JS。"""

    def __init__(self, db_path: str = DB_PATH, report_dir: str = REPORT_DIR,
                 runner=None, fetch_quotes=tencent.fetch_quotes):
        if runner is None:
            from run import run_day as runner
        self._db_path, self._report_dir = db_path, Path(report_dir)
        self._runner, self._fetch_quotes = runner, fetch_quotes
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._logs: deque[str] = deque(maxlen=500)
        self._state: dict = {}

    # ---- 运行控制 ----
    def start(self) -> dict:
        if self.is_running():
            return {"ok": False, "msg": "已经在运行"}
        self._stop = threading.Event()
        self._state.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return {"ok": True, "msg": "已启动"}

    def stop(self) -> dict:
        self._stop.set()
        return {"ok": True, "msg": "正在停止"}

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        self._log("开始运行")
        try:
            self._runner(stop=self._stop, log=self._log, state=self._state)
        except Exception as e:
            self._log(f"出错：{e}")
        finally:
            self._state["phase"] = "未运行"
            self._log("运行结束")

    def _log(self, msg: str):
        self._logs.append(f"{datetime.now():%H:%M:%S} {msg}")

    # ---- 查询 ----
    def status(self) -> dict:
        conn = db.connect(self._db_path)
        try:
            ledger = Ledger(conn)
            prices = dict(self._state.get("prices") or {})
            held = {p.code for a in ACCOUNTS for p in ledger.positions(a)}
            missing = sorted(held - prices.keys())
            if missing:
                try:
                    prices.update({c: q.price for c, q in self._fetch_quotes(missing).items()})
                except Exception as e:
                    self._log(f"持仓行情获取失败：{e}")
            accounts = [self._account(ledger, a, prices) for a in ACCOUNTS]
        finally:
            conn.close()
        return {"running": self.is_running(), "phase": self._state.get("phase", "未运行"),
                "overseas": self._state.get("overseas"), "sentiment": self._state.get("sentiment"),
                "today": date.today().isoformat(), "accounts": accounts}

    def _account(self, ledger: Ledger, account: str, prices: dict[str, float]) -> dict:
        value = ledger.equity(account, prices)
        positions = []
        for p in ledger.positions(account):
            price = prices.get(p.code)
            positions.append({"code": p.code, "shares": p.shares, "buy_price": p.buy_price,
                              "buy_date": p.buy_date.isoformat(), "price": price,
                              "pnl_pct": None if price is None else round((price / p.buy_price - 1) * 100, 2)})
        return {"id": account, "name": NAMES[account], "equity": value,
                "cash": round(ledger.cash(account), 2),
                "return_pct": round((value / INITIAL_CASH - 1) * 100, 2), "positions": positions}

    def fills(self, day: str) -> list[dict]:
        conn = db.connect(self._db_path)
        try:
            return [{"time": f.ts.strftime("%H:%M"), "account": NAMES[f.account], "code": f.code,
                     "side": "买" if f.side == "buy" else "卖", "price": f.price, "shares": f.shares,
                     "fee": f.fee, "reason": f.reason}
                    for f in Ledger(conn).fills_on(date.fromisoformat(day))]
        finally:
            conn.close()

    def decisions(self, day: str) -> list[dict]:
        conn = db.connect(self._db_path)
        try:
            rows = conn.execute("SELECT ts, code, verdict, detail FROM decisions "
                                "WHERE substr(ts, 1, 10)=? ORDER BY id DESC LIMIT 300", (day,)).fetchall()
        finally:
            conn.close()
        return [{"time": ts[11:16], "code": code or "市场", "verdict": verdict, "detail": detail}
                for ts, code, verdict, detail in rows]

    def equity_curve(self) -> dict[str, list]:
        conn = db.connect(self._db_path)
        try:
            rows = conn.execute("SELECT date, account, value FROM equity ORDER BY date").fetchall()
        finally:
            conn.close()
        out: dict[str, list] = {a: [] for a in ACCOUNTS}
        for d, a, v in rows:
            out[a].append([d, v])
        return out

    def reports(self) -> list[str]:
        if not self._report_dir.exists():
            return []
        return sorted((p.name for p in self._report_dir.glob("*.md") if REPORT_NAME.match(p.name)),
                      reverse=True)

    def report(self, name: str) -> str:
        if not REPORT_NAME.match(name):
            raise ValueError(f"非法报告名：{name}")
        return (self._report_dir / name).read_text(encoding="utf-8")

    def logs(self) -> list[str]:
        return list(self._logs)
```

- [ ] **Step 5: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_api.py -v`
Expected: 5 passed

- [ ] **Step 6: 写前端页面**

`jevq/app/ui.html`:
```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Jev 低吸模拟盘</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --text:#1d2129; --muted:#86909c; --line:#e5e6eb;
          --up:#d93026; --down:#1a9e5c; --accent:#3370ff; --shadow:#8f5cf7; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#17181a; --card:#232429; --text:#e8e9eb; --muted:#8a8f98; --line:#34363c;
            --up:#f5584a; --down:#2fbf71; --accent:#5b8cff; --shadow:#a98bff; }
  }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.5 "Microsoft YaHei UI", system-ui, sans-serif; background:var(--bg); color:var(--text); }
  header { display:flex; align-items:center; gap:16px; padding:14px 20px; background:var(--card); border-bottom:1px solid var(--line); }
  header h1 { font-size:16px; margin:0 12px 0 0; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--muted); display:inline-block; margin-right:6px; }
  .dot.on { background:var(--down); box-shadow:0 0 0 3px color-mix(in srgb, var(--down) 25%, transparent); }
  .tag { color:var(--muted); }
  .tag b { color:var(--text); font-weight:600; }
  .spacer { flex:1; }
  button { border:1px solid var(--line); background:var(--card); color:var(--text); padding:6px 16px; border-radius:6px; cursor:pointer; font:inherit; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button:disabled { opacity:.45; cursor:default; }
  main { padding:16px 20px; display:grid; gap:16px; }
  .cards { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .card h2 { font-size:14px; margin:0 0 8px; color:var(--muted); font-weight:500; }
  .big { font-size:24px; font-weight:600; font-variant-numeric:tabular-nums; }
  .up { color:var(--up); } .down { color:var(--down); }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
  td.wrap { white-space:normal; }
  th { color:var(--muted); font-weight:500; }
  .tabs { display:flex; gap:4px; border-bottom:1px solid var(--line); margin-bottom:10px; }
  .tabs button { border:none; border-radius:0; border-bottom:2px solid transparent; background:none; padding:8px 14px; }
  .tabs button.active { border-bottom-color:var(--accent); color:var(--accent); }
  .empty { color:var(--muted); padding:16px 8px; }
  #curve svg { width:100%; height:180px; display:block; }
  .legend span { margin-right:14px; color:var(--muted); }
  .legend i { display:inline-block; width:10px; height:3px; vertical-align:middle; margin-right:4px; }
  #log { font:12px/1.6 Consolas, monospace; max-height:360px; overflow:auto; white-space:pre-wrap; }
  .layout { display:grid; grid-template-columns:200px 1fr; gap:16px; }
  .report-list button { display:block; width:100%; text-align:left; margin-bottom:4px; }
  .md table { margin:8px 0; } .md h1 { font-size:18px; } .md h2 { font-size:15px; margin-top:18px; }
  .md blockquote { color:var(--muted); margin:8px 0; padding-left:10px; border-left:3px solid var(--line); }
</style>
</head>
<body>
<header>
  <h1>Jev 低吸模拟盘</h1>
  <span><span id="dot" class="dot"></span><span id="phase">未运行</span></span>
  <span class="tag">外围 <b id="overseas">–</b></span>
  <span class="tag">情绪 <b id="sentiment">–</b></span>
  <span class="spacer"></span>
  <button id="btnStart" class="primary">开始运行</button>
  <button id="btnStop" disabled>停止</button>
</header>
<main>
  <section class="cards" id="accounts"></section>
  <section class="card" id="curve">
    <h2>净值曲线 <span class="legend"><span><i style="background:var(--accent)"></i>主账户(Jev)</span><span><i style="background:var(--shadow)"></i>影子账户</span></span></h2>
    <div id="curveBody"></div>
  </section>
  <section class="card">
    <div class="tabs">
      <button data-tab="fills" class="active">今日成交</button>
      <button data-tab="decisions">Jev 判断</button>
      <button data-tab="reports">复盘报告</button>
      <button data-tab="logs">运行日志</button>
    </div>
    <div id="tabBody"></div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = (n, d = 2) => n == null ? "–" : Number(n).toLocaleString("zh-CN", {minimumFractionDigits:d, maximumFractionDigits:d});
const signCls = (n) => n > 0 ? "up" : n < 0 ? "down" : "";
let api, tab = "fills", today = "";

function table(headers, rows) {
  if (!rows.length) return '<div class="empty">暂无数据</div>';
  return `<table><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr>${rows.map((r) =>
    `<tr>${r.map((c, i) => `<td class="${i === r.length - 1 ? "wrap" : ""}">${c}</td>`).join("")}</tr>`).join("")}</table>`;
}

function renderAccounts(accounts) {
  $("accounts").innerHTML = accounts.map((a) => `
    <div class="card">
      <h2>${esc(a.name)}</h2>
      <div class="big">${fmt(a.equity)} <span class="${signCls(a.return_pct)}" style="font-size:15px">${a.return_pct > 0 ? "+" : ""}${fmt(a.return_pct)}%</span></div>
      <div class="tag">现金 ${fmt(a.cash)}</div>
      ${table(["代码", "股数", "成本价", "现价", "盈亏", "买入日"], a.positions.map((p) => [
        esc(p.code), p.shares, fmt(p.buy_price), fmt(p.price),
        `<span class="${signCls(p.pnl_pct)}">${p.pnl_pct == null ? "–" : fmt(p.pnl_pct) + "%"}</span>`, esc(p.buy_date)]))}
    </div>`).join("");
}

function renderCurve(curve) {
  const series = [["main", "var(--accent)"], ["shadow", "var(--shadow)"]];
  const all = series.flatMap(([k]) => curve[k] || []);
  if (all.length < 2) { $("curveBody").innerHTML = '<div class="empty">收盘后生成第一条记录，满两天开始画线</div>'; return; }
  const dates = [...new Set(all.map((p) => p[0]))].sort();
  const vals = all.map((p) => p[1]).concat([30000]);
  const lo = Math.min(...vals), hi = Math.max(...vals), W = 1000, H = 180, pad = 10;
  const x = (d) => pad + (dates.indexOf(d) / Math.max(1, dates.length - 1)) * (W - 2 * pad);
  const y = (v) => H - pad - ((v - lo) / Math.max(1, hi - lo)) * (H - 2 * pad);
  const lines = series.map(([k, color]) => `<polyline fill="none" stroke="${color}" stroke-width="2"
      points="${(curve[k] || []).map((p) => `${x(p[0])},${y(p[1])}`).join(" ")}"/>`).join("");
  $("curveBody").innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <line x1="0" x2="${W}" y1="${y(30000)}" y2="${y(30000)}" stroke="var(--line)" stroke-dasharray="4 4"/>${lines}</svg>
    <div class="tag">${esc(dates[0])} → ${esc(dates[dates.length - 1])}　虚线为初始资金 30,000</div>`;
}

function mdToHtml(md) {
  const out = []; let rows = [];
  const flush = () => { if (rows.length) { out.push(table(rows[0], rows.slice(1))); rows = []; } };
  for (const line of md.split("\n")) {
    if (line.startsWith("|")) {
      const cells = line.split("|").slice(1, -1).map((c) => esc(c.trim()));
      if (!cells.every((c) => /^-+$/.test(c))) rows.push(cells);
      continue;
    }
    flush();
    if (line.startsWith("## ")) out.push(`<h2>${esc(line.slice(3))}</h2>`);
    else if (line.startsWith("# ")) out.push(`<h1>${esc(line.slice(2))}</h1>`);
    else if (line.startsWith("> ")) out.push(`<blockquote>${esc(line.slice(2))}</blockquote>`);
    else if (line.trim()) out.push(`<p>${esc(line)}</p>`);
  }
  flush();
  return out.join("");
}

async function renderTab() {
  const body = $("tabBody");
  if (tab === "fills") {
    const rows = await api.fills(today);
    body.innerHTML = table(["时间", "账户", "代码", "方向", "价格", "股数", "费用", "理由"],
      rows.map((f) => [f.time, esc(f.account), esc(f.code), f.side, fmt(f.price), f.shares, fmt(f.fee), esc(f.reason)]));
  } else if (tab === "decisions") {
    const rows = await api.decisions(today);
    const label = {pass: "通过", reject: "否决", error: "出错", sentiment: "情绪"};
    body.innerHTML = table(["时间", "代码", "结果", "理由"],
      rows.map((d) => [d.time, esc(d.code), label[d.verdict] || esc(d.verdict), esc(d.detail)]));
  } else if (tab === "reports") {
    const names = await api.reports();
    if (!names.length) { body.innerHTML = '<div class="empty">还没有复盘报告，第一个交易日 15:05 后生成</div>'; return; }
    body.innerHTML = `<div class="layout"><div class="report-list">${names.map((n) =>
      `<button data-report="${esc(n)}">${esc(n.replace(".md", ""))}</button>`).join("")}</div><div class="md" id="md"></div></div>`;
    body.querySelectorAll("[data-report]").forEach((b) =>
      b.onclick = async () => { $("md").innerHTML = mdToHtml(await api.report(b.dataset.report)); });
    body.querySelector("[data-report]").click();
  } else {
    const lines = await api.logs();
    body.innerHTML = `<div id="log">${lines.length ? esc(lines.join("\n")) : "暂无日志"}</div>`;
    $("log").scrollTop = 1e9;
  }
}

async function refresh() {
  const s = await api.status();
  today = s.today;
  $("dot").className = "dot" + (s.running ? " on" : "");
  $("phase").textContent = s.running ? s.phase : "未运行";
  $("overseas").textContent = s.overseas || "–";
  $("sentiment").textContent = s.sentiment || "–";
  $("btnStart").disabled = s.running;
  $("btnStop").disabled = !s.running;
  renderAccounts(s.accounts);
  renderCurve(await api.equity_curve());
  if (tab !== "reports") await renderTab();
}

window.addEventListener("pywebviewready", async () => {
  api = window.pywebview.api;
  $("btnStart").onclick = async () => { await api.start(); refresh(); };
  $("btnStop").onclick = async () => { await api.stop(); refresh(); };
  document.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    tab = b.dataset.tab; renderTab();
  });
  await refresh();
  await renderTab();
  setInterval(refresh, 5000);
});
</script>
</body>
</html>
```

- [ ] **Step 7: 写启动入口和快捷方式脚本**

`desktop.py`:
```python
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
# pythonw 没有控制台，sys.stdout/stderr 为 None；akshare 的 tqdm 进度条一写就崩
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import webview  # noqa: E402

from jevq.app.api import Api  # noqa: E402


def main():
    api = Api()
    window = webview.create_window("Jev 低吸模拟盘", str(ROOT / "jevq" / "app" / "ui.html"),
                                   js_api=api, width=1280, height=820, min_size=(960, 640))

    def on_closing(*_):
        api.stop()

    window.events.closing += on_closing
    webview.start()


if __name__ == "__main__":
    main()
```

`install_shortcut.py`:
```python
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Jev低吸模拟盘"


def main():
    target = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    script = ROOT / "desktop.py"
    ps = ("$ws = New-Object -ComObject WScript.Shell;"
          "$d = [Environment]::GetFolderPath('Desktop');"
          f"$l = $ws.CreateShortcut(\"$d\\{NAME}.lnk\");"
          f"$l.TargetPath = '{target}';"
          f"$l.Arguments = '\"{script}\"';"
          f"$l.WorkingDirectory = '{ROOT}';"
          "$l.Save(); Write-Output \"$d\\" + NAME + ".lnk\"")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         check=True, capture_output=True, text=True)
    print("已创建快捷方式：", out.stdout.strip())


if __name__ == "__main__":
    main()
```

- [ ] **Step 8: 全量回归**

Run: `.venv/Scripts/python -m pytest -q`
Expected: 全部 passed

- [ ] **Step 9: 启动冒烟（不需要 Jev Key）**

Run: `.venv/Scripts/python -c "import subprocess,time,sys; p=subprocess.Popen([r'.venv\Scripts\pythonw.exe','desktop.py']); time.sleep(8); alive=p.poll() is None; p.kill(); print('alive', alive); sys.exit(0 if alive else 1)"`
Expected: `alive True`（窗口 8 秒内没有崩溃）

- [ ] **Step 10: Commit**

```bash
git add requirements.txt jevq/app desktop.py install_shortcut.py tests/test_api.py
git commit -m "feat: desktop panel with pywebview"
```

- [ ] **Step 11: 创建桌面快捷方式（controller 执行，不由 implementer 执行）**

Run: `.venv/Scripts/python install_shortcut.py`
Expected: 打印桌面上 `Jev低吸模拟盘.lnk` 的路径

---

### Task 14: Jev 调用通道（TypeSafe / Cloudflare）

在 Task 13 之后、Task 12 之前执行。背景：TypeSafe 官网暂停新用户注册，Jev 同时在 Cloudflare Workers AI 上架（模型 ID `typesafe/jev`），请求体外多一层 `{"model", "input"}`，返回同样带 `answers`。

**Files:**
- Modify: `jevq/judge/jev.py`, `run.py`
- Test: `tests/test_jev.py`

**Interfaces:**
- Consumes: 现有 `JevClient`、`run_day`
- Produces:
  - `Provider(name: str, url: str, headers: dict[str, str], wrap_model: str | None)`；`Provider.body(state, questions) -> dict`
  - `typesafe_provider(api_key) -> Provider`、`cloudflare_provider(account_id, token) -> Provider`
  - `provider_from_env(env: Mapping[str, str] = os.environ) -> Provider`：有 `TYPESAFE_API_KEY` 用 TypeSafe；否则有 `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` 用 Cloudflare；都没有抛 `RuntimeError("缺少 Jev 凭证：设置 TYPESAFE_API_KEY，或 CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN")`
  - `JevClient(api_key: str | None = None, session=None, conn=None, provider: Provider | None = None)`：`provider` 优先；否则用 `typesafe_provider(api_key)`。现有调用 `JevClient("k", session=s)` 行为不变。
  - 响应解析：若 JSON 顶层有 dict 型 `result` 字段则先取 `result`（Cloudflare REST 的常见外壳），再取 `answers`。

- [ ] **Step 1: 写失败的测试（追加到 `tests/test_jev.py`）**

```python
from jevq.judge.jev import cloudflare_provider, provider_from_env, typesafe_provider


def test_cloudflare_body_url_and_auth():
    s = FakeSession(OK)
    JevClient(session=s, provider=cloudflare_provider("acc123", "tok")).ask("state", Q)
    url, body, headers = s.calls[0]
    assert url == "https://api.cloudflare.com/client/v4/accounts/acc123/ai/run"
    assert body == {"model": "typesafe/jev", "input": {"state": "state", "questions": Q}}
    assert headers["Authorization"] == "Bearer tok"


def test_unwraps_result_envelope():
    wrapped = Resp(200, {"result": {"answers": {"q": {"type": "noul", "noul": 0.7}}}, "success": True})
    ans = JevClient(session=FakeSession(wrapped), provider=cloudflare_provider("a", "t")).ask("s", Q)
    assert ans["q"]["noul"] == 0.7


def test_provider_from_env():
    assert provider_from_env({"TYPESAFE_API_KEY": "k"}).name == "typesafe"
    cf = provider_from_env({"CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_API_TOKEN": "t"})
    assert cf.name == "cloudflare"
    assert provider_from_env({"TYPESAFE_API_KEY": "k", "CLOUDFLARE_ACCOUNT_ID": "a",
                              "CLOUDFLARE_API_TOKEN": "t"}).name == "typesafe"
    with pytest.raises(RuntimeError, match="缺少 Jev 凭证"):
        provider_from_env({"CLOUDFLARE_ACCOUNT_ID": "a"})


def test_typesafe_provider_matches_legacy_constructor():
    s1, s2 = FakeSession(OK), FakeSession(OK)
    JevClient("k", session=s1).ask("s", Q)
    JevClient(session=s2, provider=typesafe_provider("k")).ask("s", Q)
    assert s1.calls == s2.calls
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_jev.py -v`
Expected: FAIL，`ImportError: cannot import name 'cloudflare_provider'`

- [ ] **Step 3: 实现**

在 `jevq/judge/jev.py` 顶部 import 增加 `import os`、`from collections.abc import Mapping`、`from dataclasses import dataclass`，并加入：

```python
CLOUDFLARE_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run"
CLOUDFLARE_MODEL = "typesafe/jev"


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    headers: dict[str, str]
    wrap_model: str | None = None      # 非空时请求体为 {"model": wrap_model, "input": {...}}

    def body(self, state, questions: dict) -> dict:
        if self.wrap_model:
            return {"model": self.wrap_model, "input": {"state": state, "questions": questions}}
        return {"state": state, "model": JEV_MODEL, "questions": questions}


def typesafe_provider(api_key: str) -> Provider:
    return Provider("typesafe", JEV_URL, {"Authorization": f"Bearer {api_key}"})


def cloudflare_provider(account_id: str, token: str) -> Provider:
    return Provider("cloudflare", CLOUDFLARE_URL.format(account=account_id),
                    {"Authorization": f"Bearer {token}"}, wrap_model=CLOUDFLARE_MODEL)


def provider_from_env(env: Mapping[str, str] = os.environ) -> Provider:
    if env.get("TYPESAFE_API_KEY"):
        return typesafe_provider(env["TYPESAFE_API_KEY"])
    if env.get("CLOUDFLARE_ACCOUNT_ID") and env.get("CLOUDFLARE_API_TOKEN"):
        return cloudflare_provider(env["CLOUDFLARE_ACCOUNT_ID"], env["CLOUDFLARE_API_TOKEN"])
    raise RuntimeError("缺少 Jev 凭证：设置 TYPESAFE_API_KEY，或 CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN")
```

`JevClient` 改为：

```python
class JevClient:
    def __init__(self, api_key: str | None = None, session=None,
                 conn: sqlite3.Connection | None = None, provider: Provider | None = None):
        self.provider = provider or typesafe_provider(api_key)
        self.session = session or requests.Session()
        self.conn = conn

    def ask(self, state, questions: dict) -> dict:
        body = self.provider.body(state, questions)
        error = "unknown"
        for _ in range(2):
            try:
                r = self.session.post(self.provider.url, json=body, headers=self.provider.headers,
                                      timeout=JEV_TIMEOUT)
            except requests.RequestException as e:
                error = repr(e)
                continue
            if r.status_code == 200:
                data = r.json()
                if isinstance(data.get("result"), dict):
                    data = data["result"]
                answers = data["answers"]
                self._log(body, answers, None)
                return answers
            error = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in RETRYABLE and r.status_code < 500:
                break
        self._log(body, None, error)
        raise JevError(error)
```

`_log` 与 `score_label` 不变。

`run.py` 的 `run_day` 中，把：
```python
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("缺少环境变量 TYPESAFE_API_KEY")
```
换成：
```python
    provider = provider_from_env()
```
把 `jev = JevClient(key, conn=conn)` 换成 `jev = JevClient(conn=conn, provider=provider)`，并在其后加 `log(f"Jev 通道：{provider.name}")`。import 增加 `from jevq.judge.jev import JevClient, provider_from_env`（替换原 `JevClient` 单独 import）；若 `os` 不再使用则删除 `import os`。

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/Scripts/python -m pytest -q`
Expected: 全部 passed

Run（未设置任何凭证时）: `.venv/Scripts/python run.py --once`
Expected: 以 `缺少 Jev 凭证：设置 TYPESAFE_API_KEY，或 CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN` 退出

- [ ] **Step 5: Commit**

```bash
git add jevq/judge/jev.py run.py tests/test_jev.py
git commit -m "feat: pluggable jev provider (typesafe or cloudflare workers ai)"
```

---

### Task 12: 真实环境冒烟（需要 Jev API Key）

**前置：** 用户在自己的终端设置好 Jev 凭证（Claude 不写入任何凭证），二选一：
- TypeSafe：`setx TYPESAFE_API_KEY "<key>"`
- Cloudflare Workers AI（TypeSafe 暂停注册时的替代）：`setx CLOUDFLARE_ACCOUNT_ID "<id>"` 与 `setx CLOUDFLARE_API_TOKEN "<token>"`
设置后需要重开终端 / 重启桌面程序才会生效。

- [ ] **Step 1: 交易时段跑一轮**

Run: `.venv/Scripts/python run.py --once`
Expected:
- 股票池 2800–3300 只，5 日涨幅数量接近股票池数量
- 首次运行公告同步约 10 分钟，打印同步条数
- 打印外围判断和情绪判断，结果为有效档位名称，不是 `None`
- 打印本轮成交；主账户买入前在 `decisions` 表里有检查记录

- [ ] **Step 2: 抽查 Jev 调用**

Run:
```bash
.venv/Scripts/python -c "import sqlite3; c=sqlite3.connect('data/jevq.sqlite'); [print(r) for r in c.execute('select ts, substr(request,1,120), substr(response,1,120), error from jev_calls order by id desc limit 5')]"
```
Expected: 有 response，error 为空；抽 3 条公告分类，人工确认归类合理

- [ ] **Step 3: 清空冒烟数据**

冒烟跑出的成交会污染正式模拟盘，先问用户是否删除 `data/jevq.sqlite` 里的 `fills`、`positions`、`cash`、`equity`、`decisions`、`turnover`。公告库和判断缓存（`notices`、`notice_fetch`、`notice_verdicts`）保留。用户同意后执行：
```bash
.venv/Scripts/python -c "import sqlite3; c=sqlite3.connect('data/jevq.sqlite'); [c.execute(f'delete from {t}') for t in ('fills','positions','cash','equity','decisions','turnover')]; c.commit()"
```

- [ ] **Step 4: 正式运行**

每个交易日 9:00 前运行 `run.bat`。要不要设置成 Windows 开机或定时任务自动运行，由用户决定，属于系统配置改动，不在本计划里自动执行。
