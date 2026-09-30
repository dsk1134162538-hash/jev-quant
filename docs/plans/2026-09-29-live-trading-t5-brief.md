# Task 5 brief — 暂停/恢复、立即卖出、对账、自检、收盘小结（controller 定稿，含裁定）

先读：spec `docs/specs/2026-09-29-live-trading-design.md`「暂停、恢复、立即卖出」「对账」「模式」「通知」；plan `docs/plans/2026-09-29-live-trading.md` Task 5；Task 4 brief `docs/plans/2026-09-29-live-trading-t4-brief.md`；现有 `jevq/live/executor.py`（Task 3 买入 + Task 4 卖出对齐都已在里面）、`jevq/live/store.py`、`tests/live_fakes.py`、`tests/test_live_buy.py`、`tests/test_live_sell.py`。

两条底线照旧：终态不许猜；一次尝试最多一次 `place()`。另外：**暂停中完全不调 broker**（不下单、不撤单、不查询）。

## store.py
- `pause()` / `clear_pause()` 改成所有键写完只 `commit` 一次（页面线程和执行器共用同一个连接，避免读到写了一半的暂停状态）。行为不变，现有测试保持绿。

## step() 最终顺序（原样实现）
1. 心跳、pid。
2. 模式 `off` → 所有 new 意图 skipped「模式关闭」，返回。
3. 交易日门槛 → skipped「非交易日」，返回。
4. `resume_requested == '1'` → 删掉该键；若在暂停中：`clear_pause` 后执行 `_after_resume(now)`。
5. 手动暂停到点：`kind == 'manual'` 且 `until` 不为 None 且 `now >= until` → `clear_pause` + `_after_resume(now)`。（昨天的 until 今天自然失效；自动暂停 `until=None` 永远不自动恢复。）
6. 暂停状态变化通知（`self._was_paused`，构造时按数据库当前状态初始化，重启不重复通知）：未暂停→暂停：`kind == 'manual'` 时通知「暂停开始（到 HH:MM）：原因」；`kind == 'auto'` 不再通知（`_auto_pause` 已经发过）。暂停→未暂停：通知「暂停结束」。
7. 仍在暂停：new 的 sim 买入意图 skipped「暂停中」；sim 卖出意图 done「暂停中延后」；remote 意图保持 new，并通知一次（key `paused_sell_all`）「暂停中收到立即卖出，恢复后执行」；对 `_to_sell(now)` 里每只票通知一次（key `paused_sell:<code>`）「暂停中，X 该卖了，程序没卖」。**返回，零 broker 调用。**
8. 自检：`now.time() >= LIVE_SELF_CHECK` 且 `self_check_day != today` → 先写 `self_check_day=today`，再读资金 + 客户端持仓（`_read`）；成功通知「自检通过：模式 X，可用资金 Y 元，程序持仓 …（无则「空仓」）」；任一读取失败通知「自检失败：…」。
9. Task 4 的卖出意图备注 + `_align_sells(now)`。
10. 买入意图逐条 `_buy`。
11. remote 意图收尾：有 new 的 remote 意图且 `live_positions` 为空 → 全部 done「已卖完」，`pause(今天 LIVE_PAUSE_CLOSE, "立即卖出", "manual")`。程序持仓还在（含当天买的卖不了）→ 保持 new；当天买的那只通知一次（key `sell_all_t1:<code>`）「X 今天买的，下个交易日开盘卖」。
12. 定时对账（只在 `LIVE_SELL_WINDOWS` 内）：程序有持仓 → 距上次 ≥ `LIVE_RECONCILE_HOLDING` 分钟做 `_reconcile(now)`；空仓 → 距上次 ≥ `LIVE_RECONCILE_IDLE` 分钟 `_read(broker.balance)` 确认在线（失败只计数）。上次时间放内存（`self._last_reconcile`），启动时为 None（立即做一次）。
13. Task 4 的收盘后结算。
14. 收盘小结：`now.time() >= LIVE_CLOSE_SUMMARY` 且 `close_summary_day != today` 且收盘后结算已完成（今天没有 submitted 卖单，或 `sell_settle_day == today`）→ 写 `close_summary_day`，通知「收盘小结」：今天实盘委托笔数、成交笔数、跳过笔数（按 note 分组）、程序持仓（代码、股数、成本价）、模拟 `LIVE_ACCOUNT` 今天成交笔数（`fills` 表）。不读 broker。

