# Task 4 brief — 执行器卖出对齐（controller 定稿，含裁定）

先读：spec `docs/specs/2026-09-29-live-trading-design.md`「卖出（按状态对齐）」「撤单」「模式」；plan `docs/plans/2026-09-29-live-trading.md` Task 4；现有 `jevq/live/executor.py`（买入路径、`_wait_fill` / `_cancel_and_confirm` / `_cancel_last_checked` / `_reconcile_open_order_row` / `_read` / `_notify` / `_auto_pause`）、`jevq/live/store.py`、`tests/live_fakes.py`、`tests/test_live_buy.py`（测试写法照抄它的 helper）。

## 两条底线（和买入一样，不许破）
1. 委托终态只在「读成功 + 这笔不再 open」时才落；读不到/还 open 就维持 submitted，按下面规则处理，不许猜。
2. 一次卖出尝试最多一次 `place()`；`add_order(sending)` 之后 `place()` 抛什么都不能让同一次尝试再下单。

## 结构
- 把 `_wait_fill` 拆成 `_poll_fill(code, side, price, shares, entrust_no, since_ts, wait) -> (matched, closed)`（只轮询，按时钟截止，逻辑同现在 `_wait_fill` 的前半）和 `_wait_fill` = `_poll_fill` + 没 closed 时 `_cancel_and_confirm`。买入行为不变（买入测试一条都不许改坏）。
- 新增内部状态：`self._next_sell_try: dict[str, datetime]`、`self._sell_rounds: dict[str, int]`、`self._sell_day: date | None`；`now.date() != self._sell_day` 时两个 dict 清空（跨天清零）。
- `step()` 本任务顺序：心跳 → 模式 off → 交易日门槛 → **若暂停中：直接返回（不碰券商；暂停期间的意图/提醒是 Task 5 的事，本任务只保证暂停时零 broker 调用、不处理意图）** → 卖出意图备注（见下）→ `_align_sells(now)` → 买入意图逐条 `_buy` → 收盘后结算（见下）。
  - 注意现在 `_buy` 里有「暂停中 → skipped」检查，保留它（Task 5 会改 step 暂停分支）。暂停时 step 提前返回，买入意图留 new（Task 5 会把它们 skipped「暂停中」）。
- 意图处理：`source='sim'` 的卖出意图在 `_align_sells` **之前**逐条 `finish_intent(done)`，note 三选一：代码不在 `live_positions` →「程序没有这只」；`buy_date == today` →「当天买的，下个交易日卖」；否则「按持仓对齐卖出」。（裁定：原计划的「已卖」改成描述决策；结果看 live_orders。）`source='remote'` 的意图**保持 new**，本任务不 finish（Task 5 收尾）。

## 待卖 `_to_sell(now) -> list[str]`
`live_positions` 里 `buy_date < today` 的代码，且满足其一：模拟账户 `config.LIVE_ACCOUNT` 在 `positions` 表（`account, code, shares…`）里没有这只（或 shares ≤ 0）；或存在 `status='new' AND source='remote' AND side='sell'` 的意图。按代码排序。

## `_align_sells(now)`
不在 `LIVE_SELL_WINDOWS`（09:30–11:30、13:00–15:00，左闭右开）→ 返回。对每只待卖：`_next_sell_try.get(code)` 在 now 之后 → 跳过；否则 `_sell(code, now)`。

## `_sell(code, now)` —— 每个没把这只卖完的出口都要设 `_next_sell_try[code] = now + LIVE_SELL_RETRY`（网关一次读要约 6 秒，不许每秒狂读），除非是「立即重挂下一轮」。
1. **先看有没有挂着的程序卖单**：本地 `live_orders` 里今天这只 `side='sell' AND status='submitted'` 的行存在 → 读当日委托（读失败：设 next try，返回），按该行委托号（没有就四要素 + 时间）找到它：
   - 找到且 still open → 按成交增量更新（见「成交入账」），设 next try，返回（不重下）。
   - 找到且不 open → 按终态结算（见「成交入账」），继续往下走（剩余股数可能还要卖）。
   - 没找到 → 行 note「当日委托里找不到」，`_auto_pause("卖单核对不上：…")`，返回。
2. 模式 `live`：读客户端持仓（`_read`，失败：设 next try，返回）。`qty = min(程序股数, 客户端可用余额) // 100 * 100`（下单层只收 100 的倍数）。`qty == 0` → 设 next try，返回；其中程序股数 > 0、可用 > 0 但不足 100（零股）→ 通知一次（key `odd_lot:<code>`）「X 剩 N 股零股，程序卖不了，请手动卖出」。客户端股数为 0 的情况交给 Task 5 对账减账。
   模式 `dryrun`：**不按客户端可用封顶**（演练持仓客户端里本来就没有），`qty = 程序股数`，也不读客户端持仓。
3. 行情：`q = self.quotes([code]).get(code)`；异常、None、`q.price <= 0`、`q.limit_down <= 0`（停牌等）→ 通知一次（key `sell_quote:<code>`）「X 取不到有效行情，暂不卖」，设 next try，返回。
4. 跌停：`q.price <= q.limit_down + 0.005` → `price = q.limit_down`，`queue = True`；否则 `price = max(round(q.price * (1 - LIVE_SELL_DISCOUNT), 2), q.limit_down)`。
5. 演练：`add_order(status='dryrun')`，`reduce_position(code, qty)`，通知「卖出（演练）」（可选），返回。
6. 实盘：`add_order(sending)` → `place('sell', code, price, qty)`：
   - REJECT_CODES → 行 `failed`（note 错误），通知一次（key `sell_reject:<code>`），设 next try，返回。**不暂停。**
   - INFLIGHT_CODES → 行保持 `sending`，note，`_auto_pause("卖出结果不明：…")`，返回。
   - 其他异常 → 单笔核对：读当日委托（失败 → `_auto_pause`，返回）；四要素 + 时间找不到 → 行 `failed` + `_auto_pause("下单层返回认不出的错误：…")`，返回；找到 → 按 submitted + 委托号继续下面的流程。
   - 返回委托号 → 行 `submitted` + `entrust_no`。
