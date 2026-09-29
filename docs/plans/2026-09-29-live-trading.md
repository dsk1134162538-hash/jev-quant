# 实盘跟单 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 模拟账户 `rebound_main` 的每笔成交，由家里电脑上的 `live.py` 在东亚前海真实账户里跟单；手机上可暂停/恢复/立即卖出；低吸下线；自动上线改为先测后合并。

**Architecture:** daemon 只把成交写进 `live_intents`；`live.py` 是唯一操作账户的进程（执行器 + 手机页）；下单层是一个小接口（xiadan-gateway / easytrader / 测试用假券商）。全部状态在 `data/jevq.sqlite` 的 `live_*` 表里，页面线程只读写数据库，从不直接碰券商。

**Tech Stack:** Python 3.12、sqlite3（WAL）、标准库 `http.server`、`requests`、pytest。easytrader 只在家里电脑装，测试不导入。

**Spec:** `docs/specs/2026-09-29-live-trading-design.md`（每个任务都要先读这份 spec 的相关小节）

## Global Constraints

- 中文注释/文案，风格照现有代码：注释少、命名清楚；不加没要求的回退和校验，只在系统边界（券商返回、HTTP 请求）校验。
- 测试：`D:/claude/jev-quant/.venv/Scripts/python -m pytest -q -p no:cacheprovider`（在各自 worktree 根目录跑），必须全绿；不联网、不碰 `data/`、`reports/`（conftest 已隔离 cwd 和推送）；不导入 easytrader；时间一律注入（`clock`、`sleep`），测试里不真等。
- 凭证（`XIADAN_GATEWAY_TOKEN`、`PUSHPLUS_TOKEN`、`TYPESAFE_API_KEY`）绝不打印、不入库、不写进文件。
- 只 `git add` 自己改的文件；不做 reset --hard / force push / clean。提交信息结尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- 数值常量一律放 `jevq/config.py` 的「实盘跟单」段（Task 2 定义），其他文件从 config 导入。
- 金额/价格：委托价 `round(x, 2)`；股数按 100 股整数倍。

## 并行安排

- 轨道 A（worktree `D:\claude\jev-quant-live`，分支 `live-trading`）：Task 1 → 2 → 3 → 4 → 5。Task 1 完成后立即推送分支（用户下午要在家实测）。
- 轨道 B（worktree `D:\claude\jev-quant-lowbuy`，分支 `lowbuy-removal`，从 `26e5dec` 开）：Task 9 → 10。
- 轨道 C（worktree `D:\claude\jev-quant-livec`，分支 `live-web`，从 Task 2 完成后的 `live-trading` 开）：Task 6 → 7 → 8。
- Task 11：把 B、C 合进 `live-trading`，解决冲突，全量测试，更新 HANDOFF，推送。

## 文件结构

| 文件 | 职责 |
|---|---|
| `jevq/live/__init__.py` | 空 |
| `jevq/live/broker.py` | `BrokerError`、`Holding`、`OrderRow`、`Broker` 协议、`REJECT_CODES` |
| `jevq/live/gateway.py` | `GatewayBroker`：xiadan-gateway HTTP 客户端 |
| `jevq/live/easytrader_broker.py` | `EasytraderBroker`：easytrader 包装（懒导入） |
| `jevq/live/store.py` | `live_*` 表的读写小函数、`Intent` / `LivePosition` 数据类、暂停状态 |
| `jevq/live/executor.py` | `Executor`：买入、卖出对齐、撤单核对、暂停/恢复、对账、模式、通知 |
| `jevq/live/web.py` | 手机页 HTTP 服务（只读写数据库） |
| `jevq/live/watchdog.py` | daemon 用：检查 live.py 心跳并重启 |
| `live.py` | 入口：单实例锁、建券商、起页面线程、主循环、收盘后按新提交自我重启 |
| `scripts/live_probe.py` | 家里实测：只读读取统计；用户手动的「跌停价挂单再撤」 |
| `docs/live-setup.md` | 家里安装步骤（给用户看） |
| `tests/live_fakes.py` | `FakeBroker`、`FakeClock`、`FakeNotifier` |
| `tests/test_live_*.py` | 各模块测试 |

---

### Task 1: 下单层 + 实测脚本 + 家里安装说明

（见本文件末尾「Task 1 详细」，依赖 xiadan-gateway / easytrader 源码审查结论。）

---

### Task 2: 配置、表结构、store、daemon 写意图

**Files:**
- Modify: `jevq/config.py`（末尾加「实盘跟单」段）
- Modify: `jevq/db.py`（SCHEMA 加 4 张表）
- Create: `jevq/live/store.py`
- Modify: `run.py`（`_run_minute` 的 `emit` 里写意图；`_run_day` 判定交易日后写 `trading_day`）
- Create: `tests/live_fakes.py`、`tests/test_live_store.py`
- Modify: `tests/test_run.py`（加意图相关用例）

**Interfaces — Produces:**

