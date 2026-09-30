# 交接说明（给接手的 Claude 会话）

更新：2026-09-30（云端会话，实盘跟单开发收尾时整理）。新会话先读完本文，再按「接手第一步」做。

## 一句话
A 股主板（60/00）T+1 模拟盘：规则选股 → Jev（TypeSafe System One 模型）拦公告雷和消息面 → 主账户按 Jev 结论买、影子账户同规则不走 Jev 做对照。
**新增实盘跟单**（设计 `docs/specs/2026-09-29-live-trading-design.md`）：模拟账户 `rebound_main`（超跌反弹·主）每成交一笔，家里电脑的 `live.py` 在东亚前海真实账户里跟一笔，单只最多 2 万、最多持 1 只；手机（Tailscale）上可暂停/恢复/立即卖出。低吸策略已下线。

用户风格见全局 `~/.claude/CLAUDE.md`：中文，结论先行，先问再做，破坏性/对外操作先确认，关键权衡给一个推荐。

## 接手第一步
1. `git log --oneline -15` 看最近做了什么（账本只在写它的机器上，**以 git 为准**）。
2. 看下文「待办」，挑用户这次要做的；做新功能按 superpowers 流程：brainstorming → spec（`docs/specs/`）→ plan（`docs/plans/`）→ subagent-driven-development。实盘跟单这一轮的账本在 `docs/plans/2026-09-29-live-trading-progress.md`（全部 Ruling 都在里面），终审结论 `docs/plans/2026-09-29-live-trading-final-review.md`。
3. 改完代码：全量测试 `.venv/Scripts/python -m pytest -q -p no:cacheprovider` 必须全绿；家里电脑跑测试先去掉凭证：`env -u TYPESAFE_API_KEY -u PUSHPLUS_TOKEN .venv/Scripts/python -m pytest -q`。云端会话（Linux）用 python3.12 建 venv 装 `pytest requests pandas akshare pywebview` 就能跑全量；winreg / ctypes.windll / powershell 相关的失败先判断是不是环境差异。

## 内置策略（各有主账户 `*main` 和影子账户 `*shadow`，本金 3 万、全仓单票）
| 策略 | 账户 | 买 | 卖 |
|---|---|---|---|
| 低吸（**已下线**） | `main` / `shadow` | 历史成交、净值保留，面板/推送不再出现；显示名「低吸·主（已下线）」 | 设计 `docs/specs/2026-09-23-jev-lowbuy-papertrade-design.md` |
| 扫首板 | `board_main` / `board_shadow` | 09:30–10:30 涨到 9% 触发；近 10 日无涨停；龙头分按热门题材 | 设计 `docs/specs/2026-09-23-multi-strategy-first-board-design.md` |
| 超跌反弹 | `rebound_main` / `rebound_shadow` | 近 5 日跌 0～20%、近 40 个交易日无 2 连板、当日 0～-3%，跌得多的优先；只看情绪 | 独立规则 `jevq/rules/rebound_sell.py`：止损 -2%、低开拉起卖、跟踪止盈、最多 2 天。设计 `docs/specs/2026-09-24-rebound-design.md` |
| 自定义 | `c{n}_main` / `c{n}_shadow` | 面板「规则」页搭（盘口指标墙），次日生效，最多 5 个 | 自己配的卖出规则，必须有止损。设计 `docs/specs/2026-09-24-rule-builder-design.md` |

截至 2026-09-27 库里**还没有**用户建的自定义策略。

## Jev 在哪里用
- **公告排雷**（`jevq/judge/notices.py`，设计 `docs/specs/2026-09-26-notice-risk-design.md`）：两段式——标题关键词/Jev 标题分类挑出十一类疑似 → 抓东方财富正文 → Jev 给高/中/低/无 → 风险分 `高×1+中×0.6+低×0.2`，≥0.5 否决、0.35～0.5「拿不准」也否决。**用户选的 A 口径：Jev 判「中」即否决。** 普通「股票交易异常波动」公告和资金占用汇总表不算疑似；「风险提示」类只看近 10 天。
- **消息面**（`jevq/judge/news.py`）、**市场情绪**（`jevq/judge/market.py`，每 10 分钟刷新）。
- Jev 出错 = 当天不买这只（保守）；卖出从不依赖 Jev。
- 交易循环每分钟：**所有策略先卖**，再依次买（扫首板最先）；每分钟 Jev 总预算 40 秒，没判到的候选下一分钟再判。

## 两台机器（2026-09-30 起：后台搬到家里）
- **公司电脑** `D:\claude\jev-quant`：后台 2026-09-30 09:09 已停，开机启动项改名为 `Jev模拟盘后台.lnk.disabled`（禁用）。不再跑任何东西。
- **家里电脑**：开发目录 `E:\claude\jev-quant`（写代码、跑测试）；运行目录 `E:\jev-live`（只跑不改），用 `scripts/home_install.py` 一键安装/更新：clone/rebase **master**、建 .venv、放入公司迁来的数据库（已存在绝不覆盖）、检查凭证（只说有没有）、建开机启动项、启动 `daemon.py` 和 `live.py`；交易时段（09:15–15:30，含收盘后同步/上线）拒绝重启，除非 `--force`。步骤见 `docs/live-setup.md` 第 0.5 节。
- 数据库快照在 GitHub 临时分支 `migration-data`（7b011c6）。**家里装好后删掉这个分支——删之前先问用户。**
- GitHub 仓库 `dsk1134162538-hash/jev-quant` 是**私有**的。决策留痕（含 Jev 细节）不导出。

