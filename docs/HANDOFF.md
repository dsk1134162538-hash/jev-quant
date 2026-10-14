# 交接说明（给接手的 Claude 会话）

更新：2026-09-30（云端会话，实盘跟单开发收尾时整理）。新会话先读完本文，再按「接手第一步」做。

## 一句话
A 股主板（60/00）T+1 模拟盘：规则选股 → Jev（TypeSafe System One 模型）拦公告雷和消息面 → 主账户按 Jev 结论买、影子账户同规则不走 Jev 做对照。
**新增实盘跟单**（设计 `docs/specs/2026-09-29-live-trading-design.md`）：模拟账户 `regime_main`（情绪轮动，2026-10-09 起；之前是 `rebound_main` 超跌反弹·主）每成交一笔，家里电脑的 `live.py` 在东亚前海真实账户里跟一笔，单只用全部可用资金（2026-10-08 用户允许，原来最多 2 万；`config.LIVE_BUDGET = None`）、最多持 1 只；手机（Tailscale）上可暂停/恢复/立即卖出。低吸策略已下线。

用户风格见全局 `~/.claude/CLAUDE.md`：中文，结论先行，先问再做，破坏性/对外操作先确认，关键权衡给一个推荐。

## 接手第一步
1. `git log --oneline -15` 看最近做了什么（账本只在写它的机器上，**以 git 为准**）。
2. 看下文「待办」，挑用户这次要做的；做新功能按 superpowers 流程：brainstorming → spec（`docs/specs/`）→ plan（`docs/plans/`）→ subagent-driven-development。实盘跟单这一轮的账本在 `docs/plans/2026-09-29-live-trading-progress.md`（全部 Ruling 都在里面），终审结论 `docs/plans/2026-09-29-live-trading-final-review.md`。
3. 改完代码：全量测试 `.venv/Scripts/python -m pytest -q -p no:cacheprovider` 必须全绿；家里电脑跑测试先去掉凭证：`env -u TYPESAFE_API_KEY -u PUSHPLUS_TOKEN .venv/Scripts/python -m pytest -q`。云端会话（Linux）用 python3.12 建 venv 装 `pytest requests pandas akshare pywebview` 就能跑全量；winreg / ctypes.windll / powershell 相关的失败先判断是不是环境差异。

## 内置策略（各有主账户 `*main` 和影子账户 `*shadow`，本金 3 万、全仓单票）
| 策略 | 账户 | 买 | 卖 |
|---|---|---|---|
| 低吸（**已下线**） | `main` / `shadow` | 历史成交、净值保留，面板/推送不再出现；显示名「低吸·主（已下线）」 | 设计 `docs/specs/2026-09-23-jev-lowbuy-papertrade-design.md` |
| 扫首板 | `board_main` / `board_shadow` | 09:30–10:30 涨到 9% 触发；近 10 日无涨停；龙头分按热门题材（**hi189 晚报点名的题材排前面，只调顺序**，见「hi189 文章」）；情绪闸门只记录不拦截（见待办 8） | 设计 `docs/specs/2026-09-23-multi-strategy-first-board-design.md` |
| 超跌反弹 | `rebound_main` / `rebound_shadow` | 近 5 日跌 0～20%、近 40 个交易日无 2 连板、当日 0～-3%；**成交量连续缩量（含今天，不看涨跌）≥ 3 天才入选，≥ 4 天的优先**，同档内跌得多的优先；只看情绪（**情绪判断会带上 hi189 当日文章**）；**当天卖出的票要比卖出价低 1% 以上才再买**（**2026-10-08 起生效**，见下） | 独立规则 `jevq/rules/rebound_sell.py`：止损 -2%、低开拉起卖、跟踪止盈、最多 2 天。设计 `docs/specs/2026-09-24-rebound-design.md`（§7 变更记录） |
| 自定义 | `c{n}_main` / `c{n}_shadow` | 面板「规则」页搭（盘口指标墙），次日生效，最多 5 个 | 自己配的卖出规则，必须有止损。设计 `docs/specs/2026-09-24-rule-builder-design.md` |

截至 2026-09-30 库里有一个用户建的自定义策略「123」（c1，已停用；面板只能停用不能删除，历史成交挂在它的 id 上）。

**超跌反弹 2026-09-30 改规则（用户确认，10-08 首个交易日起生效；10-08、10-09、10-12 演练和 10-13 起的实盘跟单都按新规则，09-24～09-30 的成交是旧规则）**：
- **成交量**：连续缩量天数 = 今天（盘中按已开盘分钟折算全天量）< 昨天 < 前天 …，口径就是自定义策略的「阶梯缩量」N（`jevq/rules/metrics.py::shrink_days`）。≥ 3 天才入选，≥ 4 天排前面，其余排序不变。**09:45 前折算不了，不买**；量能历史缺失的票不入选；整批算不出来（`history_features` 失败）当日只卖不买。用户的意思：尾盘确定性最高——情绪偏弱/冰点日本来就只在 14:50 尾盘买，此时折算最准。按 09-30 收盘的库里数据估算，约 14%（425/3046）的票 ≥ 3 天、6.6%（200 只）≥ 4 天，再叠加其余条件后每天候选很少。
- **卖出后再买**：同账户当天卖出过的票（不论哪条卖出规则），现价要 ≤ `round(最近一次卖出价 × 0.99, 2)` 才允许再买；此前低开反抽卖出后会在同一分钟原价买回（无冷却）。
- 参数都在 `jevq/config.py`：`REBOUND_MIN_SHRINK_DAYS`（3，设 0 = 不看成交量）、`REBOUND_PRIORITY_SHRINK_DAYS`（4）、`REBOUND_REBUY_DROP`（1.0）。决策 trace 的 `candidate.shrink_days` 可以看某笔买入当时缩了几天。
- **同步清单（用户要求）**：改内置策略规则时，代码常量（`config.py`）→ 面板「规则」页说明（`jevq/app/api.py::builtin_summaries`，数字取自常量，新增的规则要手写一行，并在 `tests/test_api.py` 加断言）→ 本表 → 对应设计文档的变更记录，四处一起改，不能只改代码；改完还要让家里跑上新代码（休市日不会自动上线，见「运行现状」）。

