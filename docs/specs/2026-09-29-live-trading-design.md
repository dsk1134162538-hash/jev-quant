# 实盘跟单设计（超跌反弹 → 东亚前海真实账户）

日期：2026-09-29。设计已和用户逐段确认（架构、下单与对账、页面/通知/上线）。

## 目标

模拟账户 `rebound_main`（超跌反弹·主）每成交一笔，家里电脑上的程序在东亚前海证券的真实账户里跟一笔。单只最多 2 万。真实账户和用户手动交易混用。用户可以在手机上暂停、恢复、立即卖出程序的持仓。低吸策略同时下线。

不做：QMT；多策略跟单；程序自动登录客户端（交易密码不进程序）；实盘自己做买入决策（一律跟模拟盘）。

## 上线顺序

0. 家里实测：`scripts/live_probe.py` 只读读取 20 次看成功率和耗时；用户本人在交易时段跑一次「跌停价挂 100 股再撤」。两个下单层（xiadan-gateway、easytrader）都试，东亚前海客户端上读得准的那个进 `config.LIVE_BROKER`。
1. 搬家：公司后台停掉，`data/jevq.sqlite` 拷到家里；家里专用运行目录 `E:\jev-live`（只跑不改），开发仍在 `E:\claude\jev-quant`。
2. 演练 3 个交易日（节后 10-08、10-09、10-12）。
3. 实盘，最早 10-13。开之前用户问客户经理程序化交易是否需要报备。

## 架构

```
daemon.py（模拟盘）
   │ rebound_main 成交
   ▼
live_intents ──► live.py（唯一操作真实账户的进程）──► 下单层 ──► 同花顺 xiadan.exe
                    ▲        │
手机 ─Tailscale─► 页面       └─► live_orders / live_positions / live_state
```

- `daemon.py`：模拟盘逻辑不动。`run.py` 的 `_run_minute` 推送成交时，把 `LIVE_ACCOUNT` 的成交写进 `live_intents`（卖出在买入阶段开始前写）。daemon 主循环每分钟检查一次 live.py 心跳（仅当模式不是「关闭」）：心跳超过 `LIVE_WATCHDOG_STALE` 秒没更新 → 微信报警，进程还在就结束它，然后重新拉起；每天最多 `LIVE_WATCHDOG_MAX_RESTARTS` 次，超过只报警。
- `live.py`（新）：唯一操作真实账户的进程。开机自启（和 daemon 一样的启动项）。约每秒扫一次意图和状态；下单、对账、暂停都在这里；同时提供手机页面。每天 15:10 以后，若 `git rev-parse HEAD` 和启动时的提交不同，就自我重启（同 daemon 的 `_real_restart` 做法）。
- 下单层 `jevq/live/broker.py`：一个小接口，三个实现。
  - `GatewayBroker`：调本机 xiadan-gateway 的 HTTP 接口（首选）。网关单独开机自启，开 token 认证，token 放在 HKCU 环境变量 `XIADAN_GATEWAY_TOKEN`，不打印、不入库。
  - `EasytraderBroker`：在 live.py 进程内调 easytrader 的 `universal_client`（备选）。
  - `FakeBroker`：测试用，可设定立即成交/部分成交/不成交/读取失败/拒单。
  - 真实实现只在家里实测和演练中验证；测试不导入 easytrader，不连网关。

接口（所有读取失败抛 `BrokerError`，不返回半真半假的数据）：

```python
balance() -> float                               # 可用资金
positions() -> dict[str, Holding]               # code -> Holding(shares 股票余额, available 可用余额)
place(side, code, price, shares) -> str | None  # 委托号；拿不到号返回 None（不代表失败）
orders_today() -> list[OrderRow]                # 当日委托：委托号、代码、方向、价格、数量、成交数量、状态、时间
cancel_last() -> bool                           # 撤最后一笔
```

## 数据（`data/jevq.sqlite`，这些表不导出、不进报告金额）

- `live_intents(id, ts, source, side, code, name, sim_price, sim_shares, reason, status, note, handled_ts)`：`source` 为 `sim`（模拟盘）或 `remote`（手机「立即卖出」）；`status`：`new` / `done` / `skipped` / `expired`，`note` 写原因。
- `live_orders(id, intent_id, ts, side, code, price, shares, filled, avg_price, entrust_no, status, note, updated_ts)`：`status`：`sending` / `submitted` / `filled` / `partial` / `cancelled` / `failed` / `dryrun`。
- `live_positions(code, shares, cost, buy_price, buy_date, sim_buy_price)`：只记归程序管的票。
- `live_state(key, value)`：`mode`、`paused_until`、`pause_reason`、`pause_kind`（`manual` / `auto`）、`heartbeat`、`pid`、`commit`、`last_read_ok`、`read_fail_streak`、减账待确认数据等。

