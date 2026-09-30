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
- **家里电脑**：开发目录 `E:\claude\jev-quant`（写代码、跑测试）；**一键安装/更新 = 双击开发目录里的 `home_setup.bat`**（拉 master → 开发目录 .venv → easytrader → `scripts/live_install.py` 装 xiadan-gateway 到 `E:\xiadan-gateway`〔固定审查过的提交、token 生成后写网关配置和 HKCU、读 Tailscale 地址设 JEVQ_LIVE_HOST、开机自启「xiadan网关」〕→ `scripts/home_install.py`）。运行目录 `E:\jev-live`（只跑不改），由 `scripts/home_install.py` 安装/更新：clone/rebase **master**、建 .venv、放入公司迁来的数据库（已存在绝不覆盖）、检查凭证（只说有没有）、建开机启动项、启动 `daemon.py` 和 `live.py`；交易时段（09:15–15:30，含收盘后同步/上线）拒绝重启，除非 `--force`。用户操作步骤全在 `docs/live-setup.md`（时间表、同花顺设置、手机、测试、切实盘条件、日常使用）。
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
- **切 live 之前必须在家里交易时段做完的实测**（终审「待确认」，详见 `docs/plans/2026-09-29-live-trading-final-review.md` 和 `docs/live-setup.md` 第 7 步）：① `live_probe.py order-test` 两笔验证「撤最后一笔」只撤后一笔——撤了两笔就绝不能上实盘；② 手动买 100 股后 `read`，看「股数」含不含当天买入；③ 委托时间原文格式、电脑时钟和券商的偏差（上线前 `w32tm /resync`），试网关 `capture_entrust_no: true`；④ 撤单后当日委托里有没有多出「撤买/撤卖」行。演练模式不调下单和撤单，演练跑通不代表下单路径没问题。
- 每个交易日 09:05 应该收到「💰 实盘 自检」推送（模式不是 off 时，暂停中也会发「暂停中」）。**没收到 = daemon 或 live.py 没在跑**（交易日标记只由 daemon 写，daemon 停了 live.py 会当非交易日静默不动）。
- 委托状态多了 `unknown`：前几天没核对到最终状态的委托，启动/恢复时标出来并自动暂停，推送里列清楚是哪几笔；用户在 App 里处理好后再点恢复。
- **✅ 已解（2026-09-30 下午，撤销原红线）：验证码带字母的问题**——把网关自带的"4 位数字轻量模板 OCR"扶正为"ddddocr 主引擎 + 轻量兜底"，加 `case_mode` 开关（as_is / upper / lower）。改动固化在 `patches/xiadan-gateway-ocr-alnum.patch`（jev-quant 里），`scripts/live_install.py` 在 `sync_gateway` 之后幂等 `git apply`，`uv sync --extra ocr` 一并装 `ddddocr`。原"不识别/不绕过"红线撤销。
  - **2026-09-30 家里实测**：`read --n 5` = 15/15；`read --n 10` × 2 次 = 58/60（**96.7% 成功率**）。持仓 `002655 600 600` 读到。positions/orders_today 平均 13-16s（内含多次外层重试），最坏 46s（8 次外层重试全错）。同花顺**服务端大小写敏感**（ddddocr 出 `Smkf`、真值 `SmKf` 直接被拒）；**客户端"点取消 = 关查询、不刷新"** — 内层重试无意义，唯一有效路径是外层 Ctrl+C 重来。ddddocr 通用模型对当前同花顺字体单次命中 ~40-50%（连纯数字如 `4620` 都会误判，不只是大小写问题）。
  - **配套修的三处 bug**（都在 patch 里）：
    1. `_keep_alnum` 原来用 `str.isalnum()` — Python 里汉字 `.isalnum()` 返回 True，ddddocr 偶把字母判成汉字（如 `w血aK`）会漏过。改成 ASCII 白名单 `[A-Za-z0-9]`。
    2. `_copy_table_via_clipboard` 和消息级复制路径的 `max_attempts` 从 `2` 提到 `8`。ddddocr 单次 40-50%，2 次累积 64-75%（不够），8 次累积 96-99.6%。
    3. `task_queue.query_timeout_seconds` 默认 `30 → 60`。8 次外层重试单次 3-5s，最坏 30-40s，30s 会撞 `TASK_TIMEOUT`（--n 10 第一轮实测遇到过 1 次）。
  - **理论天花板**：ddddocr 通用模型的单次准确率就是 ~45%。要更高（99.9%+）得训自定义模型（拿 dj 账户的真实样本 + ddddocr 训练脚本），或换更重的 OCR。当前 96.7% 对模拟盘演练完全够用；实盘上线前若要更稳，让读失败自动暂停 + 微信提醒即可（executor 已有）。
  - **easytrader 备选层已实测排除（2026-09-30 15:35，停网关后跑 `read --broker easytrader --n 5`）**：balance 2/5，positions 0/5、orders_today 0/5，报 `[Errno 2] No such file or directory ...tmp*.xls`（Ctrl+S 没导出文件），**不是 `UNKNOWN_HEADER`**，也不是网关抢焦点——网关停着照样失败。下单层定为 gateway，不用补 `_pick` 别名。
  - **case_mode 可选切换**：现在默认 `as_is`（ddddocr 原样输出）已经跑通。若哪天成功率下滑、日志显示大小写方向单一（比如全被打回小写），改 `E:\xiadan-gateway\config\app_config.json` 的 `"ocr": {"case_mode": "upper"}` 或 `"lower"` 试。
  - **✅ 自训练验证码模型已部署（2026-09-30 晚）**（设计 `docs/specs/2026-09-30-ocr-custom-training-design.md`，补丁里带了加载逻辑）：
    - **家里实测**：`read --n 30` 三接口 **90/90 成功**，平均耗时 balance 1.8s / positions 5.7s / orders_today 5.2s（通用模型时代 positions ~12.5s、orders ~20s）；单次验证码通过率约 **87%**（132/151，通用模型约 38%）。离线 46 张真实验证图：自训练 95.7%，通用模型 50%。
    - **部署方式**：模型 `E:\xiadan-gateway\assets\custom_ocr\{model.onnx,charsets.json}`（2.5MB，**本机资产，不入库**）；网关 `ocr.py` 有这两个文件就用它，没有就回退通用模型（删文件+重启网关即回退）。日志出现"ddddocr 主引擎就绪（自训练模型）"即生效。字符集 52 个（数字 2-8，字母去掉 I/O/i/j/l/o/g）——同花顺验证码本来就不出 0/1/9/I/O/l，若哪天出现新字符，模型永远读不对，需重训。
    - **怎么训的（可复现）**：`E:\ocr-train\`（本机，不入库）：dddd_trainer + CPU torch；真实样本靠网关采集钩子攒（`XIADAN_CAPTCHA_HARVEST=1` 时每次提交的验证码存进 `assets/captcha_harvest/`，`ok_*` = 服务端通过，文件名里就是真值；`bad_*` = 被拒，真值要人标）；真实样本只有 ~600 张，直接训不出来，用 Windows 字体合成 4.8 万张（`tools/gen_synth.py`）+ 真实图增广一起训，图高 32，约 1.4 万步。**dddd_trainer 自带的 onnx 导出（输出 argmax 索引）与网关装的 ddddocr 1.6.1 不兼容**，用 `tools/export_ckpt.py`（输出 logits、归一化烤进图里）导出。
    - **标注不能全信**：bad 样本的真值是 Claude 目视标的（在已知真值的 24 张上盲标对 22 张，错的是 x/z 大小写），约 9 张看不清跳过；验证集 46 张里的 2 个"错误"都是大小写存疑。
    - **补丁现在含**：采集钩子、自训练加载、`get_clipboard` 遇 CF_TEXT 字节按 gbk 解码（修 `a bytes-like object is required, not 'str'`，之前 positions 偶发 INTERNAL_ERROR）、历史难样本回归测试在装了自训练模型时只查格式。
    - **采集钩子继续用**：演练期间网关带 `XIADAN_CAPTCHA_HARVEST=1` 跑，攒的 ok/bad 数即单次通过率监控，也是下一轮重训的数据；开机自启的网关默认不带这个变量（`E:\ocr-train\start_gateway_harvest.bat` 是带变量的启动方式，用 WMI 隐藏窗口起，别用会开黑色 cmd 窗口的方式）。
  - 客户端设置备忘（不受影响）：「查询委托时显示撤单记录」= 否（第 7 步第④项按这个解读）；默认买卖价格「卖一价/买一价」下单前清空；「是否隐藏个人信息」= 是。
  - **后续可选**（不阻塞演练/上线）：问客户经理能否给账户关掉复制数据验证码；开通 QMT/miniQMT 官方程序化接口（最稳，届时换下单层）。程序化交易报备顺带一起问。
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
2. **实盘上线路径**（验证码问题 2026-09-30 已通过 ddddocr 补丁解掉，家里 --n 10 三次实测 97.8%，见上方「✅ 已解」条）：终审发现修完 → 用户同意后合进 master → 家里 `home_install.py` → `live_probe.py` 实测定下单层 → 10-08、10-09、10-12 演练（dryrun）→ 用户报备确认 → 最早 10-13 本机切 live。每个演练日收盘后看复盘报告「实盘跟单（演练）」段和微信小结。
3. **自训练验证码模型（2026-09-30 已完成部署，见上方「✅ 自训练验证码模型已部署」）**。后续可选：演练三天攒的 `captcha_harvest` 样本再训一轮，单次通过率 87% → 95%+；训练要点见那条。
   **家里状态（2026-09-30 晚）**：`home_install.py` 已跑（授权后），`E:\jev-live` 里 daemon.py 和 live.py 在跑、模式 off，开机启动项已建；桌面有完整面板快捷方式「Jev低吸模拟盘」（名字是旧的，指向 `E:\jev-live\desktop.py`，没打开验证过）；PushPlus token 和 Tailscale（电脑 100.72.84.113，手机已连）已配；网关已停（要用双击 `E:\xiadan-gateway\start_gateway.bat`）。**没做**：a–e（交易时段 order-test 等）和 g（杀网关看自动暂停）——都要盘中，等 10-08 演练当天在场做；g 需要 live.py 在 dryrun 且当天 trading_day 标记存在。
   **发现待修（没改）**：① `jevq/sync/deploy.py` 第 29/36/40 行 `subprocess.run(text=True)` 没指定编码，中文 Windows 上按 gbk 解码 UTF-8 输出会抛 `UnicodeDecodeError`（daemon.log 里见过），加 `encoding="utf-8", errors="replace"`；② `scripts/live_install.py` 的 `stop_old_gateway` 按命令行含网关目录找进程，但开机自启的网关命令行是相对路径 `.venv\Scripts\python.exe main.py`，找不到，重启网关请按端口 5000 的监听 PID 杀（连父进程 uv shim 一起）；③ 可用资金一直读到 0.0，而持仓在（`002655 600 600`），确认是账户里真没有现金还是解析问题；④ 电脑要关屏保/睡眠/自动锁屏，否则模拟点击打不进客户端。
4. 本机有未提交代码时 `git rebase` 直接失败 → 推送「同步冲突」提醒，和日志「数据仍会正常推送」不符（影响小）。
5. 卖飞统计（`sell_followups`，报告「卖飞回看」）攒两三周后，用来评估各策略卖出参数。
6. 新指标可用：连涨天/连跌天（含今天）、阶梯缩量/阶梯放量（N=2～5，含今天，按已开盘分钟折算全天量，09:45 前不算）。按 09-24 收盘算，3 阶缩量命中约 596 只，单用太松，适合组合。
7. 用户粘贴过的中转站 key 需用户自行作废。中转站已不再使用，配置已从 `~/.claude/settings.json` 删除。

## 省额度的做法（用户关心）
- 一件事做完就换新会话，上下文超过 15～20 万也换；换前把本文更新好。
- 一条消息多说几件事，少贴大截图；中途可用 `/compact`。
- 实现、审查这类活派子 agent；日常活可以用 Sonnet。

## 验证面板
`scripts/panel_preview.py`：只读预览服务（真实数据库，拦截开始运行、保存/停用规则），端口 8765。周末或盘前试跑时，阶梯缩量/放量显示「—」是设计如此（今天没行情不折算）。
