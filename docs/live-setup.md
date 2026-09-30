# 实盘环境安装指南（家里电脑）

给要在家里跑下单客户端的那台电脑用。按 0-8 顺序做；装完第 5、6 步后跑第 7 步的实测，把输出发给 Claude。

## 0. 拿到代码

在家里的开发目录（`E:\claude\jev-quant`）：

```
git fetch origin
git checkout master
git pull
.venv\Scripts\pip install -r requirements.txt
```

（实盘代码合进 master 之前，把上面的 `master` 换成分支 `live-trading-6luqxt`。）这一步只是拿到实测脚本。

## 0.5 搬家：一键装好家里的后台

公司电脑的后台 2026-09-30 已停，开机启动项已改名禁用（`Jev模拟盘后台.lnk.disabled`）。数据库快照 `jevq.sqlite.gz` 要带回家，两种方式二选一：

- U 盘或网盘拷回来，然后：
  ```
  .venv\Scripts\python scripts\home_install.py --db D:\路径\jevq.sqlite.gz
  ```
- 或者在公司电脑上自己把它推到私有仓库的临时分支 `migration-data`，家里直接运行（不带 `--db` 就从这个分支取）：
  ```
  .venv\Scripts\python scripts\home_install.py
  ```

脚本会建运行目录 `E:\jev-live`（只跑不改，开发仍在 `E:\claude\jev-quant`）、装依赖、放数据库（已存在绝不覆盖）、检查凭证、建开机启动项并启动后台。可以重复运行：以后代码更新了，再跑一次就是更新（交易时段会拒绝，除非加 `--force`）。装好后 `migration-data` 分支可以删掉。

## 1. Tailscale（让手机连得上家里电脑）

电脑和手机都装 Tailscale（https://tailscale.com/download），用同一个账号登录。

装好后在电脑上确认分配到的地址（形如 `100.x.x.x`）：

```
tailscale ip -4
```

也可以直接点开任务栏的 Tailscale 图标，「这台设备」一栏会显示同样的地址。记下这个地址，在电脑上执行一次（重启后依然生效）：

```
setx JEVQ_LIVE_HOST 100.x.x.x
```

把 `100.x.x.x` 换成实际查到的地址——这个环境变量是给后面的实盘程序用的，决定它的状态页监听在哪个地址上，手机才能通过 Tailscale 访问到。

## 2. 远程桌面：装 ToDesk 或向日葵

任选一个装上，**不要用 Windows 自带的远程桌面（RDP）**——RDP 连接期间本机屏幕会被锁定/挂起，同花顺客户端在这种状态下可能收不到模拟的鼠标键盘操作，会导致下单和探测脚本失败。

在 ToDesk/向日葵的设置里找到「断开连接后锁定电脑」一类的选项，**关掉**，保证远程断开之后电脑仍然是解锁、可操作的状态。

## 3. Windows 电源与锁屏

- 设置 → 系统 → 电源和睡眠：屏幕、睡眠都改成「从不」。
- 设置 → 账户 → 登录选项：「离开后需要重新登录」改成「从不」，运行中不能自动锁屏，否则模拟操作打不进去。
- 关闭屏幕保护程序的密码保护。
- 电脑重启后要有人登录桌面，程序才会自己起来。要不要设成开机自动登录由你决定：方便，但别人碰到这台电脑就能直接进桌面。不设的话，每次重启后远程进去登录一次即可。
- 找到「更新和安全」里 Windows 更新的活跃时间设置，改成 08:00–16:00，避免系统在交易时段自动重启。

## 4. 同花顺独立下单客户端

打开同花顺**独立下单**客户端（不是普通行情软件），登录好账号，然后设置：

- 快速交易四项——撤单前确认、买入确认、卖出确认、委托成功后弹提示——**全部选「否」**。
- 界面不操作超时时间改成 `0`。
- 默认买入/卖出价格、默认数量都清空，不要留任何默认值。
- 窗口不最小化、不拖出屏幕可见范围。
- 确认窗口标题就是「网上股票交易系统5.0」——xiadan-gateway 和 easytrader 都是靠这个标题去找窗口的，标题不对会连不上。

## 5. 安装 xiadan-gateway（首选下单层）