## Jev 在哪里用
- **公告排雷**（`jevq/judge/notices.py`，设计 `docs/specs/2026-09-26-notice-risk-design.md`）：两段式——标题关键词/Jev 标题分类挑出十一类疑似 → 抓东方财富正文 → Jev 给高/中/低/无 → 风险分 `高×1+中×0.6+低×0.2`，≥0.5 否决、0.35～0.5「拿不准」也否决。**用户选的 A 口径：Jev 判「中」即否决。** 普通「股票交易异常波动」公告和资金占用汇总表不算疑似；「风险提示」类只看近 10 天。
- **消息面**（`jevq/judge/news.py`）、**市场情绪**（`jevq/judge/market.py`，每 10 分钟刷新）。
- Jev 出错 = 当天不买这只（保守）；卖出从不依赖 Jev。
- **hi189 文章**（`jevq/data/hi189.py`，设计 `docs/specs/2026-10-04-hi189-articles-design.md`）：读 `https://hi189.com/category/gupiaoqihuo/feed`，取上一交易日 15:00 后发布的公开文章（标题「密码保护：」开头的跳过，不绕过密码），08:50 起随情绪刷新（每 10 分钟）查到 11:30，正文缓存在 `day_cache`（kind=hi189）。用在两处：① 情绪判断的 state 加「市场解读」（每篇截 3000 字）；② 扫首板热门题材排序，文章点名的优先（代码比对，不经 Jev）。trace 记文章标题/链接，候选有 `article_hot`；复盘报告有「今日参考文章」。**会影响超跌反弹的买点**（它只看情绪）。总开关 `config.HI189_ENABLED`。云端沙箱访问不了 hi189.com；晚报解析已用 2026-09-30 晚报真实文本（6999.html，`tests/fixtures/hi189_evening.txt`）校准：版式是「板块名 / 说明文字（可能硬换行）/ 涨停板：… / 其他强势股：…」，个别板块只有「强势股：…。」。**RSS 抓取已在家里实测（2026-10-04）**：订阅源 10 篇、`pubDate` 时区对、正文带全文。上线后第一个交易日仍看日志 `[hi189]` 和报告「今日参考文章」。
  **早报、中报（2026-10-04 加）**：站上每天发早报（08:50~09:41，标题带「(早)」）、中报（13:00~14:20，「(中)」）、晚报（21 点后，「(晚)」），周末版和「深度复盘」没标。① 早报：`parse_morning_hot` 读「一、昨日热点」下每行「板块：个股」（到下一个「二、」为止，只记点名，不当涨停），和晚报板块一起进扫首板题材排序（早报每条要闻末尾的「板块：个股」是受益标的，不读）；早报本来就会作为全文交给 Jev。② 中报：联网截止从 11:30 延到 `HI189_FETCH_UNTIL = 14:40`（14:50 尾盘买点前最后一次刷新）；**默认只记录不参与判断**（`HI189_MIDDAY_JUDGE = False`，复盘报告里标「【只记录，未参与判断】」），演练几天对比后再放开；中报没有板块结构，不进题材排序。「密码保护：红宝书」仍跳过。注意情绪每 10 分钟刷新，14:30 以后才发的中报可能赶不上 14:40 的最后一次取。
- 交易循环每分钟：**所有策略先卖**，再依次买（扫首板最先）；每分钟 Jev 总预算 40 秒，没判到的候选下一分钟再判。

## 两台机器（2026-09-30 起：后台搬到家里）
- **公司电脑** `D:\claude\jev-quant`：后台 2026-09-30 09:09 已停，开机启动项改名为 `Jev模拟盘后台.lnk.disabled`（禁用）。不再跑任何东西。
- **家里电脑**：开发目录 `E:\claude\jev-quant`（写代码、跑测试）；**一键安装/更新 = 双击开发目录里的 `home_setup.bat`**（拉 master → 开发目录 .venv → easytrader → `scripts/live_install.py` 装 xiadan-gateway 到 `E:\xiadan-gateway`〔固定审查过的提交、token 生成后写网关配置和 HKCU、读 Tailscale 地址设 JEVQ_LIVE_HOST、开机自启「xiadan网关」〕→ `scripts/home_install.py`）。运行目录 `E:\jev-live`（只跑不改），由 `scripts/home_install.py` 安装/更新：clone/rebase **master**、建 .venv、放入公司迁来的数据库（已存在绝不覆盖）、检查凭证（只说有没有）、建开机启动项、启动 `daemon.py` 和 `live.py`；交易时段（09:15–15:30，含收盘后同步/上线）拒绝重启，除非 `--force`。用户操作步骤全在 `docs/live-setup.md`（时间表、同花顺设置、手机、测试、切实盘条件、日常使用）。
- 数据库快照在 GitHub 临时分支 `migration-data`（7b011c6）。**家里装好后删掉这个分支——删之前先问用户。**
- GitHub 仓库 `dsk1134162538-hash/jev-quant` 是**私有**的。决策留痕（含 Jev 细节）不导出。

