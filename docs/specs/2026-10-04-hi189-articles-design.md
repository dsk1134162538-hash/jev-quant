# hi189「股票期货」文章接入 Jev 设计

日期：2026-10-04　状态：设计已和用户在对话里谈定，待审阅 spec

## 目标与用户选择

- 把 https://hi189.com/category/gupiaoqihuo/ 栏目里的收盘晚报类文章，当作**市场解读**交给 Jev，辅助两处判断：**市场情绪**、**扫首板的热点题材**。
- 直接生效，不走「只记录」的试验。
- 扫首板**只调候选顺序**，不降低热门题材的门槛（`HOT_PCT` / `HOT_MIN` 不变）。
- 不改变：公告排雷、消息面、卖出规则、影子账户的「不经 Jev」定位。

## ⚠️ 风险（必须先看）

1. **会影响实盘买点。** 超跌反弹「只看情绪」，而情绪标签现在会受文章影响；`rebound_main` 从 10-08 起演练、最早 10-13 实盘跟单，所以文章让情绪从「正常」变「偏弱」（或反过来）时，会直接改变当天买不买、早买还是尾盘买。扫首板的龙头顺序变化只影响 `WATCH_MAX=5` 内的排序，不改买点条件。
2. **文章是外部、不可验证的内容。** 作者观点可能错、可能夸大；正文也可能夹带「忽略以上指令」之类的文字。缓解：只传给 Jev 做「情绪」打分（输出被限定在四档标签），不让文章内容直接触发买卖；扫首板的「点名」由**代码字符串比对**判定，不经 Jev。
3. **没有历史检验。** 这条改动没有回测，上线后靠 trace 里留的文章标题/链接和 `article_hot` 字段事后复盘（见「观察」）。
4. **来源不可靠时必须无感退化**：抓不到、解析不出、没有符合条件的文章 → 行为与现在完全一致。
5. **休市期间不会自动上线**（见 HANDOFF「运行现状」）：合进 master 后需要用户在家里非交易时段用 cmd 或 Git Bash 手动跑 `scripts\home_install.py`，10-08 前完成。

## 数据：`jevq/data/hi189.py`（新增）

- 读 RSS：`https://hi189.com/category/gupiaoqihuo/feed`，取每条 `title` / `link` / `pubDate` / 正文（`content:encoded`，没有就按 `link` 取页面正文）。
- **取哪些**：`pubDate` 晚于「上一个交易日 15:00」的文章（即收盘后发布的晚报，到当前为止）。
- **跳过**标题以「密码保护：」开头的文章——不尝试绕过密码，也不记录其内容。
- 正文清理成纯文本（去 HTML 标签、压缩空白）。
- 网络超时 8 秒；任何异常返回空列表并打日志，不抛出。
- 缓存：`day_cache(date, kind='hi189', key=<文章 link>, value=JSON{title, link, published, text})`。同一天同一篇不重复抓。
- **抓取时机**：每个交易日 08:50 抓一次；之后每次情绪刷新（`SENTIMENT_REFRESH_MIN`=10 分钟）顺带再查一次新文章，**到 11:30 为止**（晚报通常盘前出，之后不再查，省请求）。查询失败不影响情绪刷新本身。
- 配置（`jevq/config.py`）：`HI189_ENABLED = True`（总开关，关掉 = 完全等同现状）、`HI189_FEED_URL`、`HI189_ARTICLE_MAX_CHARS = 3000`（每篇送 Jev 前的截断字数）、`HI189_FETCH_UNTIL = time(11, 30)`、`HI189_TIMEOUT = 8.0`。

## 判断一：市场情绪（`jevq/judge/market.py::sentiment`）

- 有文章时，在 `stats` 的基础上 state 里加字段 `「市场解读」`：文章列表，每项 `{标题, 发布时间, 正文}`，正文截到 `HI189_ARTICLE_MAX_CHARS`。
- `instructions` 补一句「结合市场解读」，等级仍是 `冰点/偏弱/正常/火热`，其余不变。
- 没有文章 → state 与 instructions 都和现在一模一样。
- `Engine` 把文章列表通过 `market_judge` 的一个属性/参数传入（具体接法写进实现计划）；情绪 trace 的 `inputs` 里会带上「市场解读」，所以 `decisions.trace` 天然留痕；另外在 trace 加 `articles: [{title, link}]` 只存标题和链接，方便面板/复盘显示而不展开全文。

## 判断二：扫首板热门题材排序（`jevq/rules/board.py::watch_candidates`）