```python
# jevq/config.py 追加（原样照抄）
from datetime import time  # 已有
# 实盘跟单（docs/specs/2026-09-29-live-trading-design.md）
LIVE_ACCOUNT = "rebound_main"
LIVE_BUDGET = 20_000.0
LIVE_MAX_POSITIONS = 1
LIVE_MAX_BUYS_PER_DAY = 2
LIVE_INTENT_TTL = 60            # 秒：意图超过这么久没处理就作废（买入）
LIVE_BUY_SLIP = 0.005           # 买入限价最多比模拟成交价高 0.5%
LIVE_BUY_WAIT = 30              # 秒：买单挂多久没成交完就撤
LIVE_SELL_DISCOUNT = 0.01       # 卖出限价 = 最新价 × (1 - 1%)，不低于跌停价
LIVE_SELL_WAIT = 20             # 秒：卖单每轮等多久
LIVE_SELL_ROUNDS = 3            # 卖单撤了重挂最多几轮，之后每分钟重试
LIVE_SELL_RETRY = 60            # 秒：几轮都没卖完后，下次重试间隔
LIVE_CASH_BUFFER = 0.002        # 算股数时资金留 0.2% 余量
LIVE_POLL = 5                   # 秒：等成交时查当日委托的间隔
LIVE_RECONCILE_HOLDING = 5      # 分钟：有持仓时对账间隔
LIVE_RECONCILE_IDLE = 15        # 分钟：空仓时读资金确认在线的间隔
LIVE_READ_FAIL_PAUSE = 3        # 连续读取失败几次自动暂停
LIVE_REDUCE_CONFIRM = 60        # 秒：客户端股数变少，两次读数至少隔这么久才减账
LIVE_BUY_WINDOWS = ((time(9, 30), time(11, 30)), (time(13, 0), time(14, 57)))
LIVE_SELL_WINDOWS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))
LIVE_NO_CANCEL_AFTER = time(14, 57)   # 收盘集合竞价不能撤单
LIVE_PAUSE_CLOSE = time(15, 0)        # 「暂停到收盘」的截止
LIVE_SELF_CHECK = time(9, 5)
LIVE_CLOSE_SUMMARY = time(15, 2)
LIVE_WATCHDOG_STALE = 120       # 秒：心跳多久没更新算断
LIVE_WATCHDOG_MAX_RESTARTS = 3  # daemon 每天最多替它重启几次
LIVE_HTTP_PORT = 8766
LIVE_BROKER = "gateway"         # "gateway" | "easytrader"：家里实测后定
LIVE_GATEWAY_URL = "http://127.0.0.1:5000"
LIVE_EASYTRADER_EXE = r"C:\同花顺软件\同花顺\xiadan.exe"   # 实测时按实际安装路径改
LIVE_LOCK_NAME = "live.lock"
```

```sql
-- jevq/db.py SCHEMA 追加
CREATE TABLE IF NOT EXISTS live_intents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, side TEXT, code TEXT, name TEXT,
  sim_price REAL, sim_shares INTEGER, reason TEXT, status TEXT, note TEXT, handled_ts TEXT);
CREATE TABLE IF NOT EXISTS live_orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, intent_id INTEGER, ts TEXT, side TEXT, code TEXT,
  price REAL, shares INTEGER, filled INTEGER, avg_price REAL, entrust_no TEXT, status TEXT,
  note TEXT, updated_ts TEXT);
CREATE TABLE IF NOT EXISTS live_positions(
  code TEXT PRIMARY KEY, shares INTEGER, cost REAL, buy_price REAL, buy_date TEXT, sim_buy_price REAL);
CREATE TABLE IF NOT EXISTS live_state(key TEXT PRIMARY KEY, value TEXT);
```

```python
# jevq/live/store.py
@dataclass(frozen=True)
class Intent:
    id: int; ts: datetime; source: str; side: str; code: str; name: str
    sim_price: float; sim_shares: int; reason: str

@dataclass(frozen=True)
class LivePosition:
    code: str; shares: int; cost: float; buy_price: float; buy_date: date; sim_buy_price: float

def get_state(conn, key: str) -> str | None
def set_state(conn, key: str, value: str | None) -> None        # None = 删除该键；每次 commit
def mode(conn) -> str                                           # 'off' | 'dryrun' | 'live'，默认 'off'
def set_mode(conn, new: str) -> None                            # 非法值 ValueError；dryrun→live 时清空 live_positions 和状态键 reduce:*
def add_intents(conn, fills: list[Fill], names: dict[str, str]) -> int
    # 只取 f.account == LIVE_ACCOUNT 的成交；source='sim'、status='new'、ts=f.ts；返回写入条数
def add_remote_sell(conn, now: datetime) -> int                 # source='remote', side='sell', code='*'
def new_intents(conn) -> list[Intent]                           # status='new'，按 id 升序
def finish_intent(conn, intent_id: int, status: str, note: str, now: datetime) -> None
def positions(conn) -> dict[str, LivePosition]
def put_position(conn, p: LivePosition) -> None                 # INSERT OR REPLACE
def reduce_position(conn, code: str, shares: int) -> None       # 减到 ≤0 删行
def add_order(conn, intent_id: int | None, now: datetime, side: str, code: str, price: float,
              shares: int, status: str, note: str = "") -> int
def update_order(conn, order_id: int, now: datetime, **fields) -> None   # 允许字段：filled avg_price entrust_no status note
def orders_on(conn, day: date) -> list[dict]                    # 当天全部委托，按 id
def sending_orders(conn) -> list[dict]                          # status='sending'
def buys_today(conn, day: date) -> int                          # 口径见下
def pause(conn, until: datetime | None, reason: str, kind: str) -> None   # until=None 表示只能手动恢复（kind='auto'）
def clear_pause(conn) -> None
def paused(conn) -> dict | None                                 # {"until": datetime|None, "reason": str, "kind": str} 或 None
```