防重复下单：先写 `live_orders` 一行 `sending`，再调 `place`。live.py 启动时，所有 `sending` 的行拿当日委托核对（代码、方向、数量、价格、时间不早于 `ts`）：对上 → 补记为 `submitted` 继续跟踪；对不上 → `failed`；读不到当日委托 → 自动暂停（需手动恢复）。

## 规则

### 买入（只由 `source=sim` 的买入意图触发）

依次检查，任一不满足 → 意图 `skipped` 并写原因：
1. 模式为「演练」或「实盘」；
2. 意图产生不超过 `LIVE_INTENT_TTL`（60 秒），否则 `expired`；
3. 时间在 09:30–11:30 或 13:00–14:57；
4. 没在暂停；
5. 程序没有持仓（`LIVE_MAX_POSITIONS = 1`）；
6. 这只票不在用户的手动持仓里（客户端持仓里有、`live_positions` 里没有）；
7. 今天程序的买入委托少于 `LIVE_MAX_BUYS_PER_DAY`（2）笔。

金额：`min(可用资金, LIVE_BUDGET)`，按限价和 `ledger.buy_fee` 算股数，再留 `LIVE_CASH_BUFFER`（0.2%）余量，不足 100 股跳过。

限价：`min(round(模拟成交价 × (1 + LIVE_BUY_SLIP), 2), 涨停价)`，`LIVE_BUY_SLIP = 0.005`。等 `LIVE_BUY_WAIT`（30 秒），期间查当日委托看成交；没全部成交 → 撤单，已成交的部分入 `live_positions`。不追、不重下。

### 卖出（按状态对齐）

`live_positions` 里有、而模拟账户 `LIVE_ACCOUNT` 已经不持有的票 = 待卖。暂停延后的卖单、隔夜没卖掉的、live.py 重启后遗留的都走这一条。`source=remote` 的意图（手机「立即卖出」）把程序全部持仓标成待卖。

- 数量：`min(live_positions.shares, 客户端可用余额)`。当天买的不卖（T+1）。
- 限价：`max(round(最新价 × (1 − LIVE_SELL_DISCOUNT), 2), 跌停价)`，`LIVE_SELL_DISCOUNT = 0.01`。最新价用 `jevq.data.tencent.fetch_quotes` 现取。
- `LIVE_SELL_WAIT`（20 秒）没卖完 → 撤单、按最新价重挂，最多 `LIVE_SELL_ROUNDS`（3）轮；仍没卖完 → 之后每分钟重试，并微信报警（每只每天报一次）。
- 已跌停（最新价 ≤ 跌停价）：挂跌停价排队，不撤单。
- 14:57 以后不撤单：挂出的卖单留给收盘集合竞价；收盘仍没卖掉，下一个交易日 09:30 先卖它。

### 撤单

只用「撤最后一笔」。撤之前读当日委托，确认最后一笔可撤的委托是程序刚下的这笔（代码、方向、数量、价格一致）；不是 → 不撤，自动暂停并报警（说明用户在同一时间手动下了单）。

### 暂停、恢复、立即卖出

- 暂停：手机页选 30 分钟 / 1 小时 / 到收盘，写 `paused_until`。当前这一笔处理完就停，之后完全不碰客户端（不下单、不撤单、不查询）。不跨天。
- 暂停期间：买入意图 `skipped`（原因「暂停中」）；每出现一只新的待卖票立刻微信提醒。
- 恢复：到点自动恢复（`pause_kind=manual`）或手动恢复。恢复时先对账，再处理待卖。
- 自动暂停（`pause_kind=auto`，原因写清楚）：意料之外的情况一律自动暂停，**只能手动恢复**——委托状态查不到、撤单前核对不上、`sending` 核对不上、连续 `LIVE_READ_FAIL_PAUSE`（3）次读取失败、下单层返回认不出的错误。
- 立即卖出：写一条 `source=remote` 的卖出意图，按卖出规则卖完后暂停到收盘（`pause_kind=manual`）。

### 对账

- 时机：每次下单前、恢复时、启动时；有持仓每 `LIVE_RECONCILE_HOLDING`（5）分钟一次；空仓每 `LIVE_RECONCILE_IDLE`（15）分钟读一次资金确认在线。暂停中不对账。
- 客户端股数 ≥ 程序账：正常，多出的是用户的。
- 客户端股数 < 程序账：连续两次读到同一个数、间隔不少于 1 分钟 → 按客户端减账（0 则删行），记为「人工卖出」，微信告知。
- 读取失败：不改账；`read_fail_streak` 到 3 → 自动暂停，报警「客户端可能掉线」。

### 模式