7. `queue`（跌停排队）：不等不撤；通知一次（key `limit_down:<code>`）「X 跌停，挂跌停价排队」；设 next try，返回（之后走第 1 步只更新成交）。
8. `_poll_fill(..., wait=LIVE_SELL_WAIT)`：closed → 结算，卖完就 `_sell_rounds.pop(code)` 返回；没卖完（部分成交后被撤等）按第 9 步继续。
9. 没 closed：
   - `now_after_wait >= LIVE_NO_CANCEL_AFTER`（14:57）→ 不撤；按成交增量更新，行留 `submitted`，设 next try，返回（收盘集合竞价；之后第 1 步跟踪，收盘后结算收尾）。
   - 否则 `_cancel_and_confirm(...)`：confirmed=False → 行 note「委托状态查不到」+ `_auto_pause("委托状态查不到：…")`，返回；confirmed → 按终态结算，`_sell_rounds[code] += 1`；程序还有这只：轮数 < `LIVE_SELL_ROUNDS` → 立即回到第 2 步重挂（重新读客户端持仓和行情）；否则设 next try，通知一次（key `sell_retry:<code>`）「X 卖出没成功，之后每分钟重试」，返回。之后每分钟的重试各算一轮（下单、等 20 秒、撤）。

## 成交入账（卖单）
- 增量：`delta = 读到的 filled − 行里已记的 filled`；`delta > 0` → `reduce_position(code, delta)`，`update_order(filled=..., avg_price=...)`。
- 终态：先做增量，再写 status：`filled`（filled ≥ shares）/ `partial`（>0）/ `cancelled`（0）。有成交就通知「卖出 名称 N股 @均价，盈亏 x%」（x 相对 `live_positions.buy_price`，结算前先取出来，round 2；名称用最近一条该代码的 live_intents.name，没有就用代码）。

## 收盘后结算（`step()` 末尾）
`now.time() >= 15:00` 且今天还有 `side='sell' AND status='submitted'` 的行，且今天还没成功结算过（状态键 `sell_settle_day`）：读一次当日委托（失败就下一秒再试，按 `_read` 计数）；逐行按委托号（或四要素）找到 → 做增量入账；**15:00 后成交数已是最终值**，所以直接写终态（filled/partial/cancelled），没卖完的 note「收盘未成交部分作废」；找不到 → 不许猜：保持 submitted，note「收盘结算找不到这笔」，`_auto_pause("收盘结算找不到卖单：…")`。读成功并逐行处理完就写 `sell_settle_day=today`。

## 启动核对（改 `_reconcile_open_order_row`）
- 卖出行：closed → 增量 `reduce_position` + 终态；**still open → 不撤单**，只做增量入账，行留 `submitted`（交给 `_sell` 第 1 步跟踪）。
- 买入行保持现有行为。
- 意图收尾保持现有行为（`_finish_row_intent`）。

## 测试 `tests/test_live_sell.py`（每条一个测试；FakeBroker 可按需小改，保持现有测试全绿）
模拟账户不再持有 → 卖出，限价 = round(最新价×0.99, 2)，数量 = 客户端可用，行 filled，程序持仓清掉，通知含「卖出」「盈亏」；当天买的不卖（无 place）；卖出意图对程序没有的票 → done「程序没有这只」；客户端可用 < 程序股数 → 只卖可用；跌停 → 挂跌停价、不 cancel、只通知一次、下一分钟不重复下单、之后成交 → 持仓减少；不成交 → 撤单重挂 3 轮后进入每分钟重试且只通知一次、这一分钟内不再下单；部分成交后撤 → 按成交减仓，剩余重挂；14:58 不撤单、下一分钟不重复下单；15:01 收盘结算把部分成交落账、行终态；隔夜：昨天的持仓今天 09:25 不卖、09:30 卖；演练：不调 place、行 dryrun、持仓减少（客户端里没有这只也照样）；午休 12:00 不卖；remote 意图让模拟仍持有的票也卖出、remote 意图仍是 new；拒单（INSUFFICIENT_SHARES）→ failed、不暂停、60 秒内不再 place；INFLIGHT → sending + 自动暂停；暂停中 → broker 零调用；停牌/价格为 0 → 不下单、通知一次；异步撤单（`cancel_delay_polls=2`）→ 正常重挂不暂停；启动核对：挂着的卖单不撤、成交增量减仓、行仍 submitted；跨天轮数清零。

## 要求
- 常量从 `jevq.config` 取；中文注释，照现有风格（注释少、命名清楚）；不加没要求的功能。
- 全量测试：`/tmp/claude-0/-home-user-jev-quant/18fe0662-3f47-5990-906e-7725334eae76/scratchpad/venv/bin/python -m pytest -q -p no:cacheprovider`（仓库根目录跑），必须全绿。先写测试确认失败，再实现。
- 提交（只 `git add` 自己改的文件）：`feat(live): state-aligned sells with retries, limit-down queue, no-cancel after 14:57`，结尾两行：
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` 和 `Claude-Session: https://claude.ai/code/session_01JWDqzhRepN43vPUt55HCnr`。不要 push，不做 reset/rebase/force。