`buys_today` 口径：当天 `side='buy'` 且 `status` 属于 `submitted/filled/partial/cancelled/dryrun` 的委托条数（`failed` 不算，因为没真下出去）。

状态键约定（其他任务用到，原样照抄）：`mode`、`paused`（'1'）、`paused_until`（ISO 或无）、`pause_reason`、`pause_kind`、`resume_requested`（'1'）、`heartbeat`（ISO）、`pid`、`commit`、`last_read_ok`（ISO）、`read_fail_streak`（整数字符串）、`trading_day`（ISO 日期）、`reduce:<code>`（JSON `{"shares": n, "ts": iso}`）、`notified:<key>:<date>`（当天是否已提醒过）、`self_check_day`、`close_summary_day`。

**run.py 改动：**
- `_run_minute` 的内部函数 `emit(fills)`：在 `_notify_fills(...)` 之前调用 `live_store.add_intents(conn, fills, names)`，异常只 `log`，不影响交易。
- `_run_day`：确定 `trading`（交易日历或周一至周五兜底）且继续往下跑时，`live_store.set_state(conn, "trading_day", today.isoformat())`。

**tests/live_fakes.py（Produces，供 Task 3–8 用）：**

```python
class FakeClock:
    def __init__(self, start: datetime): self.t = start
    def now(self) -> datetime: return self.t
    def sleep(self, s: float) -> None: self.t += timedelta(seconds=s)

class FakeNotifier:
    def __init__(self): self.sent = []            # [(title, content)]
    def send(self, title, content): self.sent.append((title, content))
    def once(self, key, title, content):
        if key not in {k for k, *_ in getattr(self, "_once", [])}:
            self._once = getattr(self, "_once", []) + [(key, title, content)]
            self.sent.append((title, content))

class FakeBroker:
    """脚本化假券商。cash 可用资金；holdings: code -> [shares, available]；
    fill_mode: 'all'（下单即全部成交）| 'none'（永不成交）| 'partial'（成交 fill_ratio，按 100 股取整）|
    'late'（第 late_polls 次 orders_today 时才全部成交）。
    fail_reads: 接下来多少次读取（balance/positions/orders_today）抛 BrokerError。
    place_error: 设为 BrokerError 实例时 place 抛它（一次后清空）。
    记录 calls: [("place", side, code, price, shares), ("cancel_last",), ...]。"""
```

FakeBroker 要让成交反映到 `holdings`（买入加 shares，available 不加——T+1；卖出同时减 shares 和 available）和 `cash`，并且 `orders_today()` 返回的 `OrderRow` 带递增委托号、`time` 取创建时 clock 的 `HH:MM:SS`（构造时传入 clock）。`cancel_last()` 把最后一笔 open 的委托状态改为 `部撤`（有成交）或 `已撤`，返回 True；没有可撤的返回 False。`extra_orders` 列表可预置「用户手动下的单」。

- [ ] **Step 1: 写 store 测试**（`tests/test_live_store.py`）：`add_intents` 只写 `rebound_main`；`new_intents` 顺序；`finish_intent`；`positions/put/reduce`（减到 0 删行）；`add_order/update_order/sending_orders`；`buys_today` 口径（failed 不算、dryrun 算）；`pause(until=None, kind='auto')` 与 `paused()` 往返；`set_mode` 非法值报错、`dryrun→live` 清空持仓与 `reduce:*`。
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现 config/db/store/live_fakes**
- [ ] **Step 4: run.py 两处改动 + 在 `tests/test_run.py` 加用例：** `_run_minute` 里 `rebound_main` 成交后 `live_intents` 多一条、其他账户不写；`add_intents` 抛异常时交易与推送照常。
- [ ] **Step 5: 全量测试通过后提交** `feat(live): config, tables, store, intents from sim fills`

---

### Task 3: 执行器——骨架、买入、`sending` 恢复

**Files:**
- Create: `jevq/live/executor.py`
- Create: `tests/test_live_buy.py`

**Interfaces — Consumes:** Task 1 的 `Broker/BrokerError/Holding/OrderRow/REJECT_CODES`；Task 2 的 store 全部函数、config 常量、`tests/live_fakes.py`。
**Produces:**

```python
class Executor:
    def __init__(self, conn, broker, notifier, clock=datetime.now, sleep=time.sleep,
                 quotes=tencent.fetch_quotes): ...
    def startup(self) -> None      # 核对 sending 委托（spec「防重复下单」）；模式不是 off 且没暂停时先对账
    def step(self) -> None         # 主循环每秒调一次；本任务先实现：心跳、模式 off、交易日门槛、买入
    # 内部（后续任务扩展）：_buy(intent)、_wait_fill(...)、_cancel_last_checked(...)、_auto_pause(reason)、
    #   _read(fn)（统一读取：成功重置 read_fail_streak、写 last_read_ok；失败 +1，到 LIVE_READ_FAIL_PAUSE 自动暂停，然后抛出）
    #   _notify(title, content, key=None)（标题前加「💰 实盘」；key 不为 None 时同一 key 当天只发一次，用状态键 notified:<key>:<date>）
```

