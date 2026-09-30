# xiadan-gateway 审查结论（子 agent 通读源码所得，controller 转存）

锁定：`hylyq/xiadan-gateway` **1662adf5b7d0afc73d1ee2f0f15101104d2c07af**（2026-09-29，单一作者，166 个提交）。
结论：**有条件可用**——无恶意逻辑/遥测/自更新/混淆，不会自主下单撤单；uv.lock 812 个文件 sha256 与 pypi.org 一致。风险来自自动化本身。

## 安全要点
- 出站网络：本机 HTTP 服务（默认 127.0.0.1:5000）；可选 webhook（`alerts.webhook_url` 默认空 → 不发，保持为空）。
- **超时类错误后单子仍可能执行**：`QUEUE_TIMEOUT` 后任务还在队列里会照常执行；`TASK_TIMEOUT` 后 worker 可能仍在点下单。→ 调用方必须当作「结果不明、可能稍后报出」。
- 认证默认关闭；`auth.enabled=true` 且 token 非空才生效；token 走 `Authorization: Bearer` 或 `X-API-Key`；/health 免认证；带 `Origin` 头的请求一律拒绝（AUTH_FAILED）；**不校验 Host，不开认证时可被 DNS rebinding 读持仓 → 必须开 token**；`/actions/click` 能点任意坐标。
- 窗口监控：每 2 秒检查，只在 xiadan 最小化时恢复窗口；可 `window_monitor.enabled=false` 关（启动时读）。
- **每次 API 调用都会在交易窗口中央真实点一下鼠标、按 5 次 ESC；查询还会连按两次 Ctrl+C 并清空剪贴板 → 调用期间不能碰鼠标键盘**（用户要操作家里电脑必须先暂停）。
- OCR 全部离线（仓库自带模板）。
- 安装缺陷：numpy 顶层导入但不在基础依赖里 → 需 `uv add numpy`。
- **scripts/ 下的脚本会真实下单（如 stress_place_orders.py 一次 22 笔）——实盘机器上绝不能跑。**

## 实盘风险
- 未命中规则的警告弹窗会被自动点「是」；只有含「涨跌停/超出/价格」的点「否」。
- 快速交易模式下 `confirm=false` 不是预览，照样提交。
- 「成功」是推断的：点下单 0.2 秒内没弹窗就算成功；价格和数量不回读核对（只核对代码对应的名称）。
- **不传 price 就用客户端自动填的价；代码、数量不校验直接当按键敲 → 调用方必须自己保证 6 位代码、正整数 100 倍数数量、明确价格。**
- **`/orders/cancel-all` 不带参数 = 全部撤单。**
- 控件编号按作者券商版本写死 → 上线前用远离市价的小额限价单实测。

## API
返回：HTTP 永远 200。成功 `{"status":"success","request_id","timestamp","data",...}`；失败 `{"status":"error","error_code","message","suggestion"?,"details"?}`。路径/方法错 → Flask HTML 404/405（非 JSON）。

| 接口 | 参数 | data |
|---|---|---|
| GET /health（免认证） | — | service, version, xiadan_running, logged_in, queue_status, stats, config.recommended_client_timeout_seconds |
| GET /account/balance | — | 资金余额 冻结金额 可用金额 可取金额 股票市值 总资产 持仓盈亏 当日盈亏 当日盈亏比（字符串或 null） |
| GET /positions、/trades/today、/orders/pending | — | 列表，每行 dict，键=表头原文，值全是字符串；**/orders/pending 返回当天全部委托** |
| POST /orders | 必填 code、status（"1" 买 / "2" 卖）；amount；price（≤2 位小数）；price_type（limit 默认）；请求头 Idempotency-Key（≤128） | action, mode, code, amount, price, confirmed，可能有 entrust_no |
| POST /orders/cancel-all | type：A（默认=全撤）/ X / C / **L（撤最后）** | cancel_type, success, cancelled_count, confirm_dialog_shown；无可撤时 success=false + reason |

表头（作者样本）：
- 持仓：序号 证券代码 证券名称 **股票余额 可用余额** 冻结数量 成本价 市价 市值 盈亏 盈亏比例(%) 当日盈亏 当日盈亏比(%) 当日买入 当日卖出 仓位占比(%) 交易市场 操作
- 委托：**委托时间 证券代码 证券名称 操作 委托价格 委托数量 成交数量 成交均价 撤消数量 合同编号 备注** 交易市场
- **委托表没有「委托状态」列，状态在「备注」（如 未成交、全部撤单）。**

错误码（24）：AUTH_REQUIRED, AUTH_FAILED, VALIDATION_ERROR, DUPLICATE_ORDER, WINDOW_NOT_FOUND, CONTROL_NOT_FOUND, MODE_SWITCH_FAILED, ORDER_SUBMIT_FAILED, SERVER_CLEARING, OUTSIDE_TRADING_HOURS, T1_RESTRICTION, INSUFFICIENT_SHARES, INSUFFICIENT_BALANCE, SHORT_SELLING_FORBIDDEN, PRICE_OUT_OF_RANGE, ORDER_PRICE_REQUIRED, SERVER_UNAVAILABLE, OCR_FAILED, INPUT_VERIFY_FAILED, INTERNAL_ERROR, QUEUE_TIMEOUT, QUEUE_FULL, TASK_TIMEOUT, TASK_TIMEOUT_RECOVERY_FAILED。

配置 `config/app_config.json`（UTF-8 JSON，和默认值合并；示例文件里没有 host/port/auth，要自己加）：`host`=127.0.0.1，`port`=5000，`auth.enabled`/`auth.token`，`order.capture_entrust_no`=false，`order.verify_entrust_no`=false，`query.copy_method`=keyboard|message，`window_monitor.enabled`=true，`alerts.webhook_url`=""，`trading_app_paths` 必须与 xiadan.exe 实际路径完全一致。

安装：Python ≥ 3.11；`uv sync` → `uv add numpy` → 复制并改 `config/app_config.json` → `uv run python main.py` → 访问 /health 确认 `logged_in=true`。

客户端设置：设置 → 快速交易 四项全「否」（撤单前确认、买入确认、卖出确认、委托成功后弹提示）；窗口标题必须是「网上股票交易系统5.0」；不能最小化、不能拖出屏幕、不能锁屏。

耗时：买入 6–7 秒，撤单 2–5 秒，持仓约 6.4 秒，成交约 5.8 秒，资金约 2.2 秒，重启后首查约 9 秒；服务端最长 40 秒，客户端超时建议 60 秒；**单 worker，同一时间只发一个请求**；60 秒内参数相同下单报 DUPLICATE_ORDER，除非每次带不同 Idempotency-Key。

撤单：只有 A/X/C/L；L = 点客户端自带「撤最后」，撤哪笔由同花顺决定，大概率是「最近一笔还能撤的委托」；最新一笔已成交时撤哪笔未验证 → 上线前两笔小单实测，撤完用 /orders/pending 核对。
