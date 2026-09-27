# 交接说明（给接手的 Claude 会话）

更新：2026-09-27（公司电脑，旧的长会话收尾时整理）。新会话先读完本文，再按「接手第一步」做。

## 一句话
A 股主板（60/00）T+1 模拟盘：规则选股 → Jev（TypeSafe System One 模型）拦公告雷和消息面 → 主账户按 Jev 结论买、影子账户同规则不走 Jev 做对照。只记录、不真实下单。

用户风格见全局 `~/.claude/CLAUDE.md`：中文，结论先行，先问再做，破坏性/对外操作先确认，关键权衡给一个推荐。

## 接手第一步
1. `git log --oneline -15` 看最近做了什么（账本只在写它的机器上，**以 git 为准**）。
2. 看下文「待办」，挑用户这次要做的；做新功能按 superpowers 流程：brainstorming → spec（`docs/specs/`）→ plan（`docs/plans/`）→ subagent-driven-development（账本 `.superpowers/sdd/<plan>/progress.md`，本地、gitignore）。
3. 改完代码：全量测试 `.venv/Scripts/python -m pytest -q -p no:cacheprovider` 必须全绿；家里电脑跑测试先去掉凭证：`env -u TYPESAFE_API_KEY -u PUSHPLUS_TOKEN .venv/Scripts/python -m pytest -q`。

## 四个内置策略（各有主账户 `*main` 和影子账户 `*shadow`，本金 3 万、全仓单票）
| 策略 | 账户 | 买 | 卖 |
|---|---|---|---|
| 低吸 | `main` / `shadow` | 当日 0～-3%，按近 5 日涨幅排序；情绪/外围决定盘中还是 14:50 尾盘 | 设计 `docs/specs/2026-09-23-jev-lowbuy-papertrade-design.md` |
| 扫首板 | `board_main` / `board_shadow` | 09:30–10:30 涨到 9% 触发；近 10 日无涨停；龙头分按热门题材 | 设计 `docs/specs/2026-09-23-multi-strategy-first-board-design.md` |
| 超跌反弹 | `rebound_main` / `rebound_shadow` | 近 5 日跌 0～20%、近 40 个交易日无 2 连板、当日 0～-3%，跌得多的优先；只看情绪 | 独立规则 `jevq/rules/rebound_sell.py`：止损 -2%、低开拉起卖、跟踪止盈、最多 2 天。设计 `docs/specs/2026-09-24-rebound-design.md` |
| 自定义 | `c{n}_main` / `c{n}_shadow` | 面板「规则」页搭（盘口指标墙），次日生效，最多 5 个 | 自己配的卖出规则，必须有止损。设计 `docs/specs/2026-09-24-rule-builder-design.md` |

截至 2026-09-27 库里**还没有**用户建的自定义策略。

## Jev 在哪里用
- **公告排雷**（`jevq/judge/notices.py`，设计 `docs/specs/2026-09-26-notice-risk-design.md`）：两段式——标题关键词/Jev 标题分类挑出十一类疑似 → 抓东方财富正文 → Jev 给高/中/低/无 → 风险分 `高×1+中×0.6+低×0.2`，≥0.5 否决、0.35～0.5「拿不准」也否决。**用户选的 A 口径：Jev 判「中」即否决。** 普通「股票交易异常波动」公告和资金占用汇总表不算疑似；「风险提示」类只看近 10 天。
- **消息面**（`jevq/judge/news.py`）、**市场情绪**（`jevq/judge/market.py`，每 10 分钟刷新）。
- Jev 出错 = 当天不买这只（保守）；卖出从不依赖 Jev。
- 交易循环每分钟：**所有策略先卖**，再依次买（扫首板最先）；每分钟 Jev 总预算 40 秒，没判到的候选下一分钟再判。

## 两台机器
- **公司电脑** `D:\claude\jev-quant`：跑实盘，`data/`（`jevq.sqlite`、日志）只在这台。
- **家里电脑** `E:\claude\jev-quant`：只写代码、跑测试；桌面「Jev模拟盘（家里只读）」→ `home_panel.py` 用 GitHub 上的 `exports/` 重建只读面板。
- GitHub 仓库 `dsk1134162538-hash/jev-quant` 是**私有**的（2026-09-27 核实：匿名访问 404）。决策留痕（含 Jev 细节）不导出。

