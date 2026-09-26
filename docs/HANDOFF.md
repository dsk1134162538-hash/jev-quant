# 交接说明（给接手的 Claude 会话）

更新：2026-09-26 11:20（公司电脑会话写；用户回家后会推送家里未提交的改动，家里新会话从这里接）

## 这是什么项目
A 股主板 T+1 模拟盘：规则选股 + Jev（TypeSafe System One 模型）拦截公告雷/消息面 + 影子账户对照。用户风格见全局 `~/.claude/CLAUDE.md`（中文、结论先行、先问再做、破坏性/对外操作先确认）。

- 设计：`docs/specs/`（低吸、扫首板、规则搭建器、公告风险细判 2026-09-26）
- 计划：`docs/plans/`（同名）
- 开发流程：superpowers:subagent-driven-development；每个计划有账本 `.superpowers/sdd/<plan>/progress.md`（本地，gitignore），接手前先读账本。

## 运行现状（公司电脑）
- 后台常驻 `daemon.py`（pythonw，启动项快捷方式「Jev模拟盘后台」），工作日 08:50 自动跑 `run.run_day`，收盘后自动导出数据、同步 GitHub、代码有变化且测试通过则自动上线（`jevq/sync/`）。
- 2026-09-26 11:16 已以 HEAD a1b2696 重启（在 ec41427 基础上多了：补齐整天缺失的交易日、买不进的候选不再卡住主账户、预抓正文跳过判官不读的公告）。下次运行：周一 2026-09-28 08:50。
- **家里推送的代码会自动上线**：公司 daemon 每个交易日收盘后 `fetch` + `rebase origin/master` + `push`，代码有变化且测试通过就自动重启到新代码。所以家里推到 master 的改动，最早周一 09-28 收盘后在公司生效；测试不过则不上线（会 PushPlus 提醒）。
- 项目 Python 是 `.venv/Scripts/python.exe`（系统 `python` 没装 akshare）。
- 2026-10-01～10-07 国庆休市，节后首个交易日 10-08。
- 凭证只在用户环境变量（HKCU\Environment）：`TYPESAFE_API_KEY`、`PUSHPLUS_TOKEN`。**绝不打印、不入库、不写进文件。** 重启 daemon 时从 HKCU 读进进程环境再启动，启动后移除。
- 数据：`data/jevq.sqlite`（不入库）。不要在盘中（09:15–15:05）重启后台。

## 刚做完 / 进行中
1. **公告风险细判**（计划 `docs/plans/2026-09-26-notice-risk.md`，账本 `.superpowers/sdd/2026-09-26-notice-risk/progress.md`）：全部完成并上线。用户选 A 口径：Jev 判「中」风险（分数 0.6）即否决。
   - **待办（约 2026-10-03）**：跑满一周后，拿被公告否决的候选对比影子账户后续涨跌，决定是否放宽（B 口径）。复盘报告有「公告风险统计」小节。
2. **成交量历史补数**：2026-09-26 已完成，并用新版 `run.py --backfill` 复跑确认。最近 22 个交易日（08-26～09-24）每天 3038–3047 行，volume 基本全覆盖（09-23/24 各 6 行为空，疑似停牌）。09-25 无数据是中秋休市，不是缺口。
   - 周末试跑（把 09-24 当「今天」）阶梯缩量命中：N=2 1195、N=3 596、N=4 197、N=5 85（股票池 3047）。已抽查 000061、000017 原始量吻合。N=3 单用太松，适合与其他条件组合。

## 已知待办
- 规则搭建器 Task 9：最终全分支审查仍待做（收盘价 ≥21 个交易日这一半已满足）。
- 用户粘贴过的中转站 key 需用户自行作废（中转站配置已从 `~/.claude/settings.json` 删除，不再使用）。

## 验证面板的方法
`scripts/panel_preview.py`：只读预览服务（真实数据库，拦截 start/stop/rule_create/rule_update/rule_disable），端口 8765。`D:\claude\.claude\launch.json` 的 `panel-preview` 配置可改为指向它。