先装 uv（参考 https://docs.astral.sh/uv/getting-started/installation/ ），然后：

```
git clone https://github.com/hylyq/xiadan-gateway.git
cd xiadan-gateway
git checkout 1662adf5b7d0afc73d1ee2f0f15101104d2c07af
uv sync
uv add numpy
```

`uv add numpy` 这一步必须做——仓库代码顶层直接 `import numpy`，但依赖列表里没有它，只 `uv sync` 会在启动时报 ImportError。

在仓库目录下编辑 `config/app_config.json`（仓库自带的示例文件里没有 host/port/auth 这些字段，要自己补全，完整内容如下）：

```json
{
  "host": "127.0.0.1",
  "port": 5000,
  "auth": {
    "enabled": true,
    "token": "<你生成的随机串>"
  },
  "window_monitor": {
    "enabled": true
  },
  "alerts": {
    "webhook_url": ""
  },
  "trading_app_paths": [
    "C:\\同花顺软件\\同花顺\\xiadan.exe"
  ]
}
```

`trading_app_paths` 改成同花顺独立下单客户端 `xiadan.exe` 的实际安装路径，必须跟实际路径完全一致——记不清的话，在开始菜单找到那个程序图标，右键「打开文件所在位置」确认。

生成一个随机 token：

```
python -c "import secrets; print(secrets.token_hex(24))"
```

把打印出来的字符串填进上面 `auth.token` 占位符的位置，同时设成环境变量（必须跟 `app_config.json` 里是同一个值）：

```
setx XIADAN_GATEWAY_TOKEN <刚才生成的字符串>
```

启动网关：

```
uv run python main.py
```

浏览器打开 http://127.0.0.1:5000/health ，确认返回里 `logged_in: true`（说明网关已经认到同花顺客户端处于登录状态）。

**警告：这个仓库 `scripts/` 目录下的脚本会真实下单（其中一个脚本一次性下 22 笔单）。装完之后，`scripts/` 目录下的任何脚本都绝对不要运行。**

## 6. 安装 easytrader（备选下单层，两个都装，实测完再二选一）

在项目根目录（`.venv` 所在的目录）下执行：

```
.venv\Scripts\pip install "git+https://github.com/shidenggui/easytrader@def5a8edc5ebd7d49bf92a9f39e9e30c9bf47101" pywin32
```

不要用 easytrader 仓库自带的 `requirements.txt`/`Pipfile`——那两个文件指向 http 镜像源，还关掉了证书校验。PyPI 上发布的 0.23.7 版本比这个 commit 旧，缺两个相关修复，所以必须按上面这条命令从指定 commit 直接装。

## 7. 实测

两种 broker 各跑一次只读探测（不会下单，可以随时跑）：

```
.venv\Scripts\python scripts\live_probe.py read --broker gateway --n 20
.venv\Scripts\python scripts\live_probe.py read --broker easytrader --n 20 --exe "C:\同花顺软件\同花顺\xiadan.exe"
```

`--exe` 换成实际的 `xiadan.exe` 路径。把两次的完整输出发给 Claude——重点是成功率、耗时，以及最后打印出来的持仓/委托解析结果对不对。

`read` 最后会多打印一段「当日委托原始行」（未经程序解析，含撤单记录行）。交易时段里手里有委托的话，看这两点，一并发给 Claude：

- **委托时间的原文格式**：是 `093105`、`09:31:05`、`9:31:05`，还是被读成整数 `93105`（少了前导 0）。程序都能补成 `09:31:05`，但要知道它是哪一种。
- **撤过单以后，有没有多出「撤买」「撤卖」这样的行**（操作/买卖标志列里带「撤」字）。程序会把这种行当成撤单记录忽略掉，不会当成委托；但要知道你的客户端有没有这种行。

然后在交易时段（09:30–11:30 或 13:00–14:57，离时段结束至少留 2 分钟，全程约 1 分钟）**本人**跑一次真实下单测试，代码换成一只当前没有持仓、随便一只流动性还可以的股票：

```
.venv\Scripts\python scripts\live_probe.py order-test --broker gateway --code 601398 --i-understand
```