**step() 本任务范围：**
1. 写 `heartbeat=now.isoformat()`、`pid`。
2. `mode == 'off'`：所有 new 意图 `skipped`（note「模式关闭」），返回。
3. `trading_day != today`：new 意图 `skipped`（「非交易日」），返回。
4. 逐条处理 new 意图：`side=='buy'` → `_buy`；卖出意图本任务先 `done`（note「按持仓对齐处理」），Task 4 再接上对齐逻辑。

**_buy(intent) 规则（照 spec「买入」原样实现，顺序即检查顺序）：** 过期（`now - intent.ts > LIVE_INTENT_TTL` → `expired`）；时间不在 `LIVE_BUY_WINDOWS` → skipped「不在买入时段」；暂停中 → skipped「暂停中」（Task 5 实现暂停后生效，本任务先读 `store.paused`）；程序已有持仓数 ≥ `LIVE_MAX_POSITIONS` → skipped「程序已有持仓」；读客户端持仓，这只票在客户端持仓里但不在 `live_positions` → skipped「你持有这只」；`buys_today ≥ LIVE_MAX_BUYS_PER_DAY` → skipped「今日买入已达上限」；代码不是 60/00 开头 → skipped「非主板」。
然后：限价 `price = min(round(intent.sim_price * (1 + LIVE_BUY_SLIP), 2), 涨停价)`，涨停价用 `quotes([code])[code].limit_up`（取不到行情就只用前半）；预算 `min(balance(), LIVE_BUDGET) * (1 - LIVE_CASH_BUFFER)`；股数 `ledger.affordable_shares(预算, price)`；为 0 → skipped「买不起一手」。
- 模式 `dryrun`：`add_order(status='dryrun')`，`put_position(shares, cost=shares*price+buy_fee, buy_price=price, buy_date=today, sim_buy_price=intent.sim_price)`，意图 `done`（note「演练」），不调 `place`。
- 模式 `live`：先 `add_order(status='sending')`，再 `broker.place('buy', code, price, shares)`：
  - 抛 `BrokerError` 且 `code in REJECT_CODES` → 订单 `failed`（note 写错误），意图 `done`（note「券商拒单：…」），通知。
  - 抛其他异常 → 走「单笔核对」（同 startup 的 sending 核对）：对上当日委托 → 当作已提交继续；对不上 → `failed`；读不到 → `_auto_pause("下单结果不明")`。
  - 返回委托号（可能 None）→ 订单 `submitted`，写 `entrust_no`；`_wait_fill(..., wait=LIVE_BUY_WAIT)`：每 `LIVE_POLL` 秒读一次当日委托，按委托号匹配，委托号为 None 时按（代码、方向、价格、数量、时间 ≥ 下单时刻的 HH:MM:SS）匹配最新一条；全部成交即返回；到时没成交完 → `_cancel_last_checked(row)` → 再读一次当日委托取最终成交数。
  - 成交数 > 0 → `put_position(...)`（`buy_price` 用成交均价，没有就用限价；`cost = filled*buy_price + buy_fee(filled*buy_price)`），订单 `filled`/`partial`/`cancelled`（0 成交撤单）；通知「💰 实盘 买入 名称 N股 @均价（模拟 @x，差 y%）」或「没成交，已撤单」。
- `_cancel_last_checked(row)`：读当日委托，取最后一条 `open` 的委托，必须与 row 的委托号（或代码/方向/价格/数量）一致，否则 `_auto_pause("撤单前核对不上：最后一笔不是程序的委托")` 并返回 False；一致才 `cancel_last()`。
- `_auto_pause(reason)`：`store.pause(None, reason, 'auto')` + 通知（附原因，说明需手动恢复）。

**startup()：** 对每个 `sending_orders`：读当日委托（读失败 → `_auto_pause("启动核对读不到当日委托")` 并返回）；按代码/方向/数量/价格、时间 ≥ 该行 ts 的 HH:MM:SS 匹配：对上 → 更新为 `submitted` + 委托号，并按成交数更新持仓（已全部成交 → `filled`；未完 → 执行一次 `_cancel_last_checked` 后按最终成交数入账）；对不上 → `failed`（note「启动核对：没找到这笔委托」）。

