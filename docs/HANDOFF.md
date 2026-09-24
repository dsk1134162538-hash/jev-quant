# 交接说明（给接手的 Claude 会话）

更新：2026-09-24 晚（由家里电脑的 Claude 会话写；上一版 13:46 已过时，以本文件 + `git log` 为准）

## 这是什么项目
A 股主板 T+1 模拟盘：规则选股 + Jev（TypeSafe System One 模型）拦截公告雷/消息面 + 影子账户对照。用户风格见全局 `~/.claude/CLAUDE.md`（中文、结论先行、先问再做、破坏性/对外操作先确认）。

- 设计：`docs/specs/2026-09-23-jev-lowbuy-papertrade-design.md`（低吸）、`docs/specs/2026-09-23-multi-strategy-first-board-design.md`（扫首板）、`docs/specs/2026-09-24-rule-builder-design.md`（规则搭建器）
- 计划：`docs/plans/`（同名）
- 开发流程：superpowers:subagent-driven-development；每个计划有账本 `.superpowers/sdd/<plan>/progress.md`。**账本只在公司电脑上**（没进库），家里接手时看不到，只能以 `git log` 判断进度。

## 两台机器的分工
- **公司电脑**（`D:\claude\jev-quant`）：跑实盘。后台常驻 `daemon.py`（pythonw，启动项快捷方式「Jev模拟盘后台」），工作日 08:50 自动跑 `run.run_day`；`data/`（jevq.sqlite、run-*.log、runner.lock）只在这台。
- **家里电脑**（`E:\claude\jev-quant`）：只写代码、跑测试，不跑实盘，没有 `data/`。家里 shell 会继承 HKCU 凭证，跑测试时去掉：`env -u TYPESAFE_API_KEY -u PUSHPLUS_TOKEN .venv/Scripts/python -m pytest -q`。
- 凭证只在用户环境变量（HKCU\Environment）：`TYPESAFE_API_KEY`（Jev）、`PUSHPLUS_TOKEN`（微信推送）。**绝不打印、不入库、不写进文件。**

## 进度
- **规则搭建器 Task 1–8 已完成**（公司电脑，最后一个提交 `4669f85` 面板审查修复）。
- **GitHub 每日同步 + 收盘后自动上线**已完成（`fba700a`，修复 `c5f2b19`）。
- **Task 9 最终审查已完成**（家里，分支 `review-fixes`，提交 `ad50a3b`，**截至写作时未推送/未合并**）。修复内容：
  1. 持有天数按交易日历算到今天（原实现盘中恒为 0，「持有 N 天卖」永不触发）；`run.py` 把 `days` 传给 `CustomStrategy(trade_days=...)`。
  2. 自定义策略的 Jev 检查额度 / 错误熔断改为每分钟清零（原实现当日累计，查满 10 只后主账户整天不买）。
  3. Jev 返回 `error` 判定计入熔断；可买性预检（停牌/封死/不够一手）不消耗 Jev 调用。
  4. 自动上线重启：新进程带 `--after-deploy`，最多等 60 秒让旧进程释放 `daemon.lock`（原来可能见锁即退，上线后后台消失）。
  5. `custom_store.save` 拒绝未知策略 id。
- **新增第三个内置策略「超跌反弹」**（家里，同在 `review-fixes` 分支，未推送）。设计：`docs/specs/2026-09-24-rebound-design.md`。
  用户实盘打法：近 5 日跌 0~20%、近 40 个交易日没出现过 2 连板的票，当日回调到 0~-3% 时买，5 日跌得多的优先；
  买入时机只看情绪（正常/火热盘中，偏弱/冰点尾盘 14:50，不看外围）；卖出与低吸完全相同。账户 `rebound_main`/`rebound_shadow`。
  同时情绪刷新 30 → 10 分钟（低吸共用）。
  卖出后改为独立规则（`jevq/rules/rebound_sell.py`）：止损 -2%（相对买入价）、低开 ≤ -1.5% 后从最低拉起 0.8 点必卖、盈利 ≥ +1.5% 后回落 0.8 点跟踪止盈（最近 3 分钟主买多先留着，跌回成本必卖）、最多拿 2 个交易日。
- **卖飞统计**（所有账户）：`sell_followups` 表，记每笔卖出后的当日最高/收盘、次日最高/收盘；报告「卖飞回看」、`exports/sell_followups.csv`。只记录不参与交易，攒两三周后用来评估卖出参数。

## 回公司电脑要做
1. `git fetch`，把 `review-fixes` 合进 master（用户确认后），跑全量测试。
2. Task 9 剩余（只能在公司电脑）：用新浪补足本地收盘价——**超跌反弹的连板排除需要 ≥ 41 个交易日，补到 ≥ 60 个交易日**（`start_date` 往前推到 2026-06 下旬）。不补也能跑，但日志会提示「收盘价库只有 N 天，排除可能不全」。
3. 收盘后重启后台以启用新代码——仅当工作区干净、全量测试通过。步骤（PowerShell）：结束命令行包含 `daemon.py` 的 pythonw 进程 → 从 HKCU 读 `TYPESAFE_API_KEY`、`PUSHPLUS_TOKEN` 设到当前进程环境 → `Start-Process .venv\Scripts\pythonw.exe daemon.py -WorkingDirectory D:\claude\jev-quant -WindowStyle Hidden` → 移除这两个环境变量 → 确认进程存在、`data/daemon.log`/`data/run-*.log` 无报错。**不要在盘中（09:15–15:05）重启。**
4. 推送 GitHub 前做密钥扫描（`sk-`、`apikey_`、`gho_` 等不得出现在提交里）。

## 审查发现、待用户决定（未改）
- **自动上线在测试前就把远端代码合进工作区**（`jevq/sync/deploy.py`：fetch → rebase → 再在临时 worktree 测试）。测试失败时后台仍跑旧代码，但磁盘上已是新代码；之后手动重启/电脑重启/桌面面板都会跑未经测试的代码。改法候选：先在临时 worktree 测 `origin/master`，通过后再 rebase。涉及数据推送流程，需用户拍板。
- 本机有未提交代码改动时 `git rebase` 直接失败 → 推送「同步冲突」，与日志「数据仍会正常推送」不符（影响小）。

## 已知待办
- 让新浪成为默认补数源（腾讯 K 线曾被 WAF 封）。
- 用户粘贴过的中转站 key 需要用户自行作废更换（不要读取或打印 `~/.claude/settings.json` 里的 token）。