## 运行现状
- `daemon.py`（模拟盘，pythonw 常驻）：工作日 08:50 跑 `run.run_day`，15:05 出复盘报告 `reports/YYYY-MM-DD.md`，然后导出数据、同步 GitHub；它还每 60 秒看一次 `live.py` 的心跳（模式不是 off 时），超过 120 秒没更新就重启 live.py，每天最多 3 次。
- **自动上线已改成先测后合并**（`jevq/sync/deploy.py`）：fetch 后若 origin/master 的代码和运行中的不同（或比较失败），先在临时 worktree 测 origin/master，通过才 rebase + push + 重启；不通过继续跑旧代码、当天数据不推、微信提醒。
- `live.py`（实盘执行，唯一操作真实账户的进程）：每秒一轮；手机页端口 8766，只监听 127.0.0.1 和 `JEVQ_LIVE_HOST`（家里电脑的 Tailscale 地址）；15:10 后若 git HEAD 变了就自我重启。日志 `data/live.log`。
- 国庆 10-01～10-07 休市，节后首个交易日 10-08。收盘价历史 `closes` 表截至迁移时 71 个交易日。
- 推送前做密钥检查：未推送的提交里不能出现真实 key（PowerShell 里把 HKCU 的值和 `git log origin/master..HEAD -p` 做包含比较，只输出 True/False；云端没有 HKCU 时 grep 常见 key 形态）。

## 实盘跟单（2026-09-29～30 开发）
- 代码：`jevq/live/`（`broker.py` 接口与表头映射、`gateway.py` xiadan-gateway 客户端〔首选〕、`easytrader_broker.py`〔备选〕、`store.py` 四张 `live_*` 表、`executor.py` 执行器、`web.py` 手机页、`watchdog.py` 看门狗）、`live.py` 入口、`scripts/live_probe.py` 家里实测、`docs/live-setup.md` 安装步骤（Tailscale、ToDesk、电源、同花顺设置、网关、easytrader、实测）。
- 下单层在 `config.LIVE_BROKER` 选（默认 gateway）；家里实测两个都读 20 次（`live_probe.py read`），交易时段用户本人跑一次 `order-test`（跌停价挂 100 股再撤），读得准的那个定下来。
- 模式：`off`（默认，什么都不做）/ `dryrun`（演练：照常读客户端、算委托，不下单）/ `live`（真下单）。**模式只能在家里电脑本机打开 `http://127.0.0.1:8766` 切换**；dryrun→live 会清掉演练持仓。
- 安全规则（都在 executor 里，有测试）：委托终态只在读到且不再挂着时才落；一个意图最多下一次单；暂停中完全不碰客户端；意外情况（查不到委托状态、撤单前核对不上、连续 3 次读失败、认不出的错误）自动暂停，**只能手动恢复**；撤单只在连续竞价内（11:30、14:57 前约 50 秒不再开新买单，14:57 后卖单留给收盘集合竞价）；客户端股数少于程序账时隔 60 秒两次读到同一个数才减账。
- 日程：演练 3 个交易日 **10-08、10-09、10-12**（dryrun）；最早 **10-13** 切 live。切之前用户问客户经理程序化交易要不要报备。
- 分支：全部实盘代码在 `live-trading-6luqxt`（已合入 lowbuy-removal、live-extras、live-web）。**合进 master 必须先问用户**；home_install 装的是 master，所以演练前要合。旧分支 live-trading / lowbuy-removal / live-extras / live-web 已被它包含。

## 红线
- 凭证只在 HKCU\Environment：`TYPESAFE_API_KEY`（Jev）、`PUSHPLUS_TOKEN`（微信推送）、`XIADAN_GATEWAY_TOKEN`（网关）；`JEVQ_LIVE_HOST` 是 Tailscale 地址。**绝不打印、不入库、不写进文件。**
- 实盘：模式只能本机切换；程序操作客户端期间不要碰家里电脑的鼠标键盘（先在手机页暂停）；**xiadan-gateway 仓库 `scripts/` 下的脚本会真实下单，绝不能运行**；交易时段不重启 daemon/live.py。
- 不做破坏性 git 操作（reset --hard、force push、clean、删分支）；只 stage 自己改的文件。
- 测试不联网、不碰 `data/`、`reports/`；推送在测试里全局拦截（conftest 已有）。
- 盘中不重启后台。
- 同一时间只让一个会话改这个仓库（2026-09-27 出现过两个会话同时在动）。

## 待办（按时间）
1. **约 2026-10-03：公告口径复盘。** 拿这一周被公告否决的主账户候选，对比影子账户对这些票的后续涨跌，决定要不要放宽到 B 口径（只拦 Jev 明显判「高」的）。复盘报告里有「公告风险统计」。
2. **实盘上线路径**：终审发现修完 → 用户同意后合进 master → 家里 `home_install.py` → `live_probe.py` 实测定下单层 → 10-08、10-09、10-12 演练（dryrun）→ 用户报备确认 → 最早 10-13 本机切 live。每个演练日收盘后看复盘报告「实盘跟单（演练）」段和微信小结。
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