跑之前确认 `live.py` 是「关闭」模式或已经停掉，也没有别的程序、自己手动在下单——测试要「撤最后一笔」，会撤到别人的单。

order-test 下**两笔**买单，各 100 股，都远低于市价、不会成交：

1. A = 跌停价，等 3 秒；
2. B = 跌停价 +0.01，等 3 秒；
3. 读当日委托，A、B 都是挂单状态才继续；
4. 调一次 `cancel_last()`（撤最后一笔），等 3 秒再读：
   - **PASS「撤最后一笔只撤了 B」**：B 已撤、A 还挂着——网关（或 easytrader）确实只撤最后一笔；
   - **FAIL「cancel_last 撤了不止一笔（可能按全部撤单执行），绝不能上实盘」**：A 也被撤了，说明「撤最后一笔」被按「全部撤单」执行（网关没读到请求里的 `type`）。一上实盘就会把你自己手动挂的单一起撤掉。**看到这个 FAIL，不许开实盘**，把输出发给 Claude；
   - 其它 FAIL（B 仍挂着、撤成了 A、撤单后在当日委托里找不到这笔）也都不能上实盘。
5. 再调一次 `cancel_last()` 撤掉 A，确认 A 也不是挂单状态。

脚本会算出跌停价，打印将要做的操作，等你在终端里手动输入 `yes` 才会真的下单（输入 yes 之后会再检查一次时段，拖过了就退出、一笔都不下）。任何一步下单抛异常，或中途 FAIL 退出，脚本都会提醒「单子可能已经报出去了，去 App 里核对并手动撤单」——这时一定要去客户端确认没有残留的挂单。全程盯着屏幕看每一步的 PASS/FAIL，跑完把输出也发给 Claude。两种 broker 都跑过 `order-test` 之后，读得准、跑得稳的那个会定为正式使用的下单层。

order-test 还会打印每一笔的信息，把它们一起看一下：

- **委托号 vs 合同编号**：`place` 返回的委托号，和当日委托里对应那一行的「合同编号」。网关默认拿不到委托号（`order.capture_entrust_no` 默认 `false`，打印会是「(空)」），这时程序只能靠代码、方向、价格、数量、时间来匹配委托。在网关的 `config/app_config.json` 里加上下面这段，重启网关，再跑一次 order-test，看两个号是不是**逐字相同**（逐字相同就保持打开；不同就改回关闭，把输出发给 Claude）：
  ```json
  "order": {
    "capture_entrust_no": true
  }
  ```
- **委托时间原文**：以及它解析成什么。
- **电脑时钟和券商时钟的偏差**：脚本会打印下单调用前后的电脑时间，和委托时间对比，超过 5 秒会警告。程序按「委托时间 ≥ 下单时刻 − 5 秒」匹配委托，电脑时钟比券商快十几秒，每笔单都会匹配失败。**上线前在管理员命令行里执行一次 `w32tm /resync` 校时。**
- **撤单后有没有多出「撤买/撤卖」行**：撤单之后脚本会说当日委托里有没有单独的撤单记录行。

最后再测一件事：交易时段手动买 100 股任意一只股票，马上跑一次 `read`，看这只股票的「股数」列**有没有包含刚买的这 100 股**，把结果告诉 Claude。有的客户端「股票余额」不含当天买入的股数。程序已经按「当天买的持仓不按客户端减账」做了防护，但要知道你这台客户端是哪种。

## 8. 使用中的安全规则

- **网关或 easytrader 在操作同花顺客户端期间，不要碰这台电脑的鼠标和键盘。** xiadan-gateway 每次调用都会在交易窗口中央真实点击一下、连按 ESC；easytrader 也会抢前台焦点发按键。如果你人要用这台电脑（远程或本地都算），先在手机的页面上把交易暂停，确认程序已经停止操作后再动鼠标键盘。
- 不要运行 xiadan-gateway 仓库 `scripts/` 目录下的任何脚本（见第 5 步警告）。
- `XIADAN_GATEWAY_TOKEN`、`app_config.json` 里的 `auth.token`，都不要发给任何人、不要贴到聊天记录或截图里——探测脚本自己也绝不会把它打印出来。
- 同花顺客户端窗口全程不要最小化、不要改标题、不要切换到精简模式。
