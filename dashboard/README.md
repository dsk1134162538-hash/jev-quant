# 衡策 · AI 量化研究室

`jev-quant` 的本地可视化面板首版，使用 React + Vite 构建。

当前包含：今日总览、决策记录、早中晚复盘、策略验证、系统状态，以及决策详情、搜索筛选、指数切换、Markdown 导出和移动端导航。

> **当前全部为演示数据。** 本目录没有后端、真实持久化、实时行情或交易能力；策略状态仍为 `insufficient_data`，AI 真实委托未启用。

## Windows 桌面版

在本目录执行：

```bash
npm run desktop:install
```

脚本会在依赖缺失时自动安装依赖、构建离线单文件，并在当前 Windows 用户桌面创建 **“衡策 AI 量化研究室”** 快捷方式。以后直接双击桌面图标即可，不需要启动开发服务器或保持终端运行。

桌面快捷方式使用本机 Microsoft Edge 的应用窗口模式，直接打开 `dist/desktop.html`，不显示普通浏览器地址栏。它不是独立 exe，也不要求额外下载 Electron。家里电脑拉取仓库后，在 `dashboard` 目录执行同一命令即可按本机路径重新创建图标。

如已构建且只想重建快捷方式：

```bash
npm run desktop:install -- -NoBuild
```

桌面窗口回归测试：

```bash
npm run desktop:test
```

## 本地开发

建议使用 Node.js 20 或更高版本。

```bash
npm install
npm run dev -- --port 7100 --strictPort
```

打开终端显示的本地地址。开发服务支持通过 `--host`、`--port` 等参数覆盖监听地址和端口。

## 构建

```bash
npm run build
```

静态产物输出到 `dist/`：普通站点入口为 `index.html`，桌面离线单文件为 `desktop.html`。该目录和 `node_modules/`、`artifacts/` 均已加入仓库根目录的 `.gitignore`，`package-lock.json` 应保留提交。

## 浏览器回归测试

```bash
npm run test:ui
```

测试会先构建，再通过 Playwright 调用本机 Microsoft Edge，直接加载构建后的静态文件，无需启动开发服务器。覆盖桌面总览、决策详情、复盘导出、移动导航和移动端横向溢出检查。

测试截图与结果写入 `artifacts/`，不会提交到 Git。运行环境需要已安装 Microsoft Edge。

## 演示内容与交互边界

- 演示行情、决策、复盘和系统状态集中在 `src/data/demo.js`。
- 页面结构和交互位于 `src/App.jsx`，样式位于 `src/styles.css`。
- 页面导航、筛选和弹窗只保存在当前浏览器内存中，刷新后不保留。
- 复盘导出仅把当前演示稿件生成 Markdown 文件，不代表自动复盘已经运行。
- 页面没有真实下单、撤单或一键切换实盘的入口。

## 家里电脑的后续接入顺序

1. 先接只读 SQLite 与网关健康状态，核对字段、时间戳和异常处理。
2. 再接入只读行情、资金、持仓、意图、委托和成交快照，填充早中晚真实复盘。
3. 本地 API 与前端分层，不让浏览器直接绕过执行器调用交易接口。
4. 全链路先保持 `dryrun`；现场验证完成不等于自动开启实盘，真实委托仍须单独经过执行器与安全边界。