## 运行现状
- `daemon.py`（模拟盘，pythonw 常驻）：工作日 08:50 跑 `run.run_day`，15:05 出复盘报告 `reports/YYYY-MM-DD.md`，然后导出数据、同步 GitHub；它还每 60 秒看一次 `live.py` 的心跳（模式不是 off 时），超过 120 秒没更新就重启 live.py，每天最多 3 次。
- **自动上线已改成先测后合并**（`jevq/sync/deploy.py`）：fetch 后若 origin/master 的代码和运行中的不同（或比较失败），先在临时 worktree 测 origin/master，通过才 rebase + push + 重启；不通过继续跑旧代码、当天数据不推、微信提醒。
- **自动上线只在交易日收盘后触发**（daemon 写出当天报告后才走同步/上线，休市日 `run_day` 直接返回、什么都不做），所以节假日前后推的代码不会自动到家里。桌面快捷方式「Jev低吸模拟盘」跑的是 `E:\jev-live\desktop.py`，读的是运行目录里的 `ui.html`，运行目录没更新就看不到任何变化；daemon 换新代码只能靠重启（`live.py` 只在 15:10–17:00 检查新提交）。**手动上线** = 非交易时段在开发目录用 cmd 或 Git Bash 跑 `.venv\Scripts\python scripts\home_install.py`（rebase 运行目录、重启 daemon 和 live.py）。别在 PowerShell 工具里跑：它的控制台是 UTF-8，`install_autostart.py` 按 GBK 解码 PowerShell 输出会崩，而且崩在重启之前——代码更新了、后台还是旧进程。2026-09-30 20:56 已手动上线到 59a215c（面板美化 + 超跌反弹缩量/再买规则）；10-08 前再改代码要再上线一次。
- `live.py`（实盘执行，唯一操作真实账户的进程）：每秒一轮；手机页端口 8766，只监听 127.0.0.1 和 `JEVQ_LIVE_HOST`（家里电脑的 Tailscale 地址）；**绑定失败的地址后台每 10 秒重试直到成功**（2026-10-07 开机时 Tailscale 比 live.py 晚 3 秒起来，旧代码绑定失败后手机页再也没起来；日志「手机页已绑定」即补上了）；15:10 后若 git HEAD 变了就自我重启。日志 `data/live.log`。
- **面板顶栏的情绪/外围**：交易由 daemon 接管时面板自己的状态是空的，顶栏改为读今天最近一条情绪记录（2026-10-08 前一直显示「–」）。
- **公告同步在节假日日期报 `KeyError('代码')`**（akshare 那天没数据、返回的表没有「代码」列），只是日志噪声，不影响交易，没修。
- 国庆 10-01～10-07 休市，节后首个交易日 10-08。**09-30 盘中没有任何进程在跑**（公司后台 09:09 停、家里 15:43 才起来只出了报告），所以 09-28/09-29 买入的持仓（board 的 002244、rebound 的 603989/600499）09-30 没卖；10-08 开盘 `ran_on(09-30)` 为假，扫首板的「隔夜超期」规则会在开盘把 002244 两个账户卖掉，超跌反弹的持有天数按日历算，600499 已超 2 天、603989 当天 14:55 清仓——都是规则正常行为，不是 bug。旧 `main`/`shadow`（低吸，已下线）里还各挂着一只票（605566、600641），不再有人卖，也不在面板上。收盘价历史 `closes` 表截至迁移时 71 个交易日。
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
  - **2026-10-08 order-test 实测**：两次都是 A 还在、B 消失——「撤最后」只撤最后一笔（正确）；B「消失」是我们 `is_cancel_row` 的 bug：这个客户端不单列「撤买」行，而是把原委托的「操作」写成「买入全部撤单」（备注「全部撤单(股票)」、撤消数量=委托数量），旧代码见「撤」就当撤单记录行丢掉，撤单后程序找不到这笔、会判「委托状态查不到」自动暂停。已修（`_CANCEL_STATUS_IN_SIDE`，测试用真实行），**修复要等收盘后 home_install 才进 live.py**。用户 10-08 09:4x 已切 live（没跑完演练）。时钟偏差实测几秒内；下单返回委托号为空、合同编号能从当日委托读到。
  - 客户端设置备忘：「查询委托时显示撤单记录」10-08 改成了「是」（对上面的问题没有影响，撤掉的委托本来就在当日委托的「已撤单」分组里）；默认买卖价格「卖一价/买一价」下单前清空；「是否隐藏个人信息」= 是。
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
2. **✅ 2026-10-08 已切实盘（live）**：用户 09:4x 在本机切到 live，没跑演练。当天在场做完切实盘前的实测：order-test 第 3 次全程 PASS（撤最后只撤最后一笔、撤单后读到「已撤」〔修了 `is_cancel_row`，见实盘跟单段〕、委托时间 `HH:MM:SS`、合同编号可读、下单返回号为空、时钟偏差几秒内）；g 杀网关 → 13:10 自动暂停「连续读取失败，客户端可能掉线」、微信收到、重启网关（桌面快捷方式「xiadan网关」）+ 手机恢复正常。**注意：定时对账只在卖出时段跑，午休、收盘后网关挂了要到下一个交易时段才发现**；读失败后改为 1 分钟重试（`LIVE_RECONCILE_RETRY`），开盘后约 2 分钟内暂停。当天实盘跟买 002658 雪迪龙 500 股 @7.14（委托 3886，买时可用资金只有 4000）；用户另外手动全仓买了雪迪龙；**当晚用户允许程序全部卖出，已把程序账 002658 从 500 改成客户端总数 3100 股**（用户报的数，网关当时读持仓超时没能核对；成本按 7.14 估，`live_state` 里 `adopted:002658` 有记录），模拟盘卖出时实盘卖全部 3100 股。这是一次性的，「只卖程序自己买的票」的通用规则没改、卖了共达电声；允许用全部资金（`LIVE_BUDGET = None`）；模拟盘本金 3 万不改。报备由用户自己确认。下面第 3 条是原来的上线路径，留作参考。
2. **实盘上线路径**（验证码问题 2026-09-30 已通过 ddddocr 补丁解掉，家里 --n 10 三次实测 97.8%，见上方「✅ 已解」条）：终审发现修完 → 用户同意后合进 master → 家里 `home_install.py` → `live_probe.py` 实测定下单层 → 10-08、10-09、10-12 演练（dryrun）→ 用户报备确认 → 最早 10-13 本机切 live。每个演练日收盘后看复盘报告「实盘跟单（演练）」段和微信小结。
3. **自训练验证码模型（2026-09-30 已完成部署，见上方「✅ 自训练验证码模型已部署」）**。后续可选：演练三天攒的 `captcha_harvest` 样本再训一轮，单次通过率 87% → 95%+；训练要点见那条。
   **家里状态（2026-09-30 晚）**：`home_install.py` 已跑（授权后），`E:\jev-live` 里 daemon.py 和 live.py 在跑、模式 off，开机启动项已建；桌面有完整面板快捷方式「Jev低吸模拟盘」（名字是旧的，指向 `E:\jev-live\desktop.py`，没打开验证过）；PushPlus token 和 Tailscale（电脑 100.72.84.113，手机已连）已配；网关已停（要用双击 `E:\xiadan-gateway\start_gateway.bat`）。**没做**：a–e（交易时段 order-test 等）和 g（杀网关看自动暂停）——都要盘中，等 10-08 演练当天在场做；g 需要 live.py 在 dryrun 且当天 trading_day 标记存在。
   **发现待修（没改）**：① `jevq/sync/deploy.py` 第 29/36/40 行 `subprocess.run(text=True)` 没指定编码，中文 Windows 上按 gbk 解码 UTF-8 输出会抛 `UnicodeDecodeError`（daemon.log 里见过），加 `encoding="utf-8", errors="replace"`；② `scripts/live_install.py` 的 `stop_old_gateway` 按命令行含网关目录找进程，但开机自启的网关命令行是相对路径 `.venv\Scripts\python.exe main.py`，找不到，重启网关请按端口 5000 的监听 PID 杀（连父进程 uv shim 一起）；③ ~~可用资金一直读到 0.0~~ 用户确认账户里可用资金确实是 0.0，读得对（持仓 `002655 600 600`，资金全在股票里）——注意：可用资金为 0 时实盘跟单买不了新仓，要先有现金；④ 电脑要关屏保/睡眠/自动锁屏，否则模拟点击打不进客户端。
