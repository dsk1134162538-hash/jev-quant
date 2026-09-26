# 交接说明（给接手的 Claude 会话）

更新：2026-09-26 晚（家里电脑会话写：把家里 9/24 的工作合并到公司 9/26 的 master 之上）

## 这是什么项目
A 股主板 T+1 模拟盘：规则选股 + Jev（TypeSafe System One 模型）拦截公告雷/消息面 + 影子账户对照。用户风格见全局 `~/.claude/CLAUDE.md`（中文、结论先行、先问再做、破坏性/对外操作先确认）。

- 设计：`docs/specs/`（低吸、扫首板、规则搭建器、超跌反弹 2026-09-24、公告风险细判 2026-09-26）
- 计划：`docs/plans/`（同名）
- 开发流程：superpowers:subagent-driven-development；每个计划有账本 `.superpowers/sdd/<plan>/progress.md`（本地，gitignore），接手前先读账本。**账本只在写它的那台电脑上**，换机器时以 `git log` 为准。

## 两台机器
- **公司电脑**（`D:\claude\jev-quant`）：跑实盘，`data/`（jevq.sqlite、日志）只在这台。
- **家里电脑**（`E:\claude\jev-quant`）：只写代码、跑测试，不跑实盘。家里 shell 会继承 HKCU 凭证，跑测试时去掉：`env -u TYPESAFE_API_KEY -u PUSHPLUS_TOKEN .venv/Scripts/python -m pytest -q`。

## 运行现状（公司电脑）
- 后台常驻 `daemon.py`（pythonw，启动项快捷方式「Jev模拟盘后台」），工作日 08:50 自动跑 `run.run_day`，收盘后自动导出数据、同步 GitHub、代码有变化且测试通过则自动上线（`jevq/sync/`）。
- 2026-09-26 11:16 已以 HEAD a1b2696 重启。下次运行：周一 2026-09-28 08:50。
- **推到 master 的代码会自动上线**：公司 daemon 每个交易日收盘后 `fetch` + `rebase origin/master` + `push`，代码有变化且测试通过就自动重启到新代码；测试不过则不上线（PushPlus 提醒）。
- 项目 Python 是 `.venv/Scripts/python.exe`（系统 `python` 没装 akshare）。
- 2026-10-01～10-07 国庆休市，节后首个交易日 10-08。
- 凭证只在用户环境变量（HKCU\Environment）：`TYPESAFE_API_KEY`、`PUSHPLUS_TOKEN`。**绝不打印、不入库、不写进文件。** 重启 daemon 时从 HKCU 读进进程环境再启动，启动后移除。
- 不要在盘中（09:15–15:05）重启后台。

## 刚做完 / 进行中
1. **公告风险细判**（公司，计划 `docs/plans/2026-09-26-notice-risk.md`）：全部完成并上线。用户选 A 口径：Jev 判「中」风险（分数 0.6）即否决。
   - **待办（约 2026-10-03）**：跑满一周后，拿被公告否决的候选对比影子账户后续涨跌，决定是否放宽（B 口径）。复盘报告有「公告风险统计」小节。
2. **成交量历史补数**（公司）：2026-09-26 已完成，最近 22 个交易日 volume 基本全覆盖。阶梯缩量 N=3 单用太松，适合与其他条件组合。
3. **规则搭建器 Task 9 最终审查**（家里 9/24）：已完成。合并后保留的修复：
   - 自定义策略「持有天数」按交易日历算到今天（原实现盘中恒为 0，「持有 N 天卖」永不触发）；
   - 自动上线重启：新进程带 `--after-deploy`，最多等 60 秒让旧进程释放 `daemon.lock`（原来可能见锁即退，上线后后台消失）；
   - `custom_store.save` 拒绝未知策略 id；Jev 返回 error 判定也计入自定义策略的熔断。
   - 「每分钟检查额度」「买不进的候选不问 Jev」公司已各自修过，合并时以公司版本为准（错误熔断仍按天累计）。
4. **第三个内置策略「超跌反弹」**（家里 9/24，设计 `docs/specs/2026-09-24-rebound-design.md`）：用户实盘打法。
   近 5 日跌 0~20%、近 40 个交易日没出现过 2 连板、当日 0~-3%，5 日跌得多的优先；只看情绪决定盘中/尾盘（不看外围）。
   卖出独立规则 `jevq/rules/rebound_sell.py`：止损 -2%（相对买入价）、低开 ≤ -1.5% 后从最低拉起 0.8 点必卖、
   盈利 ≥ +1.5% 后回落 0.8 点跟踪止盈（最近 3 分钟主买多先留着，跌回成本必卖）、最多拿 2 个交易日。
   账户 `rebound_main`/`rebound_shadow`；已接入 `run._run_minute`（卖出阶段在扫首板之后，买入阶段在低吸之后，共用 Jev 时间预算）。
   同时情绪刷新 30 → 10 分钟（低吸共用）。
5. **卖飞统计**（家里 9/24，所有账户）：`sell_followups` 表，记每笔卖出后的当日最高/收盘、次日最高/收盘；
   报告「卖飞回看」、`exports/sell_followups.csv`。只记录不参与交易，攒两三周后用来评估卖出参数。

## 已知待办
- **超跌反弹的连板排除需要 ≥ 41 个交易日收盘价**：现在约 22 天，能跑但排除不全（日志会提示）。在公司电脑补到 ≥ 60 个交易日（`run.py --backfill`，起始日往前推到 2026-06 下旬）。
- 审查发现、待用户决定：**自动上线在测试前就把远端代码合进工作区**（`jevq/sync/deploy.py`：fetch → rebase → 再在临时 worktree 测试）。测试失败时后台仍跑旧代码，但磁盘上已是新代码，之后手动重启/电脑重启/桌面面板会跑未经测试的代码。改法候选：先在临时 worktree 测 `origin/master`，通过后再 rebase。
- 本机有未提交代码改动时 `git rebase` 直接失败 → 推送「同步冲突」，与日志「数据仍会正常推送」不符（影响小）。
- 用户粘贴过的中转站 key 需用户自行作废（中转站配置已从 `~/.claude/settings.json` 删除，不再使用）。

## 验证面板的方法
`scripts/panel_preview.py`：只读预览服务（真实数据库，拦截 start/stop/rule_create/rule_update/rule_disable），端口 8765。`D:\claude\.claude\launch.json` 的 `panel-preview` 配置可改为指向它。