- [ ] **Step 1: 写测试**（`tests/test_live_buy.py`，每条一个测试函数）：
  - 模式 off → 意图 skipped「模式关闭」，broker 无调用；非交易日同理。
  - 过期 61 秒 → expired；11:31 → skipped「不在买入时段」；已有持仓 → skipped；客户端持有该票而程序没有 → skipped「你持有这只」；当日已 2 笔 → skipped；`300xxx` → skipped「非主板」。
  - 正常买：`cash=50_000`，`sim_price=10.00` → 限价 10.05、股数按 `min(50000, 20000)*0.998` 算（断言 = `affordable_shares(19960.0, 10.05)`），`place` 参数正确，成交后 `live_positions` 有行、订单 `filled`、通知含「买入」。
  - 涨停价封顶：`sim_price` 使 `×1.005` 超过涨停价 → 限价 = 涨停价。
  - 部分成交：`fill_mode='partial'`，30 秒后撤单，持仓 = 部分股数，订单 `partial`；`calls` 里有 `cancel_last`。
  - 不成交：订单 `cancelled`，没有持仓，通知「没成交，已撤单」。
  - 撤单前核对不上：`extra_orders` 在程序下单后追加一笔用户的 open 委托 → 不调 `cancel_last`，进入自动暂停（`paused()['kind']=='auto'`）。
  - 券商拒单（`BrokerError(code='INSUFFICIENT_BALANCE')`）→ 订单 failed、不暂停；未知错误且当日委托里找不到 → failed；未知错误且读当日委托也失败 → 自动暂停。
  - 演练：不调 `place`，订单 `dryrun`，持仓按限价记入。
  - startup：sending 对上（全部成交）→ filled + 持仓；对不上 → failed；读失败 → 自动暂停。
  - 连续 3 次读取失败 → 自动暂停，原因含「客户端可能掉线」。
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 全量测试通过**
- [ ] **Step 5: 提交** `feat(live): executor buy path with fill wait, checked cancel, sending recovery`

---

### Task 4: 执行器——卖出对齐

**Files:** Modify `jevq/live/executor.py`；Create `tests/test_live_sell.py`

**Produces（内部）：** `_to_sell(now) -> list[str]`、`_sell(code, now)`、每只票的下次重试时间 `self._next_sell_try: dict[str, datetime]`、每只票已重挂轮数。

**规则（spec「卖出」）：**
- 待卖 = `live_positions` 里有，且（模拟账户 `LIVE_ACCOUNT` 在 `positions` 表里已不持有该代码，或存在 new 的 `source='remote'` 卖出意图）。买入日是今天的不卖（T+1）。
- 只在 `LIVE_SELL_WINDOWS` 内执行。暂停中不执行（Task 5 负责提醒）。
- `_sell(code)`：读客户端持仓 → `qty = min(程序股数, 客户端可用余额)`；`qty == 0` 且客户端股数也为 0 → 交给对账（Task 5 的两次读数规则），本次跳过。
  - 行情 `q = quotes([code])[code]`；`q.price <= q.limit_down` → 限价 = 跌停价，下单后不撤单，通知一次「跌停排队」（key `limit_down:<code>`），结束本次，下次重试间隔 `LIVE_SELL_RETRY` 且不再重复下单（当日委托里还有该票 open 的卖单就不再下）。
  - 否则限价 `max(round(q.price * (1 - LIVE_SELL_DISCOUNT), 2), q.limit_down)`。
  - 演练：记 `dryrun` 订单，`reduce_position(code, qty)`，结束。
  - 实盘：`sending` → `place('sell', ...)`（异常处理同买入），`_wait_fill(wait=LIVE_SELL_WAIT)`；卖完 → `reduce_position`、订单 `filled`、通知「💰 实盘 卖出 … 盈亏 x%」（相对 `buy_price`）。
  - 没卖完：`now >= LIVE_NO_CANCEL_AFTER` → 不撤，订单留 `submitted`，下次重试时先读当日委托，已有 open 卖单就只更新成交、不重下；否则 `_cancel_last_checked` → 按最终成交数 `reduce_position` → 轮数 +1；轮数 < `LIVE_SELL_ROUNDS` 立即按新行情重挂；达到后设 `_next_sell_try[code] = now + LIVE_SELL_RETRY`，通知一次「卖出没成功，每分钟重试」（key `sell_retry:<code>`）。
  - 下一个交易日：`_next_sell_try`、轮数是内存状态，跨天清零；隔夜仍持有且模拟已不持有 → 09:30 起照常卖。
- 卖出意图（`source='sim'`）在对齐检查后 `done`，note 写「已卖 / 程序没有这只 / 暂停中延后」之一；`source='remote'` 的意图在程序持仓清空后才 `done`（Task 5 接上「然后暂停到收盘」）。

- [ ] **Step 1: 写测试**（`tests/test_live_sell.py`）：模拟账户不再持有 → 卖出、限价 = 最新价×0.99、数量取可用；当天买的不卖；程序没有这只 → 意图 done「程序没有这只」；客户端可用比程序少 → 只卖可用的；跌停 → 挂跌停价、不撤单、只通知一次、下一分钟不重复下单；不成交 → 撤单重挂 3 轮后进入每分钟重试并通知一次；14:58 不撤单、下一分钟不重复下单；隔夜：次日 09:30 卖出；演练：不调 place、持仓减少；午休 12:00 不卖；remote 意图使仍被模拟持有的票也卖出。
- [ ] **Step 2–4: 失败 → 实现 → 全量通过**
- [ ] **Step 5: 提交** `feat(live): state-aligned sells with retries, limit-down queue, no-cancel after 14:57`

---

### Task 5: 执行器——暂停/恢复、立即卖出、对账、自检、小结

**Files:** Modify `jevq/live/executor.py`；Create `tests/test_live_control.py`

