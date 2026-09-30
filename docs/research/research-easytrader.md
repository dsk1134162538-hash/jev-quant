# easytrader 调研结论（子 agent 读源码所得，controller 转存）

- 锁定：`shidenggui/easytrader` master `def5a8edc5ebd7d49bf92a9f39e9e30c9bf47101`（2026-02-28），版本号 0.23.7。
- **PyPI 上的 0.23.7（2025-04-16）比 master 旧**：没有 #515（去掉市场下拉框）和 #518（验证码修复）。必须按 SHA 从 git 安装：
  `pip install "git+https://github.com/shidenggui/easytrader@def5a8edc5ebd7d49bf92a9f39e9e30c9bf47101"`，另装 `pywin32`（pywinauto 隐性依赖）。不要用仓库里的 requirements.txt/Pipfile（http 镜像、verify_ssl=false）。

## 连接
- `user = easytrader.use('universal_client')` → UniversalClientTrader，默认 **Xls** 读表（Ctrl+S 另存为临时 .xls，按 GBK TSV 读回，**不触发验证码**）。
- `user.connect(r'C:\...\xiadan.exe')`：要求 xiadan.exe 已运行且已登录；按进程路径 attach（超时 10s），然后关闭所有标题不是「网上股票交易系统5.0」的 #32770 窗口。必须传路径。
- 不用 `prepare`（会尝试登录，且 login 没有超时）。

## 客户端设置（docs/install.md）
界面不操作超时时间设为 0；默认买入/卖出价格和数量都设为空；不能最小化、不能精简模式；远程用 VNC 类工具，不要用 RDP。

## 返回格式
```python
user.balance   # dict[str, float]
{'资金余额': 5000.0, '可用金额': 4000.0, '可取金额': 4000.0, '总资产': 9000.0}
user.position  # list[dict]，key 是客户端表头原文（各券商/版本不同！例：'证券代码', '当前持仓', '参考成本价', '盈亏比例(%)'；也可能是 '股票余额', '可用余额', '可卖数量'）
user.today_entrusts / user.today_trades  # 同样 list[dict]，表头原文
user.buy(security, price, amount)   # sell 同
# -> {'entrust_no': '1234567'}   提示框含「成功」
# -> {'message': 'success'}      没弹框（拿不到委托号，不代表一定报出去了）
# raise easytrader.exceptions.TradeError(content)   提示框不含「成功」
user.cancel_entrust('1234567')      # 按「合同编号」字符串匹配，按像素坐标双击该行；返回 {'message': ...}
user.cancel_all_entrusts()          # 返回 None
```
- 证券代码、合同编号等列强制 str；其他列 pandas 推断 int/float，但一列里有空格就整列成 str —— 解析时一律自己转 float/int。
- 解析失败可能返回 None；pywinauto 异常直接抛出。

## 风险（写适配器时要防）
1. 必须有未锁屏的交互桌面；会抢前台焦点发按键。
2. 可能永久挂起（未知「提示信息」框时 handler 死循环）——需要外部看门狗（我们的 daemon watchdog 负责）。
3. 返回 success 不代表已报单（标题不完全等于「委托确认」的确认框会被关掉=取消，照样返回 success）——每笔都要用当日委托对账（执行器已这么设计）。
4. 「超出涨跌停」提示会被自动确认。
5. 撤单按像素行定位，DPI 缩放或表格滚动时可能点错行 → cancel_last 用 cancel_entrust(委托号) 实现前必须先确认委托号。
6. Xls 临时文件（含持仓）不删除。
7. 后端 win32；32 位 Python 只是推荐，64 位会有 UserWarning；Python 3.12 上游没测过。
8. universal_client 路径没有外联网络请求。
