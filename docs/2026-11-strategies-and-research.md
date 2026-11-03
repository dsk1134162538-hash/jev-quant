# 2026-11 新增：策略、研究结论、运维问题（给接手的 Claude 会话）

更新：2026-11-03。先读 `docs/HANDOFF.md` 的「接手第一步」，再读本文。GitHub Issues #4~#8 有同样的记录。

## 一、现状一句话

模拟盘现在有 **打首板（AI 版）、高标接力、弱转强、补涨、冰点修复、小市值轮动、AI 灵活交易** 七类在跑的策略，老的低吸/超跌反弹/情绪轮动/扫首板老规则 11-01 起被 `LEGACY_BUY_ENABLED=False` 关了买入（只卖）。**全部是模拟，实盘不跟**；实盘 `live.py` 目前是暂停状态，网关关着，开不开由 dj 决定。7 年日线回测里没有任何一个机械规则有稳定正收益（见第四节），所以这些策略的角色是「攒真实样本、做对照」，不是赚钱保证。

## 二、策略一览（账户本金都是 12 万，除非另说；配置全在 `jevq/config.py`）

| 策略 | 账户 | 买 | 卖 | 代码 |
|---|---|---|---|---|
| AI 打首板 | `aiboard_main/shadow` | 09:30–10:30 涨到 9% 扫板；闸门由规则定（昨日跌停>涨停且接力红盘率<40% 才空仓），DeepSeek 只否决个股 | 次日开盘卖（`AI_BOARD_SELL_OPEN`） | `jevq/strategies/board.py`、`jevq/rules/ai_board.py` |
| 高标接力 | `relay_main/shadow` | 昨日 ≥3 连板的主板票，竞价高开 3~6%，开盘价买，每天最多 3 只 | 下一交易日 14:55 后 | `jevq/strategies/relay.py::RelayStrategy`、`jevq/rules/relay.py` |
| 弱转强 | `wts_main/shadow` | 昨日炸板且炸板前 ≥2 连板，竞价高开 0~6% | 同上 | `WtsStrategy`、`jevq/rules/wts.py`、`jevq/data/wts_pool.py`（盘前用新浪日线看昨日最高价） |
| 补涨 | `bz_main/shadow` | 题材龙头 ≥5 板，同题材昨日首板，竞价高开 0~3% | 同上 | `BzStrategy`、`jevq/rules/themes.py::bz_pool`（盘前用同花顺涨停揭秘） |
| 冰点修复 | `ice_main` | 连续两个冰点日（主板跌停 ≥10 家且炸板率 ≥40%）的 14:50 买 20 只一篮子 | 持有 3 个交易日 | `jevq/strategies/ice.py`、`jevq/rules/ice.py`；每天 14:50 记一条 `decisions(strategy='ice', verdict='state')`，第二天靠它判断「昨天是不是冰点」 |
| 小市值轮动 | `sc_main` | 60 日均成交额最小 10% 里选 20 只，每 20 个交易日调仓 | 持满 20 个交易日 | `jevq/strategies/smallcap.py`、`jevq/rules/smallcap.py` |
| AI 灵活交易 | `aiflex_main` | DeepSeek 自己判断（见下） | 自己判断 + 自设止损 | `jevq/strategies/flex.py`（daemon 执行器）、`dashboard/flex_trader.py`（ai_loop 里的「交易员」） |

`relay/wts/bz` 共用 `AuctionStrategy`（`jevq/strategies/relay.py`）：09:30~09:33 按开盘价买，主账户先过 Jev 公告/消息排雷，影子只按规则，每个账户当天只买一轮。

**接线的坑（11-03 事故）**：新策略的 `can_buy` 必须用 `not (late or data_sell_only)`，**不能用 `sell_only`**——`LEGACY_BUY_ENABLED=False` 会把 `sell_only` 置位（`run.py`）。回归测试 `tests/test_run.py::test_relay_still_buys_when_legacy_rules_are_off`；每个新策略上线后要在 daemon 日志里核对启动那一行。

## 三、AI 灵活交易（aiflex）怎么工作

1. `ai_loop` 每次循环问一下 `flex_trader.due_slot(now)`；关键时点（09:26、10:00、10:30、11:15、13:20、14:00、14:35，到点 20 分钟内）各跑一轮：`gather_context` 拼现场（市场状态、题材梯队含成员代码、各策略标的、候选实时行情、账户与自设止损、今天前几轮的想法）→ 调 DeepSeek（`ask_workbuddy`，提示词在 `SYSTEM_PROMPT`：知识库手册 + 回测校准 + 明确允许突破规则）→ `validate_answer` → 写 `dashboard/runtime/flex_orders.json`（订单 10 分钟有效）→ 推送「🧭 灵活交易」。日志 `flex_log.jsonl`，状态 `flex_state.json`。失败不记为已跑，窗口内还会再试，也不影响主循环的退避。
2. daemon 里 `AiflexStrategy` 每分钟读订单文件，按实时行情成交，并**实时执行 AI 自己设的止损**。订单、拒绝原因、止损位都记在 `decisions(strategy='aiflex')`（verdict：`order_ok` / `order_reject` / `stop`）。
3. **四条护栏由代码强制，AI 绕不过去**：单票 ≤30% 净值、持仓 ≤5、亏损持仓不加仓；买入必须带止损（低于现价、≤12% 远）、「错了的信号」、模式（模式内/模式外）；当日亏 3% 或连亏 3 笔当天不再开新仓；只做主板、T+1、订单只成交一次、现价偏离参考价 >2% 不追。
4. 复盘时把「模式内」和「模式外」分开统计，用成绩判断灵活是否比规则强。**样本要攒两三个月才有结论。**