**规则（spec「暂停、恢复、立即卖出」「对账」「通知」）：**
- `step()` 顺序（最终形态，原样实现）：心跳 → 模式 off → 交易日门槛 → 处理 `resume_requested`（清掉暂停、先对账、删掉该键）→ 手动暂停到点（`kind=='manual'` 且 `now >= until`）自动恢复（同上，通知「暂停结束」）→ 若仍暂停：new 买入意图 skipped「暂停中」，对每只待卖票通知一次（key `paused_sell:<code>`）「暂停中，X 该卖了，程序没卖」，卖出意图 done「暂停中延后」，返回 → 09:05 后当天第一次：自检（读资金+持仓，通知「自检通过：模式、可用资金、程序持仓」，失败通知「自检失败」；记 `self_check_day`）→ 卖出对齐 → 买入意图 → remote 意图收尾（程序持仓清空 → 意图 done → `pause(今天 15:00, "立即卖出", 'manual')`）→ 定时对账 → 15:02 后当天第一次：收盘小结（今天实盘委托、成交、持仓，和模拟 `LIVE_ACCOUNT` 今天成交的对照），记 `close_summary_day`。
- `step()` 开头若发现 `paused` 刚被写入（页面线程写的），当前这一笔已经在上一轮处理完，所以直接按暂停处理即可——不需要额外同步。
- 暂停状态变化要通知：执行器记住上一轮是否暂停（`self._was_paused`），未暂停 → 暂停时通知「暂停开始（到 HH:MM / 需手动恢复：原因）」，暂停 → 恢复时通知「暂停结束」。
- 对账 `_reconcile(now)`：读客户端持仓（失败按 `_read` 计数）；对每只 `live_positions`：客户端股数 ≥ 程序股数 → 删 `reduce:<code>`；客户端股数 < 程序股数 → 若已有 `reduce:<code>` 且股数相同、时间差 ≥ `LIVE_REDUCE_CONFIRM` → 按客户端股数减账（0 删行），通知「按客户端减账：X 程序账 a 股 → b 股（记为人工卖出）」，删键；否则写/更新 `reduce:<code>`。触发时机：每次 `_buy` 前（已有读持仓，复用结果）、恢复时、startup、有持仓每 `LIVE_RECONCILE_HOLDING` 分钟、空仓每 `LIVE_RECONCILE_IDLE` 分钟读一次资金。
- 暂停中完全不调 broker（测试断言 `calls` 为空）。

- [ ] **Step 1: 写测试**（`tests/test_live_control.py`）：暂停中买入意图 skipped 且 broker 零调用；暂停中出现待卖只通知一次；手动暂停到点自动恢复并先对账再卖；`resume_requested` 生效；自动暂停到点不恢复；立即卖出 → 卖完后暂停到 15:00（kind manual）；对账：一次读到少不减账、隔 60 秒再读到同样的数才减、读到不同的数重新计时；客户端股数多于程序不动；09:05 自检只发一次；15:02 小结只发一次；暂停不跨天（昨天的 until 今天自动失效）。
- [ ] **Step 2–4: 失败 → 实现 → 全量通过**
- [ ] **Step 5: 提交** `feat(live): pause/resume, sell-all, reconciliation, self-check, close summary`

---

### Task 6: 手机页 + `live.py` 入口 + 开机自启

**Files:**
- Create: `jevq/live/web.py`、`live.py`、`tests/test_live_web.py`、`tests/test_live_main.py`
- Modify: `install_autostart.py`（加「Jev实盘执行」启动项：`pythonw live.py`；看现有写法照抄）

**web.py Produces：**

```python
def make_handler(conn_factory, clock=datetime.now, quotes=tencent.fetch_quotes) -> type[BaseHTTPRequestHandler]
def serve(hosts: list[str], port: int, handler_cls) -> list[ThreadingHTTPServer]   # 每个地址一个线程（daemon=True），绑定失败只记日志
def status(conn, now, quotes) -> dict   # 页面数据：mode、paused、last_read_ok、positions（含现价、盈亏%）、to_sell、orders（今天）、skipped（今天）
```

- `GET /` 返回内嵌 HTML（手机宽度可用，16px 边距；JS 每 10 秒 `fetch('/api/status')` 渲染；按钮：暂停 30 分钟 / 1 小时 / 到收盘、恢复、立即卖出（`confirm()` 二次确认）；本机访问时多一组模式按钮）。不显示账户资金和用户自己的持仓。
- `GET /api/status` → `status(...)` 加 `"local": 请求来自 127.0.0.1`。
- `POST /api/pause`：`{"minutes": 30|60}` → until = now+minutes（不超过今天 15:00）；`{"until": "close"}` → 今天 15:00；`store.pause(until, "手机暂停", "manual")`。
- `POST /api/resume` → `set_state("resume_requested", "1")`。
- `POST /api/sell_all` → `store.add_remote_sell(conn, now)`。
- `POST /api/mode`（仅 `client_address[0] == "127.0.0.1"`，否则 403）→ `store.set_mode`。
- 所有 POST：`Content-Type` 不是 `application/json` → 415；有 `Origin` 头且其 host:port 与请求 `Host` 不同 → 403；JSON 解析失败 → 400。