现在：`sorted(hot.items(), key=-len(members))` 遍历题材，最后 `sorted(out.values(), key=(-heat, score))[:WATCH_MAX]`。

改成：热门题材排序键 = （**文章点名的优先**，再按强势成员数降序）；最终候选排序同理 `(not article_hot, -heat, score)`。`watch_candidates` 新增可选参数 `articles: list[ArticleHot] | None`，不传 = 行为不变。

**「点名」的判定（纯代码，不用 Jev）**，满足任一即点名：
1. 题材名与文章里的某个板块名**互相包含**（任一方是另一方的子串，归一化后比较：去空白、去「概念」「板块」后缀）。
2. 该题材**盘中强势成员**（`hot_concepts` 返回的成员）里，有代码/名称出现在文章的个股列表（`涨停板：…`、`其他强势股：…`）中。

**文章解析**：晚报格式是「板块名 / 涨停板：… / 其他强势股：…」，例如 https://hi189.com/6999.html。解析成 `[{sector, limit_up: [股票名…], strong: [股票名…]}]`。解析不出结构 → 视为没点名任何题材（退化为现状排序），不报错。股票名与行情里的名称比对（`universe` 里有名称）。

不改动：`HOT_PCT=5.0`、`HOT_MIN=3`、`ENTER_PCT`、`WATCH_MAX`、龙头选法 `leader()`、`TRIGGER_PCT`、扫板窗口。

## 留痕与显示

- 情绪 trace：见上，`articles: [{title, link}]`。
- 扫首板候选的 trace（`decisions.trace` 里候选部分）加字段 `article_hot: bool` 和命中原因 `article_hot_why`（命中的板块名或个股）。
- 复盘报告（`jevq/report.py`）加一行「今日参考文章：标题（链接）；…」，没取到写「未取到」。
- 面板：不新增页面；情绪 trace 展开时能看到文章标题/链接即可（沿用现有 trace 展示，不专门做界面）。

## 四处同步清单（HANDOFF 要求）

1. `jevq/config.py` 常量（上面列的）。
2. `jevq/app/api.py::builtin_summaries`：扫首板、超跌反弹的说明各加一行（文章参与情绪判断 / 扫首板顺序），并在 `tests/test_api.py` 加断言。
3. `docs/HANDOFF.md` 策略表（扫首板、超跌反弹两行）+ 新增一节说明数据来源、开关、风险；「Jev 在哪里用」补一条。
4. 相关设计文档变更记录：`docs/specs/2026-09-23-multi-strategy-first-board-design.md`、`docs/specs/2026-09-24-rebound-design.md` 各加一条指向本文。

## 测试（全量 pytest 必须全绿；测试不联网）

- `tests/test_hi189.py`：RSS 解析（pubDate 过滤、跳过「密码保护：」、正文清理、截断）、缓存命中不重复抓、网络异常返回空、`HI189_ENABLED=False` 不抓取。
- 晚报解析与点名：用**真实晚报文本做夹具**（`tests/fixtures/hi189_evening.txt`）。
  - **夹具来源待解决**：云端会话的网络出口屏蔽了 hi189.com（`EGRESS_BLOCKED`），我在这里抓不到 6999.html。需要用户把该篇正文粘贴给我，或在家里电脑抓一份放进仓库；在拿到真实文本前，解析器只能按已知格式「板块名 / 涨停板：… / 其他强势股：…」写，真实页面的细节（全角冒号、顿号/空格分隔、板块名前缀编号等）要靠夹具校准。
- `watch_candidates`：不传 `articles` 与现状逐项相同（回归）；点名的题材排前面；两种点名规则各一例；无强势成员不因点名而入选（门槛不降）。
- `market.sentiment`：有文章时 state 含「市场解读」且被截断；无文章时与现状一致；Jev 出错仍返回 None。
- `report`：有/无文章两种输出。
- `tests/test_api.py`：`builtin_summaries` 新增行断言。

## 观察与回退

- 观察：上线后 trace 里同时有情绪标签与文章标题，可事后对比「有文章 vs 无文章」的情绪差异；扫首板用 `article_hot` 分组比盈亏（样本量小，仅作参考）。
- 回退：`HI189_ENABLED = False` 并重启后台即可完全恢复现状；或删掉 `day_cache` 里 `kind='hi189'` 的行。

## 不做

- 不绕过「密码保护」文章；不抓登录态内容。
- 不把文章直接变成买卖信号；不让 Jev 决定「点名」。
- 不降低扫首板热门题材门槛；不改卖出规则。
- 不新增面板页面。