4. 本机有未提交代码时 `git rebase` 直接失败 → 推送「同步冲突」提醒，和日志「数据仍会正常推送」不符（影响小）。
5. 卖飞统计（`sell_followups`，报告「卖飞回看」）攒两三周后，用来评估各策略卖出参数。
6. 新指标可用：连涨天/连跌天（含今天）、阶梯缩量/阶梯放量（N=2～5，含今天，按已开盘分钟折算全天量，09:45 前不算）。按 09-24 收盘算，3 阶缩量命中约 596 只，单用太松，适合组合。
8. **约 2026-10-30：首板情绪闸门复盘。** 10-08 起扫首板每天第一次买入阶段记一条 `decisions`（`strategy='board'`、`verdict='gate'`、`code=''`，面板「Jev 判断」里显示成「闸门」），内容是按上一交易日收盘前最后一次情绪统计算出来的「会拦 / 放行 / 无法判断」和原因；**只记录不拦截，买卖照旧**（规则 `jevq/rules/board.py::sentiment_gate`，线是炸板率 > 30%、跌停 > 10 家、最高连板 < 3，来自知识库里各家比较一致的说法，没经过自己数据检验）。攒三周左右后，把首板两个账户的成交按买入日是「会拦」还是「放行」分两组比盈亏，再决定要不要真的拦。没有直接让影子账户去拦：影子账户的作用是「不经 Jev」的对照，再加一道闸门就分不清差异来自哪边。
9. **约 2026-10-30：超跌卖出 A/B 复盘。** 10-08 晚建了两个自定义策略（只跑模拟盘，10-09 生效）：`c2` 超跌对照、`c3` 超跌宽回落1.5，选股/买点/止损/到期完全一样，只差跟踪止盈的回落线（0.8 点 vs 1.5 点）。起因：002218 3.67 元买入，回落 0.8 点（约 3 个价位）就被止盈在 +1.4%，当天最高 +3.8%、次日 +6.3%。比较看 c2 vs c3，**不要拿它们和 `rebound_main` 比**：自定义策略复现不了超跌反弹的几处细节——「近 40 日无 2 连板」用「近 20 日没涨停过」代替、排序只按 5 日涨幅（没有「缩量 ≥4 天优先」）、没有低开反抽和「主买多先留着」、跟踪止盈的最高涨幅按昨收算而不是按买入价算。spec 在会话 scratchpad，库里 `custom_strategies` 有完整定义。
10. **约 2026-10-30：主账户止损确认 + Jev 走向复盘。** 10-09 起（用户定）`rebound_main`（实盘跟它）的止损改成：现价连续 2 分钟在 -2% 线下才算，确认后和低开反抽触发时问 Jev 今天走向（`jevq/judge/trend.py`，六档：冲高回落/高开低走/单边下跌/震荡/探底回升/低开高走），判「探底回升/低开高走」先留着、下分钟再看；-3.5% 硬止损不问 Jev；Jev 没答上照卖。`rebound_shadow` 保持旧规则当对照组。每次问 Jev 记一条 `decisions.verdict='trend'`（含分时输入和概率）。复盘：同一笔买入，主账户和影子的卖出价、卖后当天/次日最高（`sell_followups`）对比，看少扫了几次洗盘、真跌时多亏多少。起因：10-09 雪迪龙 10:14 瞬间跌到 6.98（-2.1%）止损，卖在当天最低。
11. **2026-10-10 起：超跌反弹情绪偏弱/冰点/未知不买**（用户 10-09 定，原来是等 14:50 尾盘买；10-09 当天按用户要求仍照旧尾盘买）。`rebound_mode` 返回 `"none"`；旧决策记录里的 `"tail"` 面板照旧显示。复盘时注意：弱势日空仓，交易笔数会变少。
12. **约 2026-11-06：自建板块复盘**（10-09 合进主线，只记录不进交易）。10-09 用户加了两条：①板块情绪每天全记（`OWN_SECTORS_JEV_MIN_STRONG=0`，有成员行情的板块收盘后都问 Jev）；②盘中每 30 分钟也问 Jev（`OWN_SECTORS_INTRADAY_SLOTS` 7 个时点，`IntradayRecorder` 后台线程、自己的库连接，表 `own_sector_intraday`，上一轮没跑完跳过、重启晚了不补跑），面板「自建板块」每个板块下显示当天盘中情绪。成员要收盘后 `refresh_step` 才有，所以 10-09 当天盘中没有记录，10-12 起才有盘中数据。用户要求：晚报里新出现的板块慢慢加进来（现有流程：`propose_from_articles` 记 proposed，面板写产品定义后启用）。
13. **情绪拐点抄底（10-09 晚做，用户要求直接实盘）。** 起因：10-09 上午恐慌（红盘最少 649 家、跌停 18 家），13:00 后修复，用户凭盘感抄底赚钱，程序的情绪标签 13:41 才转「正常」。规则 `jevq/rules/turn.py`（取自知识库几条：红盘不到 1000 家、恐慌释放后不再创新低、跌停降到约 5 家以内、成交额边际放大），引擎每分钟用股票池行情算广度（表 `market_breadth`），规则满足后 `jevq/judge/turn.py` 问 Jev（广度轨迹 + 外围 + 知识库几条经验 + 7x24 快讯 `jevq/data/news7x24.py`，财联社 + 东方财富）。实测 10-09 13:10 同一份数据 Jev 给 0.50~0.58（不给知识库只有 0.35），像掷硬币，所以默认 `TURN_JEV_REQUIRED=False`：规则满足就确认，Jev 只记录（`jev_agrees`）；改 True 则要 Jev ≥ `TURN_JEV_MIN`(0.55)。确认后当天锁定 `engine.turn_on` → 超跌反弹偏弱/冰点也盘中买（`rebound_mode` 返回 "turn"，买入理由前缀「拐点抄底｜」），实盘跟。每次问 Jev 记 `decisions.verdict='turn'`，确认时推送「💡 情绪拐点」。用 10-09 的 10 分钟数据回放：13:10 触发、上午 09:50 的假反弹不触发。7x24 快讯也进了市场情绪的 Jev 输入。卖出不变（次日冲高按跟踪止盈卖，最多 2 天）。**复盘**：每次触发的买点、次日最高、是否被继续砸；阈值都在 `config.TURN_*`。知识库里「做上一轮人气最高、调整最充分的老龙头」这条还没做（现在买的还是超跌反弹候选）。
14. **面板「情绪」页（10-09 用户要求）**：每天一根情绪 K 线（Jev 四档概率期望分：冰点 0 / 偏弱 1 / 正常 2 / 火热 3，开=第一次判断、收=最后一次），下面涨停/跌停柱，「拐」标记拐点确认，点某天看每 10 分钟明细。`jevq/app/sentiment_board.py`，读 `decisions.verdict='sentiment'`。
15. **情绪迟滞 + 开盘未定 + 网关预警（10-09 晚）。** `Engine._settle`：各策略用的情绪 = Jev 四档概率期望分（冰点 0~火热 3）带迟滞——越过档位分界 0.5/1.5/2.5 再多 `SENTIMENT_HYSTERESIS`(0.15) 才换档；`SENTIMENT_SETTLE`(09:45) 前沿用上一交易日最后一次的情绪。decisions 里 `label` 是生效的、`raw_label` 是 Jev 原判、`held` 写为什么没跟（开盘未定/迟滞）。用历史回放：09-29 一天翻 15 次 → 0 次，09-28 7 → 1，10-09 13:41 转正常不拖慢。拐点抄底不受迟滞影响（自己的规则）。网关预警 `Executor._gateway_precheck`：08:52、09:00 查网关 `/health`（不读客户端），任务卡死/没登录/下单程序没开/连不上推「网关异常」，恢复推「网关已恢复」。没做的：规则分与 Jev 融合（要先攒数据校准）。
16. **情绪轮动 + 低吸重新上线（10-09 晚，用户定）。** 实盘 `config.LIVE_ACCOUNT` 从 `rebound_main` 改成 `regime_main`（切换时实盘空仓）。`jevq/strategies/regime.py`：自己不选股不问 Jev，按生效情绪（迟滞后）跟三个策略主账户的成交——火热跟扫首板；正常 10:30 前只等扫首板、之后再跟低吸；偏弱跟低吸；冰点/未知不买；当天确认拐点则超跌反弹（拐点抄底）优先。同价跟买，原策略卖时同价跟卖（来源记在表 `regime_sources`），兜底：原策略已不拿且不是今天买的就补卖。扫板快扫（3 秒）里的成交也跟（`run._follow`）。已知局限：某策略主账户正拿着当时没跟的票，它当天不会再出买点，轮动跟不到。低吸用 9-23 版规则重新上线（`jevq/rules/lowbuy.py`、`jevq/strategies/lowbuy.py`，新账户 `lowbuy_main/lowbuy_shadow`，旧 `main/shadow` 历史不动）：0~-3%、5 日涨幅高的优先、主账户过 Jev；偏弱/冰点/外围偏弱只在尾盘买；次日高开止盈/摸 +1%/低开回 0 轴或反弹 1.5 点/-3% 止损/14:55 清仓。c2/c3 10-12 起停用（持仓按原规则卖完）。**复盘**：轮动 vs 三个单策略主账户的净值；拐点、偏弱低吸各自的胜率。
17. **扫首板、低吸细化 + 规则页情绪说明 + 删 c1~c3（10-09 晚，用户定）。** 扫首板：只做主线首板（题材强势 ≥5 只或早报/晚报点名，`MAINLINE_MIN`）；扫板那一刻同时段放量倍数（累计量 ÷ 前几天同一分钟，5 日分时 `tencent.fetch_day_minutes`，进观察池时预取）< 1.5 倍不扫（`VOLUME_MIN`，记 skip「缩量板不扫」），倍数写进成交理由「扫板x%｜量y倍」——用户原想「温和放量」，3 天 49 只回测反而 ≥6 倍最好，所以只排缩量、不设上限，攒数据再定；次日卖法按 162 只首板数据改：开盘 -3%~+3% 竞价不及预期开盘卖，≤-3% 不开盘止损、等反抽 1.5 点或 10:00、-6% 硬止损，>3% 原规则。低吸：5 日涨幅 +5%~+20%、缩量（今天折算量 < 昨天）、属于主线；止盈改跟踪（最高 +1% 后回落 0.8 点或回到成本），偏弱仍尾盘买。规则页加「大盘情绪（各策略共用）」卡片和每个策略的「情绪」一节。c1 已删（`custom_store.purge`：删定义和现金行，成交/净值/决策留着，`next_id` 不复用编号）；c2/c3 在 `CUSTOM_PURGE_WHEN_FLAT`，收盘后清仓了自动删（预计 10-13）。
18. **Vibe-Research 自动导出导入（10-10，用户要）。** 用户的投研面板 `E:\vibe-research-agent`（github.com/simonlin1212/Vibe-Research，用 Claude 订阅问答；不碰交易）。daemon 收盘后 `_default_after_close` → `jevq/sync/vibe.py::sync_day`：写 `data/vibe/`（`trades.csv` 实盘成交 + `docs/` 复盘报告、hi189 文章、知识库专题 MD；data/ 不进 git），再推进 Vibe——API(8765) 开着就用，没开就临时 `node orchestrator/src/api.ts` 推完关掉，token 读 Vibe 的 `.local/api.token`。成交进台账「交易流水」（账户「实盘」，note 前缀 `jev:live:<委托id>` 去重；卖出多于程序买入的补一笔期初持仓 `jev:opening:<代码>`），文档进「研报库」（`data/vibe/state.json` 记哈希，变了传新版删旧版）。只导实盘，不导模拟盘成交（会把持仓页搞乱）。用户手动买的票（如 10-09 共达电声 700 股）不在程序账里，要在 Vibe 里自己记。Vibe 没装好（没有 `orchestrator/node_modules`）就只写本地文件。`VIBE_ENABLED` 开关。同一次同步还写**论点 + 判据**（`sync_plans`）：实盘每个持仓（`live_positions`）一条论点（买入理由、来源策略，`jev:thesis:<代码>:<买入日>`）+ 止损判据（falsifier，文字按来源策略 `stop_text`）+ 到期裁决点（decision_point，超跌 = 买入后第 `REBOUND_MAX_HOLD_DAYS` 个交易日、扫首板/低吸 = 次日），Vibe 事件日历按到期日提醒；持仓没了就关论点、判据按卖出理由判（含「止损」→ met，否则 dropped）。只动 note 带 `jev:` 的记录。Vibe 启动器 `E:\vibe-launch\start-vibe.cmd`（桌面「Vibe 投研」）：设 `CLAUDE_BIN`（npm 版 claude 是原生 exe 外壳，Vibe 不认）和 `HTTPS_PROXY=127.0.0.1:8889`（命令行 claude 不走系统代理）；**这个 .cmd 只能写 ASCII**，中文注释会被 cmd 按 GBK 读坏、吞掉下一行。
19. **涨停补充数据（10-10，用户要）。** `jevq/data/ztpool.py`（接口对照 Vibe 的 data-access 脚本，MIT）：①东财「昨日涨停今日表现」→ `yesterday_stats`（均涨、红盘率、跌超 5%、再涨停、昨日连板均涨）并进 Jev 情绪统计（`Engine.zt_extra`，提示词里说明是赚钱效应）；②同花顺「涨停揭秘」原因题材 → `reason_sectors`（同一题材 ≥ `ZT_REASON_MIN`(2) 家涨停，过滤业绩/分红类非题材词）变成「涨停原因·X」板块，和 hi189 文章板块一起（`Engine.all_sectors`）给扫首板、低吸的主线判断当「点名」——理由里显示「板块：涨停原因·X」。跟情绪同频（10 分钟）刷新，失败返回空不影响原逻辑。decisions 情绪记录多了 `zt_reasons`（题材 → 家数）。`ZT_EXTRA_ENABLED` 开关。复盘时看：主线点名多了涨停原因后，扫首板候选质量变没变。
20. **隔夜消息催化 + 人气与资金（10-10，用户要「多接一点数据」，只记录不进交易）。** ①`jevq/data/rss_news.py` + `rss_sources.json`（106 个源，清单复制自 Vibe，MIT，19 个中文源）：run_day 盘前（09:15 等待之前）抓上一交易日 15:00 → 现在的新闻（10-10 实测 21 秒、97/106 源成功、730 条）；`catalyst.py` 按题材名匹配（东财概念 + 自建板块名/别名 + `CATALYST_ALIASES` 英文/别称词；两字中文词只用关键词表里列过的，「电商」「汽车」「黄金」太泛），存 `day_cache(kind='catalyst')`，复盘报告「隔夜消息催化」一节列前 15 个题材 + **当天该题材首板数**（board_events.concept 对得上的才算）。②`market_extra.py` 收盘后、报告前快照：东财人气榜（emappdata，只取排名）、同花顺热榜（带题材标签和几天几板）、同花顺概念资金流、龙虎榜（akshare），存 `day_cache(kind='market_extra')`，报告「人气与资金」一节。**东财 push2 接口经系统代理（VPN）会被断开**，所以人气榜、资金流都没用 akshare 里走 push2 的函数。开关 `CATALYST_ENABLED`、`MARKET_EXTRA_ENABLED`；测试里 conftest 一律关掉（要联网）。约 10-30 复盘：有催化的题材首板数是不是明显多，决定要不要当主线点名。
21. **10-10 夜间对今天三个提交找 bug，修了 9 处（`9ef9cf1..1d547ad` 之后的提交）。** ①涨停原因板块被 `article_hot` 按个股名也比对，涨停股本来就是强势成员又属于十几个概念，几乎所有概念都会被判成「点名」→ 现在 `涨停原因·` 前缀（`hi189.TOPIC_ONLY_PREFIX`）只按题材名比；②新闻抓取没有总时限（requests 的 timeout 只管单次读写）→ `fetch_all(budget=RSS_BUDGET 60s)`，过了 `CATALYST_DEADLINE`(09:20) 启动（盘中重启）直接跳过，不让交易循环等它；③龙虎榜傍晚才公布，收盘后报告那会儿当天的还没有 → 存上一交易日名下，报告标「上一交易日（MM-DD）」，同一只票多条上榜理由只留净买最大的；④Vibe 客户端走了 Windows 系统代理（VPN 例外里没有 127.0.0.1）→ `session.trust_env=False`；⑤同一只票第二次出现「接管前已有持仓」会被去重标记吞掉 → 第一次仍是 `jev:opening:<代码>`（已导入 Vibe 的不能变），之后 `jev:opening:<代码>:<卖出委托id>`，期初持仓现在紧挨在那笔卖出前面、日期取卖出那天；⑥`api_client` 打开的 api.log 不关、`health()` 为假时把不健康的连接当可用交出去；⑦涨停池里缺字段的行让整份作废 → 跳过那一行；⑧36氪地址 `36kr.com/feed` 返回网页不是 RSS → 改 `www.36kr.com/feed`；⑨匹配到完全同一批新闻的题材（光伏/光伏概念、华为四个）合并成一行，首板数加总。另：东财人气榜只有代码，名字借同花顺热榜；`ZT_EXTRA_TIMEOUT` 8→5 秒（每 10 分钟在买入阶段同步取两次，最坏拖 10 秒）。仍失败的源（Cloudflare 拦截 / 地址已变）：CleanTechnica、Renewable Energy World、FierceBiotech、FiercePharma、动点科技、Yahoo Finance、New Scientist、国际能源网（读超时），不影响整体。
22. **10-14 用户定：超跌主账户收紧 + 主账户跟踪止盈放宽。** 起因：超跌的「超跌」只要求 5 日涨幅在 -20%~0%、排序缩量 ≥4 天优先于跌幅，10-12/10-14 主账户买的 603373、600510 五日只跌 1.6%、1.4%；低吸众生药业 10-14 开盘 +1.4% 回落 0.8 点被洗掉，之后最高 +8.9%（主账户历史 3 笔跟踪止盈都在开盘半小时内，2 笔卖飞）。改：超跌主账户只买 5 日 ≤ `REBOUND_MAIN_RET5_MAX`(-10%)，缩量门槛（≥3 天）不变；超跌、低吸主账户跟踪止盈回落线 0.8 → 1.5 点（`REBOUND_MAIN_TRAIL_PULLBACK`、`LOWBUY_MAIN_TRAIL_PULLBACK`）。影子账户全照旧，当对照。情绪轮动镜像主账户，自动跟着变。复盘：主/影子同票的卖出价与卖后最高对比。
23. **10-14 用户定：情绪拐点修规则 + 必须 Claude 同意。** 起因：10-14 10:01 确认了拐点（Jev 也给 0.71），之后一路走弱、收盘红盘 22%——「恐慌低点」只是 09:30 开盘那一分钟的红盘 21%，跌停最多才 5 家。规则改：恐慌低点从 `TURN_PANIC_FROM`(09:35) 起算；新增 `ld_peak`：当天跌停峰值 ≥ `TURN_LD_PEAK_MIN`(10)。红盘门槛 30% 没改（改 15% 会误杀 10-09 的真拐点：那天 10 分钟采样最低 21%，但跌停最多 18 家）。回放：10-09 13:10、10-13 13:00 照样触发，10-14 不触发。把关：`jevq/judge/claude_cli.py`（本机 claude.exe -p、订阅登录、关工具、`--model sonnet`、显式 `HTTPS_PROXY`、删 ANTHROPIC_* 环境变量）+ `turn_claude.py`（给每 5 分钟广度 + 每 10 分钟涨跌停统计）；`TURN_CLAUDE_REQUIRED=True`：规则满足后 Claude ≥ `TURN_CLAUDE_MIN`(0.55) 才确认，没答上下一分钟再问、不认 5 分钟后再问；Jev 照问只记录（decisions 里 `prob`=Jev、`claude_prob`/`claude_reason`=Claude，trace 分 jev/claude）。回放 Claude：10-13 0.72/0.68、10-14 0.35；Jev 0.83、0.71。找不到 claude.exe 会推警告、当天拐点不会确认。额度：一次约 5k token 输入，拐点一天 0~几次；每 10 分钟的情绪若也换 Claude，一天 24 次约 12 万 token（API 价折约 0.8 美元/天），用户还没定。测试里 conftest 关掉 Claude。
24. **10-14 用户定：大盘情绪（含外围）、自建板块情绪直接切 Claude。** `jevq/judge/claude_jev.py` 的 `ClaudeJev` 和 `JevClient` 接口一样（score/choice 两种题，答案转成 Jev 格式），判断器只换客户端；Claude 没答上（超时 `CLAUDE_JUDGE_TIMEOUT` 25 秒、额度用完、格式不对）自动退回 Jev，`sentiment_trace.by` 记 claude/jev，调用照样记进 jev_calls（model=claude-sonnet）。板块只换情绪（`OwnSectorJudge(strength_jev=)`），成员归类仍用 Jev。新闻用上了：大盘情绪的 7x24 快讯放宽到 90 分钟 40 条，加「隔夜消息催化」前 10 个题材（`catalyst.summary_lines`）；板块情绪加「隔夜相关新闻」（`own_sectors.sector_news`，催化摘要里板块名/别名对得上的那条）。实测一次大盘 9 秒、板块 6 秒。开关 `SENTIMENT_BY_CLAUDE`、`SECTOR_STRENGTH_BY_CLAUDE`。注意：催化里英文科技媒体多，「苹果概念」常排第一（10-14 有 88 条），看情绪时别太当真。
25. **10-14 用户定：盘中全天量改按日内成交量曲线折算。** 起因：超跌老选到 5 日只跌 1%~3% 的票。查下来主因是「连续缩量（含今天）」里今天的量原来线性折算（量 ÷ 已过分钟 × 240），A 股成交两头多中间少，10:00 实际已成交约三成，线性按 12.5% 算，全天量估高 2.4 倍，活跃的深跌票几乎都被判成「今天放量」挡掉，上午只剩成交清淡、本来就没怎么跌的票（10-14 收盘时 17 只 5 日 ≤-10% 的票全部满足条件，10:01 一只都没进池子）。改：`metrics.VOLUME_PROFILE`（160 只股票 × 5 天分时的中位数曲线，每 5 分钟一点，插值）+ `projected_volume = 量 ÷ intraday_fraction(已过分钟)`。10-14 回放：深跌票 10:01 新折算 0.52~0.92 倍昨天（实际 0.51~0.97），旧的 1.23~2.19。影响所有用今天折算量的地方：超跌缩量、低吸缩量、自定义策略阶梯缩量/放量。曲线是静态的，市场成交结构大变时重统计一次（脚本思路：fetch_day_minutes 抽 160 只，每分钟累计量 ÷ 全天量取中位数）。
7. 用户粘贴过的中转站 key 需用户自行作废。中转站已不再使用，配置已从 `~/.claude/settings.json` 删除。