**live.py：** 照 `daemon.py` 的 `bootstrap()`（cwd 切到仓库根、pythonw 下 stdout/stderr 指 devnull）；`hold_run_lock(Path(DB_PATH).parent / LIVE_LOCK_NAME)`，已被占用 → 记日志退出；`make_broker()` 按 `LIVE_BROKER` 返回 `GatewayBroker(LIVE_GATEWAY_URL, token=环境变量或 HKCU 的 XIADAN_GATEWAY_TOKEN)` 或 `EasytraderBroker(LIVE_EASYTRADER_EXE)`（HKCU 兜底复用 `jevq.notify._windows_env_fallback`）；页面监听 `["127.0.0.1"] + [JEVQ_LIVE_HOST（环境变量或 HKCU）]`；`notifier_from_env()`；`Executor(...).startup()`；主循环：`step()`，异常只记 `data/live.log` 并继续；每秒一轮；15:10 之后若 `git rev-parse HEAD` 与启动时不同 → 起新进程（同 daemon `_real_restart`，参数 `--after-deploy`，新进程最多等 60 秒拿锁）后退出。日志写 `data/live.log`（同 daemon 的 `file_log` 写法）。

- [ ] **Step 1: 写测试**：`tests/test_live_web.py` 用真实 `ThreadingHTTPServer` 绑 `127.0.0.1:0` + `requests`（测试内部本机回环，不算联网）或直接调用 handler：暂停/恢复/立即卖出写对状态；非 JSON 415；跨站 Origin 403；`/api/mode` 本机可改（测试里来源就是 127.0.0.1，另测把判断函数抽成 `_is_local(addr)` 单测非本机返回 False）；status 不含资金字段。`tests/test_live_main.py`：`make_broker` 按配置返回类型（用 monkeypatch 替换两个类）；锁被占用时退出码 0 且不建 Executor。
- [ ] **Step 2–4: 失败 → 实现 → 全量通过**
- [ ] **Step 5: 提交** `feat(live): phone page, live.py entry, autostart`

---

### Task 7: daemon 看门狗

**Files:** Create `jevq/live/watchdog.py`、`tests/test_live_watchdog.py`；Modify `daemon.py`（`main()` 里起一个 daemon 线程每 60 秒调一次 `Watchdog.check()`，自己开数据库连接）；Modify `jevq/sync/deploy.py` 的 `CODE_PATHS` 加 `"live.py"`。

```python
class Watchdog:
    def __init__(self, conn, notifier, spawn, kill, alive, clock=datetime.now): ...
    def check(self) -> None
        # mode off → 返回；heartbeat 距今 < LIVE_WATCHDOG_STALE → 返回；
        # 当天重启次数 ≥ LIVE_WATCHDOG_MAX_RESTARTS → notifier.once("live_dead", "💰 实盘 执行进程停了", "…已重启 3 次，不再自动重启，请远程检查") 返回；
        # pid 存在且 alive(pid) → kill(pid)；spawn()；次数 +1；notifier.send("💰 实盘 执行进程重启", 原因「心跳 N 秒没更新」)
def pid_alive(pid: int) -> bool     # ctypes: OpenProcess(0x1000) + GetExitCodeProcess == 259；不要用 os.kill（Windows 上会直接结束进程）
def kill_pid(pid: int) -> None      # subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
def spawn_live(repo: Path, python_exe: Path) -> None   # 同 daemon._real_restart 的 Popen 写法，启动 live.py
```

- [ ] **Step 1: 测试**：off 不动；心跳新鲜不动；心跳旧 + 进程活着 → kill 后 spawn 并通知；3 次后只 once 通知不再 spawn；跨天次数清零。
- [ ] **Step 2–4 / Step 5: 提交** `feat(live): daemon watchdog restarts a stalled live.py`

---

### Task 8: 复盘报告「实盘跟单」段

**Files:** Modify `jevq/report.py`；Create `tests/test_live_report.py`

`live_report_lines(conn, day) -> list[str]`：`live_orders` 从没有过记录且模式为 off → 返回 `[]`。否则输出一段 Markdown：今天模拟 `LIVE_ACCOUNT` 成交几笔 → 实盘成交几笔、跳过几笔（按 `live_intents.note` 分组计数）；每笔实盘成交的滑点%（`(实盘均价/模拟价 - 1)*100`，买卖分开，保留 2 位）；累计收益率：实盘 = 已平仓的每笔 `(卖出均价/买入均价 - 1)` 的连乘 − 1，模拟 = `LIVE_ACCOUNT` 同期（从第一次实盘成交那天起）净值变化。**不出现金额和股数。** `write_report` 在现有段落之后追加这些行。

- [ ] **Step 1: 测试**：没有实盘记录 → 无此段；有记录 → 含笔数、滑点、收益率，且正文里不出现股数和金额（断言成交股数、金额字符串不在输出里）。
- [ ] **Step 2–4 / Step 5: 提交** `feat(live): live-vs-sim section in daily report`

---

### Task 9: 低吸下线（轨道 B）

**Files:** Modify `jevq/config.py`、`jevq/engine.py`、`jevq/rules/buy.py`、`jevq/rules/sell.py`、`run.py`、`jevq/app/api.py`、`jevq/app/ui.html`、`jevq/report.py`、`jevq/notify_content.py`、`jevq/app/home_view.py`（如有引用）、相关测试。先 `grep -rn "lowbuy\|\"main\"\|'main'\|\"shadow\"\|'shadow'\|低吸" jevq run.py daemon.py home_panel.py scripts tests` 列全引用再动手。