`_after_resume(now)`：先做 sending/submitted 核对（现有 `_reconcile_sending_orders`，读不到 → 它自己会自动暂停，然后直接返回），再 `_reconcile(now)`；之后本轮继续往下走（卖出对齐等）。

## startup()
- 暂停中 → 什么都不做（零 broker 调用；恢复时会核对）。
- 否则：今天有 sending/submitted 行 → 核对（现有逻辑，不论模式）；然后模式不是 off → `_reconcile(now)`。
- 更早日期遗留的 sending/submitted 行不动（它们的成交以对账为准）。

## `_reconcile(now, holdings=None)`
- `holdings` 为 None 时 `_read(broker.positions)`（失败直接返回，不改账——计数由 `_read` 处理）。
- 模式 `dryrun`：只读不改账（演练持仓客户端里本来就没有）。
- 对每只 `live_positions`：今天这只有 `sending`/`submitted` 委托 → 跳过（交给委托跟踪，避免重复减账）。客户端股数（`Holding.shares`，没有这只按 0）≥ 程序股数 → 删 `reduce:<code>`。客户端 < 程序：已有 `reduce:<code>` 且 shares 相同且 `now - ts >= LIVE_REDUCE_CONFIRM` 秒 → 按客户端股数减账（0 删行），通知「按客户端减账：X 程序账 a 股 → b 股（记为人工卖出）」，删键；已有但 shares 不同 → 用新数重新计时；没有 → 写 `reduce:<code>` = JSON `{"shares": n, "ts": iso}`。
- 客户端股数多于程序：不动（多出的是用户的）。
- `_buy` 里已经读到的客户端持仓、`_sell` 里读到的客户端持仓都顺手传进来复用（下单前对账）；**`_sell` 调用前后别让刚记的成交和对账重复减**——上面「有 sending/submitted 委托的票跳过」已经保证。

## 测试 `tests/test_live_control.py`
暂停中买入意图 skipped 且 broker 零调用；暂停中待卖只通知一次、不下单；暂停中 remote 意图保持 new；手动暂停到点自动恢复、先核对/对账再卖（`calls` 顺序：positions 读在 place 之前）；`resume_requested` 生效并删键（自动暂停也能这样恢复）；自动暂停到点不恢复；暂停开始/结束各通知一次（手动），自动暂停不重复通知；立即卖出 → 卖完后暂停到 15:00（kind manual）、意图 done；立即卖出时程序空仓 → 立刻 done + 暂停到收盘；立即卖出遇到当天买的 → 意图保持 new、通知一次；对账：一次读到少不减账、隔 60 秒再读到同样的数才减、读到不同的数重新计时、客户端多于程序不动、有 submitted 卖单的票不对账、演练模式不改账；定时对账间隔（有持仓 5 分钟、空仓 15 分钟读资金）；午休不对账；09:05 自检只发一次、失败也只发一次；15:02 小结只发一次且不调 broker；暂停不跨天（昨天 until 的手动暂停今天自动失效）；startup 暂停中零调用；startup 未暂停时核对 + 对账；`store.pause` 一次提交后 `paused()` 往返正确。

## 要求
- 常量从 `jevq.config` 取；中文注释照现有风格；不加没要求的功能。
- 全量测试：`/tmp/claude-0/-home-user-jev-quant/18fe0662-3f47-5990-906e-7725334eae76/scratchpad/venv/bin/python -m pytest -q -p no:cacheprovider`（仓库根目录跑），必须全绿。先写测试确认失败，再实现。
- 提交（只 `git add` 自己改的文件）：`feat(live): pause/resume, sell-all, reconciliation, self-check, close summary`，结尾两行：
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` 和 `Claude-Session: https://claude.ai/code/session_01JWDqzhRepN43vPUt55HCnr`。不要 push，不做 reset/rebase/force。
