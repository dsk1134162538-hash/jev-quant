# 衡策 · AI 量化研究室

`jev-quant` 的本地只读可视化面板，使用 React + Vite 和 Python 本地服务。

当前包含：今日总览、决策记录、早中晚复盘、策略验证、系统状态，以及决策详情、搜索筛选、Markdown 导出和移动端导航。

> 本地服务启动后，后台每 1 秒刷新三大指数、观察样本、模拟持仓和前三个行业领涨候选，全市场与行业广度按 30 秒缓存，不依赖页面是否打开。SQLite 通过 URI `mode=ro` 只读打开。服务只监听本机，没有下单、撤单、切换实盘或券商网关接口。
>
> 全市场覆盖沪深京 A 股，行业广度来自新浪行业。盘中数据完整且新鲜时，Kimi 每 5 分钟生成一次研究判断；市场宽度、行业上涨数量或观察样本价格明显变化时，可在 60 秒冷却后加急复核。原始回答与证据追加到本地 JSONL，历史记录不会被覆盖。Kimi 根据市场、行业、持仓和候选行情生成动态组合意图，只在独立模拟账户中完成风控、模拟成交与对账；秒级持仓风险检查只报警，不自动卖出，不产生真实交易指令。早中晚自动复盘尚未接入，样本外策略有效性仍需持续积累数据验证。

## Windows 桌面版

在本目录执行：

```bash
npm run desktop:install
```

脚本会在依赖缺失时自动安装依赖、构建面板，并在当前 Windows 用户桌面创建 **“衡策 AI 量化研究室”** 快捷方式。双击图标会启动本机只读服务并用 Edge 应用窗口打开，不需要保持终端运行。

如已构建且只想重建快捷方式：

```bash
npm run desktop:install -- -NoBuild
```

也可以直接双击 `start-dashboard.bat`。服务默认地址为 `http://127.0.0.1:7100/`。

## 本地开发

建议使用 Node.js 20 或更高版本，并先在仓库根目录准备 `.venv` 及项目 Python 依赖。

```bash
npm install
npm run dev -- --port 7100 --strictPort
```

开发命令同时启动 Vite 和 `127.0.0.1:8767` 的只读 API，并转发 `--host`、`--port` 等参数。生产模式不依赖 Node 常驻，由 `server.py` 直接提供 `dist/` 和 API。

## 构建

```bash
npm run build
```

静态产物输出到 `dist/`：普通站点入口为 `index.html`，离线降级单文件为 `desktop.html`。离线文件显示静态快照，不请求 API。`dist/`、`node_modules/` 和 `artifacts/` 均已加入仓库根目录的 `.gitignore`。

## 测试

```bash
npm run test:ui
npm run desktop:test
```

浏览器回归覆盖桌面总览、实时数据、决策详情、复盘导出、移动导航和横向溢出检查。桌面测试另外覆盖离线单文件的应用窗口启动。测试通过 Playwright 调用本机 Microsoft Edge，结果写入 `artifacts/`。

后端只读接口测试在仓库根目录运行：

```bash
.venv/Scripts/python.exe -m pytest -q tests/test_dashboard_server.py tests/test_dashboard_market.py
```

## 数据与交互边界

- `server.py` 合并腾讯只读行情和独立模拟账户；模拟资金、持仓、策略与成交单独保存在 `dashboard/runtime/ai_paper.sqlite`，不读取或改写真实交易账户。
- `market_snapshot.py` 负责沪深京全市场及新浪行业快照，首次载入约十几秒；覆盖不足或请求失败会明确标记，并保留最后成功数据。
- 默认 `DASHBOARD_DECISION_SOURCE=subscription`：本地服务不请求付费 API。Kimi Work 订阅定时任务每 5 分钟评估一次完整、新鲜的盘中数据，`subscription_bridge.py` 负责快照绑定、答案校验和本地提交；答案过期、不匹配或格式无效时不生成模拟策略，也不退回 Jev 或 Claude。只有显式设置 `DASHBOARD_DECISION_SOURCE=api` 时，`kimi_judge.py` 才会通过 `KIMI_API_KEY` 和 `KIMI_BASE_URL` 调用付费 API。
- AI 记录追加到 `dashboard/runtime/ai_decisions.jsonl`，服务重启后重新载入，并增量读取订阅任务追加的记录。`adaptive_paper.py` 将有效盘中判断转为动态组合意图，经确定性风控后调用本地 `Ledger` 模拟成交，并从初始资金和全部成交独立重算现金、持仓与 T+1；对账异常会暂停新的模拟策略。该目录已加入 `.gitignore`，不提供浏览器写接口。Claude 仅用于独立代码审查和人工发起的复核，不参与常规盘中判断。
- `subscription_bridge.py` 复用 `jevq.notify` 的 PushPlus 微信通道：每轮推送分析，每笔模拟成交另行推送，凭证只从本机环境读取。推送失败不影响模拟账本，未配置凭证时只静默跳过。
- 隔离模拟闭环不等于策略有效，也不等于自动实盘：真实委托仍未启用，执行器保持原有真实交易阻断和保护暂停。
- 历史决策、复盘和策略验证文案来自 `src/data/realSnapshot.js`；实时数据结构由 `src/data/useDashboardData.js` 管理。
- 页面导航、筛选和弹窗只保存在当前应用内存中，刷新后不保留；AI 判断不受此限制，已独立持久化。
- 复盘导出仅生成当前历史记录的 Markdown 文件，不代表自动复盘已经运行。