## 运行现状（公司电脑）
- 后台常驻 `daemon.py`（pythonw，启动项快捷方式「Jev模拟盘后台」；公司电脑任务计划程序被 IT 禁用，别再试）。工作日 08:50 跑 `run.run_day`，15:05 出复盘报告 `reports/YYYY-MM-DD.md`，然后导出数据、同步 GitHub。
- **推到 master 的代码会在下一个交易日收盘后自动上线**：daemon `fetch` + `rebase` + 临时 worktree 跑测试，通过就重启到新代码，不通过继续跑旧代码并微信提醒。
- 2026-09-27 11:10 以 HEAD 54202bc 重启。下次运行：周一 2026-09-28 08:50。国庆 10-01～10-07 休市，节后首个交易日 10-08。
- 收盘价/成交量历史：`closes` 表 2026-06-22～09-24 共 69 个交易日（每只 ≥59 天）。之后每天收盘自动存。
- 手动重启 daemon（只在 09:15–15:05 之外）：结束命令行含 `daemon.py` 的 pythonw 进程 → 从 HKCU 读 `TYPESAFE_API_KEY`、`PUSHPLUS_TOKEN` 设到当前进程环境 → `Start-Process .venv\Scripts\pythonw.exe daemon.py -WorkingDirectory D:\claude\jev-quant -WindowStyle Hidden` → 移除这两个环境变量 → 确认进程在、日志无报错。
- 推送前做密钥检查：未推送的提交里不能出现真实 key（在 PowerShell 里把 HKCU 的值和 `git log origin/master..HEAD -p` 做包含比较，只输出 True/False）。

## 红线
- 凭证只在 HKCU\Environment：`TYPESAFE_API_KEY`（Jev）、`PUSHPLUS_TOKEN`（微信推送）。**绝不打印、不入库、不写进文件。**
- 不做破坏性 git 操作（reset --hard、force push、clean、删分支）；只 stage 自己改的文件。
- 测试不联网、不碰 `data/`、`reports/`；推送在测试里全局拦截（conftest 已有）。
- 盘中不重启后台。
- 同一时间只让一个会话改这个仓库（2026-09-27 出现过两个会话同时在动）。

## 待办（按时间）
1. **约 2026-10-03：公告口径复盘。** 拿这一周被公告否决的主账户候选，对比影子账户对这些票的后续涨跌，决定要不要放宽到 B 口径（只拦 Jev 明显判「高」的）。复盘报告里有「公告风险统计」。
2. **自动上线的顺序隐患（建议修，等用户点头）**：`jevq/sync/deploy.py` 先 `rebase origin/master` 再测试。测试失败时后台还跑旧代码，但磁盘已是新代码，之后电脑重启/手动重启/桌面面板会跑未测试的代码。改法：先在临时 worktree 测 `origin/master`，通过后再 rebase。
3. 本机有未提交代码时 `git rebase` 直接失败 → 推送「同步冲突」提醒，和日志「数据仍会正常推送」不符（影响小）。
4. 卖飞统计（`sell_followups`，报告「卖飞回看」）攒两三周后，用来评估各策略卖出参数。
5. 新指标可用：连涨天/连跌天（含今天）、阶梯缩量/阶梯放量（N=2～5，含今天，按已开盘分钟折算全天量，09:45 前不算）。按 09-24 收盘算，3 阶缩量命中约 596 只，单用太松，适合组合。
6. 用户粘贴过的中转站 key 需用户自行作废。中转站已不再使用，配置已从 `~/.claude/settings.json` 删除。

## 省额度的做法（用户关心）
- 一件事做完就换新会话，上下文超过 15～20 万也换；换前把本文更新好。
- 一条消息多说几件事，少贴大截图；中途可用 `/compact`。
- 实现、审查这类活派子 agent；日常活可以用 Sonnet。

## 验证面板
`scripts/panel_preview.py`：只读预览服务（真实数据库，拦截开始运行、保存/停用规则），端口 8765。周末或盘前试跑时，阶梯缩量/放量显示「—」是设计如此（今天没行情不折算）。