## 省额度的做法（用户关心）
- 一件事做完就换新会话，上下文超过 15～20 万也换；换前把本文更新好。
- 一条消息多说几件事，少贴大截图；中途可用 `/compact`。
- 实现、审查这类活派子 agent；日常活可以用 Sonnet。

## 知识库（面板「知识库」页，2026-10-02）
- **是什么**：用户让学 Telegram 频道 @gsxt233（公开预览 `https://t.me/s/gsxt233`）里的实战文章。376 篇带链接的文章里 269 篇有摘要、21 篇是重复发布、86 篇原文已被删只留标题；读完后整理成 10 个专题共 249 条要点（每条带出处）加一份文章索引。只读、带搜索，**只是参考资料，没有接进任何交易规则**。
- **内容在哪**：整份在 `jevq/app/knowledge.json`（`source` / `topics[].sections[].points[{text, refs}]` / `articles[{id, date, title, url, topic, points, dup, deleted}]`），接口 `Api.knowledge()` 原样返回，界面在 `ui.html` 的「知识库」段（`kbNavHtml` / `kbMainHtml` 是纯函数，`tests/test_ui.py` 用 node 跑）。要点和摘要都是用自己的话写的，不存原文；原文里明显夸大的说法（如“胜率九成”）在要点里标了不可当真。
- **怎么改**：直接改 `knowledge.json`。`tests/test_api.py` 会检查每条要点的出处都指向索引里有摘要、不是重复的文章，文章的专题必须是已有专题或空串。文章 `id` 是频道里的帖子号。频道有新文章时：从公开预览页取帖子列表，telegra.ph 用 `api.telegra.ph/getPage/<path>?return_content=true` 取正文（空页时看返回里的 `author_url`，多数在 mobile.aigupiao.com），读完再往 `articles` 和对应专题里加。登录态的 web.telegram.org 不好批量读，别走那条路。
- 「知识库」页不参加 5 秒定时刷新（和规则页、复盘报告一样），否则搜索框会丢焦点。

