# 全分支终审 brief（实盘跟单）

你是终审：这套代码下周要操作用户的真实证券账户（东亚前海，同花顺独立下单客户端 + xiadan-gateway / easytrader）。只审不改代码。

## 范围
`git diff origin/master..HEAD`（分支 live-trading-6luqxt，已合入 lowbuy-removal、live-extras、live-web）。先读：
- spec `docs/specs/2026-09-29-live-trading-design.md`
- plan `docs/plans/2026-09-29-live-trading.md`、Task 4/5 brief（`docs/plans/2026-09-29-live-trading-t4-brief.md`、`-t5-brief.md`）
- 账本 `docs/plans/2026-09-29-live-trading-progress.md`（全部 Ruling 是已拍板的取舍，除非你认为某条会亏钱或出事故，否则不要重新争论）
- 研究结论 `docs/research/research-gateway.md`、`research-easytrader.md`

## 重点（按优先级）
1. **执行器 `jevq/live/executor.py` 的真金白银不变量**：终态只在「读成功 + 不再 open」时落（唯一例外：收盘后结算）；一次意图/一次卖出尝试最多一次 `place()`（含进程在任意两次写库之间崩溃再重启）；暂停中零 broker 调用；自动暂停只能被显式恢复解除；撤单只在连续竞价内发；撤单前核对不会撤到用户自己的单；持仓账（`live_positions`）不会被重复减或漏减（成交增量 vs 对账减账）；跨天状态。请构造具体时间线去找反例。
2. **下单层**（Task 1，没做过任务级审查）：`jevq/live/broker.py`（表头映射、`open` 判定、REJECT/INFLIGHT 分类）、`gateway.py`（请求/错误信封/超时分类、绝不发不带 type 的撤单）、`easytrader_broker.py`（懒连接、异常包装、cancel_last 选单）、`scripts/live_probe.py`（`order-test` 的安全闸：时段、`--i-understand`、键盘 yes、只挂跌停价 100 股、撤单确认）。对照研究文件里网关的真实行为。
3. **live.py + web.py**：主循环异常处理、自我重启、单实例锁；页面线程和执行器共用一个 sqlite 连接（`check_same_thread=False`）是否会出事；POST 的 Content-Type / Origin / 本机限定；手机能做的操作会不会绕过安全规则。
4. **看门狗** `jevq/live/watchdog.py` + `daemon.py` 接线（账本 deferred：线程里 `db.connect` 在 try 外、线程可能静默死掉；ctypes 无 argtypes）；会不会在交易时段误杀正常的 live.py。
5. **报告** `jevq/report.py` 实盘段（Task 8，没做过审查）：绝不能出现金额和股数（报告推到 GitHub）；模式 off 且从没跑过时不出这一段。
6. **同步/上线** `jevq/sync/deploy.py`（每日同步 pathspec 修复没审过；git diff 失败已改成按有变化处理）、`scripts/home_install.py`（家里一键安装，没审过：绝不覆盖已有数据库、交易时段不重启、凭证只说有没有）。
7. `run.py` 写意图、`engine.py` 低吸下线（已审过，只看和实盘的接口）。

## 账本里的 deferred minor（逐条给结论：现在修 / 上线后再说 / 不用管）
- Task 2：emit 回滚（已并入 T3？核实）；add_remote_sell 返回值没断言；reduce_position 空操作没测
- Task 6：同 host 不同端口的 Origin 没测试；live.py 里 `web_serve()` 在 try 外
- Task 7：watchdog 线程 db.connect 在 try 外；通知文案；ctypes 无 argtypes
- Task 9：install_shortcut 名字还叫「Jev低吸模拟盘」
- Task 3/4：无委托号时四要素匹配取最后一条（5 秒窗口内两笔相同委托有歧义）；上午被慢网关留下的卖单下午只跟踪不改价

## 输出
写到 `docs/plans/2026-09-29-live-trading-final-review.md`（中文），格式：
- 结论一句话：能不能进入 10-08 起的 3 天演练（dryrun）；能不能在演练通过后 10-13 实盘。
- 发现按 Critical / Important / Minor 分组，每条：`文件:行`、问题、具体会怎样出事（时间线/输入）、建议改法（一两句）。只报你有把握的；拿不准的单列「待确认」并说明怎么在家里实测确认。
- deferred minor 逐条结论。
不要改代码、不要提交、不要 push。跑测试可以：`/tmp/claude-0/-home-user-jev-quant/18fe0662-3f47-5990-906e-7725334eae76/scratchpad/venv/bin/python -m pytest -q -p no:cacheprovider`（云端是 Linux：winreg、ctypes.windll、powershell 相关的失败先判断是环境差异还是真 bug）。
