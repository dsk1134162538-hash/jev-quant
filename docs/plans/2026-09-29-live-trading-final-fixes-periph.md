# 终审修复 · 外围部分（sonnet 实现）

依据：`docs/plans/2026-09-29-live-trading-final-review.md`（先读它对应的条目）。执行器 `jevq/live/executor.py` 由 controller 同时在另一个分支修，**本任务不要碰 executor.py 和 `store.set_mode`**。

在 worktree `/tmp/claude-0/-home-user-jev-quant/18fe0662-3f47-5990-906e-7725334eae76/scratchpad/wt-periph`（分支 `final-fixes-periph`）里做。每一条先写测试确认失败，再改。

1. **I-3 页面独立连接 + 暂停原子写**
   - live.py 给页面的 `conn_factory` 每次返回新连接：`sqlite3.connect(DB_PATH, timeout=15)`（库已是 WAL，不用再跑 SCHEMA）；web.py 每个请求用完关掉自己拿到的连接。测试里仍可注入共享的内存连接（比如 `make_handler(..., close_conn=False)` 默认不关、live.py 传 True；或别的等价写法）。
   - `jevq/live/store.py` 新增 `pause_unless_auto(conn, until, reason) -> dict | None`：`BEGIN IMMEDIATE` 里读当前暂停，是 auto 就回滚并返回它；否则写 manual 暂停（四个键）并提交，返回 None。`/api/pause` 改用它，auto 时 409（现有文案不变）。测试：两个文件库连接模拟「页面检查后、写入前执行器自动暂停」不会被覆盖（至少测：已是 auto → 拒绝；BEGIN IMMEDIATE 期间另一连接的写会等待）。
2. **Minor 4 恢复要对得上页面看到的暂停**：页面 JS 点「恢复」时带上当前显示的 `paused.reason`（`{"reason": ...}`）；服务端：当前在暂停且 `reason` 和当前暂停原因不同 → 409「暂停状态刚变了：<当前原因>，请看清后再点恢复」；相同或当前没暂停 → 照旧写 `resume_requested`。
3. **Minor 3 Host 白名单（防 DNS rebinding）**：`make_handler(..., allowed_hosts=None)`；不为 None 时，请求 `Host` 去掉端口后的主机名不在集合里 → 所有方法 403。live.py 传 `{"127.0.0.1", "localhost"} ∪ {JEVQ_LIVE_HOST}`。
4. **I-1 live.py 主循环异常 → 自动暂停**：`executor.step()` 抛异常时照旧记日志，再 `executor._auto_pause(f"执行异常：{e!r}")`（外面再包一层 try，失败只记日志）。测试：step 抛异常后库里是 auto 暂停、notifier 收到通知。
5. **Minor 6 自我重启检查**：15:10（`LIVE_RESTART_CHECK`）到新常量 `LIVE_RESTART_UNTIL = time(17, 0)` 之间每 `LIVE_RESTART_EVERY = 300` 秒检查一次 `git rev-parse HEAD`（新常量放 config「实盘跟单」段），变了就自我重启。
6. **I-6 easytrader**：`TradeError` 映射成 `"TRADE_ERROR"`（不在 REJECT_CODES 里），执行器会按「认不出的错误」先核对当日委托。改对应测试。
7. **待确认 3/4 的防御（broker.py + 两个适配器）**：
   - `order_from_row` 的委托时间规范化：`"093105"`→`"09:31:05"`（已有）；再加 `"93105"`（5 位数字，easytrader 按整数读掉了前导 0）→ `"09:31:05"`、`"9:31:05"` → `"09:31:05"`。
   - 撤单记录行：操作/买卖标志列含「撤」（如「撤买」「撤卖」）的行不是委托，`GatewayBroker.orders_today` 和 `EasytraderBroker.orders_today` 都跳过（在 broker.py 加一个共用的判断函数）。
8. **Minor 1 看门狗 PID 复用**：daemon.py 接线处 `alive=lambda pid: pid_alive(pid) and is_held(<live.lock 路径>)`（`jevq.runlock.is_held`，锁路径同 live.py：`Path(DB_PATH).parent / LIVE_LOCK_NAME`）：进程还占着锁才杀。
9. **Minor 2 看门狗线程**：`daemon.py` 里 watchdog 线程的 `db.connect` 挪进循环的 try，失败记日志、60 秒后重试，线程不死。
10. **Minor 8 home_install**：拒绝重启的时段结束改成 15:30（daemon 15:05–15:20 在同步/上线）；`check_env` 另外报 `XIADAN_GATEWAY_TOKEN`、`JEVQ_LIVE_HOST` 有没有设（只说有没有，不打印值；这两个没设不算失败，提示「实盘需要」）。
11. **Minor 9 + 待确认 1 live_probe order-test 改成两笔验证「撤最后一笔」**：
    - 流程：检查时段 → 取跌停价 → 打印计划 → 键盘 yes → **再检查一次时段**（拖过 11:30/14:57 就退出）→ 下 A（跌停价 100 股）→ 等 3 秒 → 下 B（跌停价 +0.01，100 股，同样远低于市价不会成交）→ 等 3 秒 → 读当日委托，A、B 都 open 才继续 → `cancel_last()` → 等 3 秒 → 读：必须 B 不 open、A 仍 open → PASS「撤最后一笔只撤了 B」；A 也不 open → **FAIL「cancel_last 撤了不止一笔（可能按全部撤单执行），绝不能上实盘」** → 再 `cancel_last()` 撤 A → 等 3 秒 → 确认 A 不 open。
    - 任何一步下单抛异常：打印「单子可能已经报出去了，去 App 里核对并手动撤单」再退出；撤单后在当日委托里找不到这笔 → FAIL（不是 PASS）。
    - 打印每笔的委托号（place 返回值，可能为空）和当日委托里对应行的合同编号、委托时间原文，方便核对 `capture_entrust_no` 和时间格式。
12. **docs/live-setup.md 第 7 步「实测」补充**：order-test 现在是两笔（说明 PASS/FAIL 含义）；上线前 `w32tm /resync` 校时；在网关 `config/app_config.json` 试 `"order": {"capture_entrust_no": true}`，看 order-test 打印的委托号和「合同编号」是否逐字相同；交易时段手动买 100 股任意一只，跑 `read`，看「股数」列是否包含刚买的（程序已按「当天买的不按客户端减账」防护，但要知道结果）；`read` 输出里看委托时间的原文格式、撤单后有没有多出「撤买/撤卖」行。

要求：常量放 config；中文注释照现有风格；全量测试 `/tmp/claude-0/-home-user-jev-quant/18fe0662-3f47-5990-906e-7725334eae76/scratchpad/venv/bin/python -m pytest -q -p no:cacheprovider`（在 worktree 根目录跑）全绿。可以分几次提交，提交信息结尾两行：`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`、`Claude-Session: https://claude.ai/code/session_01JWDqzhRepN43vPUt55HCnr`。只 `git add` 自己改的文件；不要 push；不做 reset/rebase/force/clean。