- `off`（关闭，默认）：什么都不做，也不报心跳缺失。
- `dryrun`（演练）：照常读客户端、算委托，`live_orders` 记 `dryrun`，不调 `place` / `cancel_last`。演练的「持仓」按模拟成交价记入 `live_positions`，卖出对齐照常推演。
- `live`（实盘）：真下单。
- 模式只能在家里电脑本机（请求来自 127.0.0.1）切换；从 `dryrun` 切到 `live` 时清空演练留下的 `live_positions`。

### 硬上限（`jevq/config.py`，改就是改代码）

`LIVE_ACCOUNT = "rebound_main"`、`LIVE_BUDGET = 20_000`、`LIVE_MAX_POSITIONS = 1`、`LIVE_MAX_BUYS_PER_DAY = 2`，只做 60/00 开头的主板代码，以及上面各节的时间、价格参数。

## 手机页（live.py 内置）

- 标准库 `http.server.ThreadingHTTPServer`，只监听 `127.0.0.1` 和环境变量 `JEVQ_LIVE_HOST`（家里电脑的 Tailscale 地址）两个地址，端口 `LIVE_HTTP_PORT`（8766）。
- `GET /`：页面，每 10 秒刷新。显示模式、暂停状态（到几点、原因）、客户端最后一次读成功时间、归程序管的持仓（股数、成本、现价、盈亏%）、待卖、今天的委托、今天被跳过的信号。不显示账户资金和用户自己的持仓（看同花顺 App）。
- `GET /api/status`：同上数据的 JSON。
- `POST /api/pause`（`{"minutes": 30|60}` 或 `{"until": "close"}`）、`POST /api/resume`、`POST /api/sell_all`（页面上二次确认）、`POST /api/mode`（`{"mode": "off"|"dryrun"|"live"}`，仅本机）。
- 所有 POST 只接受 `Content-Type: application/json`，带 `Origin` 且与 Host 不一致的一律拒绝。不设密码：Tailscale 网络里只有用户自己的设备。

## 通知（现有 PushPlus `Notifier`，标题前缀「💰 实盘」）

实盘成交（附与模拟价的差 %）；买入没成交或部分成交已撤；卖出失败重试中 / 跌停排队 / 收盘没卖掉；暂停开始、结束；暂停中出现待卖；按客户端减账；自动暂停（附原因）；每个交易日 09:05 自检（在线、可用资金、程序持仓，通过也发）；收盘后实盘小结；心跳断（daemon 发）。

## 复盘报告

`jevq/report.py` 加「实盘跟单」一段：今天信号数、实际成交、跳过（按原因）、每笔滑点%、实盘和模拟 `rebound_main` 的累计收益率。只写百分比和笔数，不写金额、股数（报告会推到 GitHub）。模式为 `off` 且从没跑过实盘时不出这一段。

## 低吸下线

- `STRATEGIES` 去掉 `lowbuy`；`Engine` 只保留共享职责：取行情（`_fetch`）、记成交额、市场情绪（仍在买入阶段刷新，保持「慢 Jev 拖不住卖出」）、涨跌停统计、行情失败停买。删掉低吸自己的排序、买入、卖出和相关规则函数，以及只被低吸用的配置项。
- 数据库里 `main` / `shadow` 的历史（现金、成交、净值、决策）保留不删；面板和推送里不再出现这两个账户，历史成交的显示名保留「低吸·主（已下线）」「低吸·影子（已下线）」。上线那天如果这两个账户还有持仓，就原样冻结。
- 面板、复盘报告、推送、导出里对低吸的引用一并清掉；相关测试删改。

## 自动上线顺序修复（HANDOFF 待办 2）

`jevq/sync/deploy.py`：`fetch` 之后，若 `origin/master` 的代码和运行中的提交不同，先在临时 worktree 里测 `origin/master`；通过才 `rebase` 并按原逻辑上线；不通过 → 不 rebase、当天数据不推送，微信提醒（本地数据提交留到下次成功时一起推）。代码没变化时照旧 rebase + 推送，不跑测试。

## 测试

- `FakeBroker` + 注入时钟，覆盖上面每条规则：买入各跳过原因、股数计算、限价、30 秒撤单与部分成交；卖出对齐、重挂 3 轮、跌停不撤、14:57 后不撤、隔夜先卖；撤单前核对；暂停跳过买入与待卖提醒、恢复先对账、到点自动恢复、自动暂停只能手动恢复；立即卖出后暂停到收盘；两次读数才减账、连续 3 次失败自动暂停；`sending` 启动核对三种结果；演练不调 `place`；模式只能本机切换；HTTP 各接口与跨站拒绝。
- daemon：只把 `LIVE_ACCOUNT` 的成交写成意图；看门狗重启次数上限；模式 `off` 时不报警。
- 部署顺序：测试失败不 rebase。
- 全量测试不联网、不碰 `data/`、`reports/`，不导入 easytrader。