## 四、研究结论（脚本都在 `research/`，只读，不碰账户；详细数字见 GitHub #7）

- 日线 + 次日卖出 + 扣 0.15% 双边：打板、二板、龙回头、高标接力（2019–2026 −0.84%/笔，仅 2025/2026 为正）、妖股跟随、低吸（−0.1~−0.3%）、超跌反弹、8 个日频因子（32 格全负）都没有稳定正收益。
- 小市值（成交额最小 10%、20 天调仓）：十分位单调、两个口径一致，实盘式组合年化约 12~15%、最大回撤约 −35%，逐年极不均匀；**未校正退市票的幸存者偏差，偏乐观**。
- 知识库规则（`research/kb_rules.py`、`kb_events.py`）：冰点修复（连续第 2 个冰点日买全市场持有 3 天，21 个事件均 +2.34%、81% 为正）和高标烂板弱转强（日均 +0.72%、t≈2.6）有苗头；一进二（首板+量能+换手+市值）全负。
- 题材梯队（`research/theme_ladder.py`，同花顺涨停揭秘一年）：一致高潮日（题材涨停 ≥8 家、有先锋、梯队完整）次日竞价追买更差；题材涨停数回落的分歧日更好；补涨小幅高开 0~3% 为正、3~7% 为负。
- 盘中状态预测追板（`intraday_regime.py`）：10:00 封板数越高越不亏，但最好的档位仍是负。
- **教训**：短窗口结论先拉长历史再上线（高标接力两年为正、七年为负）。

## 五、数据与文件

| 文件 | 内容 |
|---|---|
| `data/minutes.sqlite`（运行目录，不进 git） | `minute_bars/minute_days`（每天最高 ≥6% 的票的分时，前一存档日摸过 6% 的票次日也存）、`touch_snaps`（主板第一次涨到 9% 的盘口快照）、`news_flash`（财联社/东财快讯带时间戳）、`open_snaps`（每天第一笔行情的开盘价、量额、五档，攒竞价量比） |
| `dashboard/runtime/strategy_targets.json` | daemon 盘前写：今天高标接力 / 弱转强的标的，给推送的【其他策略标的】用 |
| `dashboard/runtime/theme_cache.json` | 同花顺涨停揭秘缓存（今天 120 秒、前一天每天一次） |
| `dashboard/runtime/flex_orders.json` / `flex_log.jsonl` / `flex_state.json` | 灵活交易订单 / 每轮记录 / 今天跑过的时点 |
| `research/artifacts/first_board/`（不进 git） | 日线 `kline*.sqlite`（2019 起，腾讯 + 新浪补齐）、5 分钟线 `min5.sqlite`、涨停揭秘 `theme_zt.sqlite`（2025-10 起）等 |

**知识库在哪**：`jevq/app/knowledge.json`（面板「知识库」页，Telegram @gsxt233 整理，10 个专题约 250 条要点）。灵活交易的提示词是从它压缩来的。

## 六、运维问题（别再踩）

1. **本机代理 `127.0.0.1:8889`（v2ray）间歇性抖动**：pushplus、东财、同花顺、新浪被绕道代理，一抖就 `ProxyError`，微信推送丢失、取行情变慢。`jevq/netproxy.py` 在 `import jevq` 时把国内站点加进 `NO_PROXY`（requests 和 node 都认）。
2. **`create_snapshot` 里一只候选行情无效不能跳整轮**（11-03 一天跳 4 轮）：现在只丢那一只，持仓票无效才跳。**答案过期（>180 秒）仍然推分析**（标题带延迟），只是不处理模拟账户（`notify_stale`）。
3. **机器被其他项目占满 CPU 会让 AI 复盘变慢到全部超时**（11-03：`tuxun-assist` 常驻进程占约 4 个核）。dj 要求不动它。看 `dashboard/runtime/ai_loop.log` 里每轮 `seconds`，正常 30~80 秒。
4. **腾讯日线 `fqkline` 批量拉取会触发 WAF 501**；研究用的日线改走新浪（`research/kline_sina_fill.py`、`akdata.fetch_daily_bars_sina`）。实时/分时接口不受影响。
5. 开盘时段（09:15–15:30 北京时间）**不重启 daemon / live.py**；终端系统时钟是 GMT+1，判断时间用 `[DateTime]::UtcNow.AddHours(8)`。ai_loop 是独立进程，可以单独重启。
6. 旧账户 `main/shadow` 各挂着一只 9-29 的持仓（605566、600641），没人卖，待 dj 决定（#6）。

## 七、上线后核对清单

- daemon 日志（`data/run-YYYYMMDD.log`）出现：「高标接力：昨日 3 板以上…」「弱转强：昨日炸板的高标…」「补涨：昨日涨停…」「冰点修复：已加载」「小市值轮动：已加载」「AI 灵活交易：执行器已加载」；
- 09:26 的 AI 复盘推送里有【其他策略标的】和【题材梯队】；
- 09:30~09:33 接力/弱转强/补涨有决策记录或成交（符合条件时）；
- 灵活交易 09:26 起每个时点有「🧭 灵活交易」推送，订单被拒的原因在 `decisions` 里；
- 收盘后 `data/minutes.sqlite` 有当天的 `touch_snaps`、`open_snaps`、`news_flash`、`minute_bars`。
