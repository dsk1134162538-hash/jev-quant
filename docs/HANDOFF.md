# 交接说明（给接手的 Claude 会话）

更新：2026-09-24 13:46（由 Claude 桌面端会话写，交接到 VS Code 扩展会话）

## 这是什么项目
A 股主板 T+1 模拟盘：规则选股 + Jev（TypeSafe System One 模型）拦截公告雷/消息面 + 影子账户对照。用户风格见全局 `~/.claude/CLAUDE.md`（中文、结论先行、先问再做、破坏性/对外操作先确认）。

- 设计：`docs/specs/2026-09-23-jev-lowbuy-papertrade-design.md`（低吸）、`docs/specs/2026-09-23-multi-strategy-first-board-design.md`（扫首板）、`docs/specs/2026-09-24-rule-builder-design.md`（规则搭建器）
- 计划：`docs/plans/`（同名）
- 开发流程：superpowers:subagent-driven-development；**每个计划有账本** `.superpowers/sdd/<plan>/progress.md`，接手前先读账本，按账本续做，不要重做已 complete 的任务。

## 运行现状（公司电脑）
- 后台常驻 `daemon.py`（pythonw，启动项快捷方式「Jev模拟盘后台」），今天 09:42 以当时代码启动，工作日 08:50 自动跑 `run.run_day`；持有 `data/runner.lock` 期间桌面面板「开始运行」会被禁用。
- 凭证只在用户环境变量（HKCU\Environment）：`TYPESAFE_API_KEY`（Jev）、`PUSHPLUS_TOKEN`（微信推送）。**绝不打印、不入库、不写进文件。**
- 数据：`data/jevq.sqlite`（不入库）。今天扫首板买入南方精工 002553（未封板，浮亏），低吸看尾盘。

## 进行中
1. **规则搭建器计划**（账本 `.superpowers/sdd/2026-09-24-rule-builder/progress.md`）：Task 1、2、3、3b、4 已完成并审查通过（HEAD e657307）。**下一个：Task 5 CustomStrategy 运行时**，注意账本里带给 Task 5 的事项（取值函数用 `jevq/rules/metrics.py`；股本换算成亿股；`ensure_account` 用 `capital_of` 注资）。
2. **GitHub 同步 + 收盘后自动上线**（brief：`.superpowers/sdd/2026-09-24-rule-builder/sync-deploy-brief.md`）：13:40 由桌面端会话派给 implementer，完成后需要做一次 task review（重点：从不做破坏性 git 操作、密钥扫描、测试门槛、失败不影响交易），并记入账本。若交接时 implementer 已完成，其报告在同目录 `sync-deploy-report.md`。

## 今天 15:10 收盘后必须做
1. 收盘复盘给用户（中文）：`data/run-YYYYMMDD.log` 尾部；只读查询 `data/jevq.sqlite` 今天各账户成交、持仓、equity、各策略 decisions、Jev 调用数/错误；`reports/2026-09-24.md`；统计今天因「不减持/承诺不」否定词 bug 被误杀的票数。
2. **重启后台以启用今天完成的修复**（严格首板=近 10 日无涨停、锁竞争重试、否定词修复、推送修复等）——仅当：工作区干净（`git status` 无改动）、全量测试通过（`.venv/Scripts/python -m pytest -q`）。步骤（PowerShell）：结束命令行包含 `daemon.py` 的 pythonw 进程 → 从 HKCU 读 `TYPESAFE_API_KEY`、`PUSHPLUS_TOKEN` 设到当前进程环境 → `Start-Process .venv\Scripts\pythonw.exe daemon.py -WorkingDirectory D:\claude\jev-quant -WindowStyle Hidden` → 移除这两个环境变量 → 确认进程存在、`data/daemon.log`/`data/run-*.log` 无报错。**不要在盘中（09:15–15:05）重启。**
3. 推送 GitHub（先做密钥扫描：提交历史里不得出现 `sk-`、`apikey_`、`gho_` 等）。

## 已知待办（账本里有详细记录）
- 规则搭建器 Task 5–9。
- 让新浪成为默认补数源（腾讯 K 线曾被 WAF 封）。
- 用户粘贴过的中转站 key 需要用户自行作废更换（不要读取或打印 `~/.claude/settings.json` 里的 token）。