## 候选页（面板「候选」，2026-10-04）
只读展示此刻被选出的票，按买入优先顺序：扫首板观察池（文章点名 → 热度 → 龙头分，含 Jev 结论）、超跌反弹入选名单（缩量 ≥4 天优先 → 5 日跌得多的优先，含缩量天数、情绪和是否在买入时段）。后台 `run.py::_publish_candidates` 每分钟把快照写进 `day_cache`（kind=candidates，`jevq/data/candidates.py`），`Api.candidates()` 读最近一天的；盘后/休市看到的是最后一次快照。策略侧是 `BoardStrategy.candidates_view` / `ReboundStrategy.candidates_view`（不看买入时段，不影响买卖）。自定义策略的候选没做。家里只读面板用导出重建库，没有 day_cache，这页显示「还没有候选快照」。

## 自建板块（2026-10-04，只记录，不进交易）
按**产品收入占比 ≥ 50%** 自己划板块（≥30% 记「相关」不入板块），记录龙头顺序和 Jev 板块强弱，攒约 20 个交易日数据后再决定要不要用到选股。设计 `docs/specs/2026-10-04-own-sectors-design.md`（含数据探测结果），计划 `docs/plans/2026-10-04-own-sectors.md`。**不被任何策略 import，不影响买卖**；总开关 `config.OWN_SECTORS_ENABLED`。
- 代码：`jevq/data/own_sectors.py`（种子 `own_sectors_seed.json`、取数缓存、成员判定、龙头、日快照）、`jevq/judge/own_sectors.py`（Jev 三问：产品→板块映射、主营介绍、板块强弱）；表 `own_*`（`db.py`）。只收主板 60/00 股票池内的票。
- 流程（`run.py` 收盘后）：先 `snapshot_step`（报告之前，写 `own_sector_daily`）→ 写报告（有「自建板块」一节）→ `refresh_step`（报告之后，最多 `OWN_SECTORS_BUDGET`=600 秒：提议新板块 + 刷新成员，没跑完下个交易日接着跑）。
- 数据源：东方财富主营构成 `akshare.stock_zygc_em`（收入比例是小数，要排除「其中:」细分行）、同花顺主营介绍 `stock_zyjs_ths`（偶发连接重置，已重试）。产品行是收入类型（技术授权、金融业务）的公司走「Jev 读主营介绍」，成员 `pending`，**用户确认后才算**。
- 面板「自建板块」页：板块摘要（Jev 强弱、强势、涨停、龙头前三、晚报点名）、龙头顺序、成员依据；pending/auto 成员可确认/驳回，晚报提议的新板块要写产品定义才能启用。家里只读面板拦截这些写操作。
- 种子清单 `own_sectors_seed.json` 首版整理自 9-30 晚报，**待用户审**（产品定义写得越具体越好）。

