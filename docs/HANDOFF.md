# 交接说明（给接手的 Claude 会话）

更新：2026-09-26（Claude 桌面端长会话结束前写；新会话从这里接）

## 这是什么项目
A 股主板 T+1 模拟盘：规则选股 + Jev（TypeSafe System One 模型）拦截公告雷/消息面 + 影子账户对照。用户风格见全局 `~/.claude/CLAUDE.md`（中文、结论先行、先问再做、破坏性/对外操作先确认）。

- 设计：`docs/specs/`（低吸、扫首板、规则搭建器、公告风险细判 2026-09-26）
- 计划：`docs/plans/`（同名）
- 开发流程：superpowers:subagent-driven-development；每个计划有账本 `.superpowers/sdd/<plan>/progress.md`（本地，gitignore），接手前先读账本。

## 运行现状（公司电脑）
- 后台常驻 `daemon.py`（pythonw，启动项快捷方式「Jev模拟盘后台」），工作日 08:50 自动跑 `run.run_day`，收盘后自动导出数据、同步 GitHub、代码有变化且测试通过则自动上线（`jevq/sync/`）。
- 2026-09-26 已以 HEAD ec41427 重启（含：公告两段式细判、先卖后问 Jev、每分钟 Jev 预算 40s、连跌天/阶梯缩量/阶梯放量、成交量存库）。
- 凭证只在用户环境变量（HKCU\Environment）：`TYPESAFE_API_KEY`、`PUSHPLUS_TOKEN`。**绝不打印、不入库、不写进文件。** 重启 daemon 时从 HKCU 读进进程环境再启动，启动后移除。
- 数据：`data/jevq.sqlite`（不入库）。不要在盘中（09:15–15:05）重启后台。

## 刚做完 / 进行中
1. **公告风险细判**（计划 `docs/plans/2026-09-26-notice-risk.md`，账本 `.superpowers/sdd/2026-09-26-notice-risk/progress.md`）：全部完成并上线。用户选 A 口径：Jev 判「中」风险（分数 0.6）即否决。
   - **待办（约 2026-10-03）**：跑满一周后，拿被公告否决的候选对比影子账户后续涨跌，决定是否放宽（B 口径）。复盘报告有「公告风险统计」小节。
2. **成交量历史补数**：2026-09-26 在旧会话后台启动（新浪，约 1 小时）。新会话先检查是否补齐：
   `SELECT date, COUNT(*), SUM(volume IS NOT NULL) FROM closes GROUP BY date ORDER BY date DESC LIMIT 12;`
   近 10 个交易日 volume 覆盖率应接近 100%；不够就重跑（重复跑会跳过已补齐的）：
   见 `.superpowers/sdd/2026-09-24-rule-builder/review-volume-steps.md` 末尾的 backfill 命令（持 `runner.lock`，非交易时段跑）。
   合并 `claude/volume-supplement-ladder-test-l1bmyb` 之后也可以直接 `python run.py --backfill`（已改用新浪，并补最近 10 天的成交量）。
   补齐后在面板「规则」页试跑「阶梯缩量」，把命中数告诉用户。
   注意：旧版周末/盘前试跑阶梯缩量/放量恒为 0；该分支已修，周末/盘前用上一交易日库里的全天量判断。
   口径：N=3 = 三天前 > 两天前 > 昨天 > 今天，今天在内连续 3 天缩量（用户已确认）。

## 云会话 2026-09-26 修复（分支 `claude/volume-supplement-ladder-test-l1bmyb`，未合并 master，未上线）
- 周末/盘前试跑阶梯缩量/放量恒为 0 → 用库里最后一天的全天量当「今天」（`VOL_DAYS` 5→6）。
- 自定义策略主账户会卡在前排买不进的票（涨停/买不起一手）上，每分钟重复问 Jev、占检查名额，影子却越过去买 → 问 Jev 前先跳过，与低吸一致。
- 盘前自动补数和 `run.py --backfill` 还在用被封的腾讯 K 线 → 改用新浪 `akdata.fetch_daily_sina`。
- 后台 daemon 在交易时，面板试跑拿不到市场情绪/外围（面板状态为空），含这两个条件的规则试跑恒为 0 → 从决策表最近一条情绪记录读（引擎的情绪记录里顺带记外围）。
- 预抓公告正文不再抓细判不读的（例行异动、资金占用汇总表、超过 10 天的风险提示），免得占满 300 条上限和 60 秒预算。

## 已知待办
- 规则搭建器 Task 9（最终全分支审查 + 本地收盘价补到 ≥21 个交易日，用新浪 `akdata.fetch_daily_sina`）。
- 用户粘贴过的中转站 key 需用户自行作废（中转站配置已从 `~/.claude/settings.json` 删除，不再使用）。

## 验证面板的方法
`scripts/panel_preview.py`：只读预览服务（真实数据库，拦截 start/stop/rule_create/rule_update/rule_disable），端口 8765。`D:\claude\.claude\launch.json` 的 `panel-preview` 配置可改为指向它。