**要求（spec「低吸下线」）：**
- `STRATEGIES` 删 `lowbuy`；`ACCOUNT_NAMES` 保留 `"main": "低吸·主(已下线)", "shadow": "低吸·影子(已下线)"` 供历史显示；`builtin_accounts()` 不再含这两个账户（`Ledger` 不再给它们开户）。
- `Engine`：删 `_sells`、`_buy_shadow`、`_buy_main`、`rank_candidates`/`buy_mode`/`decide_sell` 的使用；`accounts` 参数删掉。保留 `sell_step`（只取行情、记成交额，名字不改以免牵动 run.py 的调用顺序）和 `buy_step`（只刷新情绪 `_sentiment`，返回 `[]`；`halted` 时也要刷新情绪吗——保持现状：先算情绪再判断 halted，情绪照常刷新）。`_restore_rejected` 删；`_restore_sentiment` 保留。决策日志里情绪那一行的 `strategy` 仍写 `'lowbuy'`？改为 `'market'`，并让 `_restore_sentiment`、`api.py` 读情绪处兼容旧值（`strategy IN ('lowbuy','market') OR strategy IS NULL`）。
- 删除只被低吸用的函数与配置（`rank_candidates`、`buy_mode`、`decide_sell`、`STOP_LOSS`/`ZERO_LINE`/`REBOUND`/`TAKE_PROFIT`/`LOW_OPEN` 等——先 grep 确认没有其他使用者；`SellDecision`、`in_buy_zone`、`BUY_ZONE_*`、`buy_window_open`、`rebound_mode`、`rank_rebound` 保留）。
- 面板 `builtin_summaries`、决策列表默认 strategy、`ui.html` 里低吸的卡片/标签、推送 `notify_content` 里 `lowbuy` 分支、报告里 `sid or 'lowbuy'` 的兜底：删除或改成对「已下线」历史的只读显示；run.py `_notify_fills` 的 `f.account != "main"` 条件改为只看 `_main` 结尾。
- 数据库历史不删；`exports` 照常导出历史行。
- 测试：删掉纯低吸行为的测试；新增/保留：情绪仍每 `SENTIMENT_REFRESH_MIN` 分钟在买入阶段刷新一次、超跌反弹和扫首板拿得到情绪、`Engine.sell_step` 仍取行情并写 turnover、面板/推送不再出现 `main`/`shadow` 的当日数据。

- [ ] **Step 1: grep 列清单，写/改测试（先让新断言失败）**
- [ ] **Step 2: 实现删除**
- [ ] **Step 3: 全量测试通过**
- [ ] **Step 4: 提交** `refactor: retire the low-buy strategy; engine keeps quotes and sentiment`

---

### Task 10: 自动上线先测后合并（轨道 B）

**Files:** Modify `jevq/sync/deploy.py`、`tests/test_deploy.py`

**规则（spec「自动上线顺序修复」）：** 流程改为：提交数据 → fetch → 若 `running_commit` 为 None 或 `git diff --name-only <running_commit> origin/master -- CODE_PATHS` 非空：在临时 worktree `worktree add --detach <dir> origin/master` 里跑测试；不通过 → 通知「新代码测试未通过（N failed），继续使用旧版本 xxx，今天的数据没推送」，返回 `{"deploy": False}`，**不 rebase、不 push**；通过 → rebase → push → 返回 `{"deploy": True, "commit": <rebase 后 HEAD>}`。代码没变 → rebase → push → `{"deploy": False}`。`code_dirty` 时维持原行为（不上线；数据照推）。worktree 用完照旧 `remove --force`。`CODE_PATHS` 加 `"live.py"`（若 Task 7 已加则跳过）。

- [ ] **Step 1: 测试**：测试失败时 `calls` 里没有 `rebase`、没有 `push`；worktree 基于 `origin/master`；测试通过才 rebase + push 且 deploy True；代码没变不跑测试；原有「绝不出现破坏性操作」断言保留。
- [ ] **Step 2–4 / Step 5: 提交** `fix(deploy): test origin/master before rebasing the running checkout`

---

### Task 11: 合并、交接、推送

- [ ] 在 `D:\claude\jev-quant-live`：`git merge --no-ff lowbuy-removal`、`git merge --no-ff live-web`，解决冲突（`config.py`、`run.py`、`report.py` 最可能）。
- [ ] 全量测试通过。
- [ ] 更新 `docs/HANDOFF.md`：两台机器分工（家里跑实盘、公司停）、`live.py` 与网关、模式切换方法、红线（实盘模式只能本机切；交易时段不重启 live.py 以外的后台）、待办（演练 3 天、实盘前报备）。
- [ ] 推送前密钥检查（PowerShell：HKCU 的 `TYPESAFE_API_KEY`、`PUSHPLUS_TOKEN`、`XIADAN_GATEWAY_TOKEN` 与 `git log origin/master..live-trading -p` 做包含比较，只输出 True/False），全 False 才 `git push origin live-trading`。