## 验证面板
`scripts/panel_preview.py`：只读预览服务（真实数据库，拦截开始运行、保存/停用规则），端口 8765。周末或盘前试跑时，阶梯缩量/放量显示「—」是设计如此（今天没行情不折算）。
- **家里没有真实库**：`panel_preview.py` 里 `REPO` 写死公司路径 `D:\claude\jev-quant`，家里用不了。家里做视觉验收：起一个临时 HTTP 服务，把 `ui.html` 接到 `jevq.app.home_view.ReadOnlyApi`（先 `build_db(exports/, 临时路径)` 用导出重建库），页面里注入 `window.pywebview = {api: Proxy → fetch('/api/<方法>')}` 并在 load 后派发 `pywebviewready`（写法同 `panel_preview.py`）。内置浏览器的截图只截左上 800 px，宽窗口要用 `javascript_tool` 滚动/位移后多截几张，页面自带的 5 秒刷新会冲掉手动注入的内容（先 `window.renderTab = async () => {}`）。
- **面板视觉约定（2026-09-30 美化，纯样式，没改行为）**：`ui.html` 顶部 `<style>` 里是令牌（浅色默认、深色跟随系统，文字色都过了 4.5:1 对比度）；涨红跌绿是 A 股习惯，`.up/.down`；卡片只用 1px 细边框不加阴影，弹层（图表提示、规则弹框）只用阴影；不要用超过 1px 的彩色 `border-left`；图标是页面顶部 `<svg>` 里的精灵，用 `ic("x")` 取，不要再往界面里放 ▸ ✕ ❌ ⚠️ 这类字符；表格整列是数字的会自动右对齐（`table()` 里 `NUM_CELL`，「代码」列除外）；所有影子账户的净值线都是虚线（主/影子重合时主账户实线不会被盖住）；顶栏在窗口宽度 ≤ 1100 时不再吸顶。
