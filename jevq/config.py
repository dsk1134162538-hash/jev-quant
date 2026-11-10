from datetime import time

# 资金与仓位
INITIAL_CASH = 30_000.0
MAX_PER_TRADE = float("inf")   # 全仓：单笔预算 = 全部可用现金（min(cash, inf) == cash）
MAX_POSITIONS = 1              # 全仓单票
LOT = 100

# 费用
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_RATE = 0.0005          # 仅卖出
TRANSFER_RATE = 0.00001      # 双向

# 买入
BUY_ZONE_LOW = -3.0
BUY_ZONE_HIGH = 0.0
MAX_CHECKS_PER_TICK = 10
MAX_ERRORS_PER_TICK = 3

# 超跌反弹（当日区间用上面的 BUY_ZONE_*，尾盘时刻、Jev 检查额度与其他策略共用）
# 低吸（9-23 版规则，10-09 重新上线）：当日 0~-3% 区间，近 5 日涨幅高的优先（强势股回调）
LOWBUY_STOP = -3.0             # 卖出：当前涨幅或开盘涨幅 ≤ 这个值止损（%，相对昨收）
LOWBUY_TAKE_PROFIT = 1.0       # 卖出：开盘 ≥ 这个值高开止盈；平开时盘中摸到这个值卖
LOWBUY_LOW_OPEN = -1.0         # 卖出：开盘 ≤ 这个值算低开（之后回到 0 轴或从低点反弹就卖）
LOWBUY_ZERO_LINE = -0.2        # 卖出：低开后回到这个涨幅以上算回到 0 轴
LOWBUY_BOUNCE = 1.5            # 卖出：低开后从当天最低反弹这么多个点卖
# 低吸 2026-10-09 细化（用户同意）：选股收窄 + 跟踪止盈替代「高开止盈 / 摸到 +1%」
LOWBUY_RET5_LOW = 5.0          # 买入：近 5 日涨幅下限（%）——强势股
LOWBUY_RET5_HIGH = 20.0        # 买入：近 5 日涨幅上限（%）——太高的多是高位放量滞涨
LOWBUY_MIN_SHRINK_DAYS = 1     # 买入：今天量（盘中折算）比昨天小 = 缩量回调；量能判断不了（09:45 前等）不买
LOWBUY_TRAIL_START = 1.0       # 卖出：当天最高相对买入价到 +1% 后启动跟踪止盈
LOWBUY_MAIN_TRAIL_PULLBACK = 1.5   # 主账户回落线（用户 2026-10-14：众生药业开盘 +1.4% 回落 0.8 被洗掉，之后 +8.9%）；影子仍用下面的 0.8
# 卖出（主账户，10-15 dj「中高风险」+ 回测：77 天抽 600 只 2126 笔日线近似，影子照旧）：拿 2 个交易日、低开反抽/回 0 轴不卖。
# 每笔均：现在 +0.22% → +0.39%，普跌/中性/强 各环境都更好；再把止损放到 -4% 还能到 +0.48%，但止损按每天相对昨收算、两天可能叠到 -8%，没放
LOWBUY_MAIN_MAX_HOLD_DAYS = 2
LOWBUY_MAIN_LOW_OPEN_SELL = False
LOWBUY_TRAIL_PULLBACK = 0.8    # 卖出：从当天最高回落这么多个点（相对买入价）卖；跌回成本价也卖
# 情绪轮动：各情绪下用哪个策略（按顺序 = 优先级）；拐点确认后另外允许超跌反弹（拐点抄底）
REGIME_SOURCES = {"火热": ("board",), "正常": ("board", "lowbuy"), "偏弱": ("lowbuy",), "冰点": ()}
REGIME_BY_MARKET = True        # 用户 2026-10-14：按大盘环境跟（全市场 5 日涨幅中位数），盘中情绪只把关（冰点不买、拐点超跌优先）
REGIME_WEAK_MARKET_RET5 = -2.0  # 大盘 5 日中位数 ≤ 这个 = 普跌：只跟超跌反弹（回测两日均 +1.3%~+1.7%，扫首板次日平均低开 1.5%）；
                                # 否则跟扫首板、10:30 后没买到再跟低吸（大盘偏强时主线首板次日均 +2.1%）
REGIME_BOARD_FIRST_UNTIL = time(10, 30)   # 正常：这之前只等扫首板（扫板窗口），之后没买到再做低吸
REBOUND_RET5_LOW = -20.0       # 近 5 日涨幅下限（%）
REBOUND_RET5_HIGH = 0.0        # 近 5 日涨幅上限（%）
REBOUND_BOARD_LOOKBACK = 40    # 近 N 个交易日出现过连板的排除
REBOUND_MIN_BOARDS = 2         # 几连板算「连板」
REBOUND_MIN_SHRINK_DAYS = 3    # 买入：成交量至少连续缩量几天（含今天，不看涨跌，同「阶梯缩量」N）；0 = 不看成交量
REBOUND_PRIORITY_SHRINK_DAYS = 4   # 买入：连续缩量达到这个天数的候选排在前面
REBOUND_REBUY_DROP = 1.0       # 买入：当天已卖出的票，现价要比卖出价低这么多（%）才允许再买
REBOUND_STOP = -2.0           # 卖出：相对买入价止损（%）
REBOUND_TRAIL_START = 1.5      # 卖出：当天最高相对买入价达到这个盈利（%）后启动跟踪止盈
REBOUND_TRAIL_PULLBACK = 0.8   # 卖出：从当天最高回落这么多个点（相对买入价）卖
REBOUND_MAX_HOLD_DAYS = 2      # 卖出：最多拿几个交易日，最后一天 14:55 清仓
REBOUND_LOW_OPEN = -1.5        # 卖出：开盘相对昨收 ≤ 这个值算低开（%）
REBOUND_LOW_OPEN_BOUNCE = 0.8  # 卖出：低开后从当天最低拉起这么多个点（相对昨收）必须卖
# 情绪拐点抄底（用户 2026-10-09：恐慌后一有反弹拉升迹象就快速抄底，直接实盘）。规则见 jevq.rules.turn
TURN_START = time(9, 45)        # 开盘前 15 分钟广度不稳，不判
TURN_END = time(14, 50)
TURN_PANIC_UP = 0.30            # 今天红盘占比最低到过这个以下 = 恐慌过（股票池约 3000 只 ≈ 红盘不到 1000 家）
TURN_PANIC_FROM = time(9, 35)   # 恐慌低点从这时起算：开盘头几分钟的广度是集合竞价余波（10-14 09:30 红盘 21% 下一分钟就 39%）
TURN_LD_PEAK_MIN = 10           # 当天跌停峰值至少这么多家才算真恐慌（10-09 18 家、10-13 14 家是真拐点；10-14 5 家是假的）
TURN_LOW_AGE = 15               # 最低点离现在至少几个交易分钟（恐慌释放后没再创新低）
TURN_UP_MIN = 0.35              # 现在红盘占比至少
TURN_UP_GAIN = 0.15             # 现在红盘占比比最低点至少多几个百分点（0.15 = 15 个点）
TURN_LD_FLOOR = 5               # 跌停家数降到这个以内，或降到今天最多的 TURN_LD_SHRINK 倍以内
TURN_LD_SHRINK = 0.6
TURN_VOL_LOOKBACK = 30          # 成交额较昨日同期% 要比这么多交易分钟前高（放量）
TURN_CLAUDE_REQUIRED = True     # 用户 2026-10-14：规则满足后必须 Claude 同意才确认（jevq/judge/turn_claude.py）；Jev 照问只记录
TURN_CLAUDE_MIN = 0.55          # Claude 判修复拐点的概率至少这么多（10-14 回放：真拐点 0.72、假的 0.35）
TURN_JEV_REQUIRED = False      # True = 规则满足后还要 Jev 确认；False = 规则满足就确认，Jev 照问但只记录
                                # （10-09 实测同一份 13:10 数据 Jev 给 0.50~0.58，像掷硬币，当门槛只会拖慢）
TURN_JEV_MIN = 0.55             # Jev 判「修复拐点」的概率至少这么多才确认（10-09 13:10 实测约 0.6）
TURN_JEV_RETRY = 5              # 分钟：Jev 没确认，规则仍满足时多久后再问（拐点要快）
REBOUND_STOP_CONFIRM = 2      # 卖出（主账户）：现价连续几分钟在止损线下才算止损，防瞬间下探（用户 2026-10-09 定）
# 买入（主账户，用户 2026-10-14 按回测定）：大盘普跌时才抄超跌——全市场 5 日涨幅中位数 ≤ 这个值；确认拐点的日子不看这条。
# 72 天回测（收盘买、持有 2 天）：普跌时两日均 +1.3%~+1.7%、胜率 64%~68%；大盘中性时各档都是负的。影子账户不看，当对照
REBOUND_MAIN_MARKET_RET5_MAX = -2.0
# 买入（主账户）：个股 5 日涨幅减大盘中位数 ≥ 这个值（比大盘多跌超过 10 个点 = 个股自己出问题，大盘中性时两日均 -2.8%、三成继续跌 5% 以上）
REBOUND_MAIN_EXCESS_MIN = -10.0
# 买入（主账户，用户 2026-10-14）：当天跌幅放宽到这个（影子账户仍按 BUY_ZONE_LOW -3%）。普跌时回测：盘中跌 3~5% 买两日均 +2.2%、胜率 71%；
# 跌 5~7% 的一半止损，不放。开盘就买（09:45 前今天的量不能折算，缩量看截至昨天的，见 metrics.history_shrink_days）：+2.2%、胜率 70%
REBOUND_MAIN_ZONE_LOW = -5.0
REBOUND_MAIN_TRAIL_PULLBACK = 1.5   # 卖出（主账户）：跟踪止盈从最高回落这么多个点才卖（用户 2026-10-14，原 0.8；影子账户仍 0.8）
REBOUND_HARD_STOP = -3.5       # 卖出（主账户）：硬止损（%），到了直接卖，不等确认、不问 Jev
# 卖出（主账户，2026-10-15 按 dj「中高风险」风格 + 回测改，影子账户仍 -2% / 2 天 / 卖低开反抽当对照）：
# 普跌日 821 笔日线近似（每天等权）：现在 -2%/2 天 日均 +1.67% → -3%/3 天 +2.07%；全部环境 +0.91% → +1.22%，非普跌也更好；
# 去掉低开反抽再 +0.15 个点。硬止损 -4.5% 回测没更好，留 -3.5%。见 docs/HANDOFF.md 第 29 项
REBOUND_MAIN_STOP = -3.0
REBOUND_MAIN_MAX_HOLD_DAYS = 3
REBOUND_MAIN_LOW_OPEN_SELL = False
# 买入（主账户，用户 2026-10-15「超跌也要看板块」）：白酒等酒类不买（自建板块 id + 概念/行业名关键词）。
# 板块跌幅的回测（77 天、700 只、2016 笔）：全部环境里「板块 5 日跌 3% 以上」比「板块在涨」好得多（+1.7%~+2.3% 对 +0.3%），
# 但主账户只在普跌日买，普跌日板块跌多跌少差别不大；「个股自己跌（个股跌超 5%、板块跌不到 3%）」第一次测出来很差，
# 把「业绩预降」「基金重仓」这类非题材标签剔掉后普跌日就没差别了——对口径太敏感，先不启用（REBOUND_LONE_FILTER）。
# 「板块在涨就不买」普跌日几乎不起作用、全部环境里反而变差，不加
REBOUND_LONE_FILTER = False
# 买入（主账户 + 情绪轮动跟它）：大盘普跌时情绪「冰点」也买（10-14 dj 定的是「偏弱也买、冰点仍不买」，10-16 dj 同意改）。
# 依据：普跌日回测本来就不看盘中情绪；「昨天全市场中位数跌超 1%」（收盘多半判成冰点）的普跌日反而最好：
# 新卖法下 7 天 295 笔均 +4.1%、胜率 87%；09:45 前沿用昨天收盘的冰点会把这些日子的开盘买点整段挡掉
REBOUND_MAIN_BUY_ON_ICE = True     # dj 2026-10-16 早上同意打开
REBOUND_SECTOR_BLACKLIST_IDS = ("liquor",)
REBOUND_SECTOR_BLACKLIST_WORDS = ("酿酒", "白酒", "黄酒", "啤酒", "葡萄酒")
REBOUND_LONE_RET5 = -5.0
REBOUND_LONE_SECTOR_MAX = -3.0
# 买入（超跌、低吸主账户，用户 2026-10-19「低活跃股和八万以上股东的股不买」，见 jevq/data/holders.py）；扫首板不用（回测相反）
HOLDERS_MAX = 80_000              # 股东户数超过这个不买
LOW_ACTIVITY_DAYS = 20
LOW_ACTIVITY_ABSRET = 1.5         # 近 20 日平均每天涨跌幅绝对值（%）低于这个 = 股性不活跃，不买
HOLDERS_REFRESH_ENABLED = True    # 盘前刷新股东户数（7 天一次）
HOLDERS_FETCH_TIMEOUT = 90.0
# 每周核对（用户 2026-10-19）：主账户按回合对比回测期望（胜率%, 每笔平均%, 说明）；笔数够了还明显差 → 提醒复盘
REVIEW_SINCE = "2026-10-16"        # 这批规则（超跌 -3%/3 天、低吸 2 天等）上线那天
REVIEW_EXPECT = {"rebound_main": (76.0, 2.7, "普跌日、-3%/3 天"), "lowbuy_main": (50.0, 0.4, "拿 2 天"),
                 "board_main": (49.0, 0.9, "次日收盘")}
REVIEW_ALSO = ("regime_main",)
REVIEW_MIN_TRADES = 8
REVIEW_MEAN_GAP = 2.0              # 每笔平均比回测差这么多个点
REVIEW_WIN_GAP = 20.0              # 胜率比回测低这么多个百分点
PREMARKET_CHECK_ENABLED = True    # 盘前（等 09:15 之前）试一遍外部依赖，推送一条结果（用户 2026-10-19）
PREMARKET_CLAUDE_TIMEOUT = 40.0
# 买入（超跌、低吸、扫首板主账户，用户 2026-10-19）：持有期内有财报、大额解禁的不买，见 jevq/data/events.py
EVENTS_REFRESH_ENABLED = True     # 盘前每天刷新财报预约披露日、未来解禁
EVENTS_FETCH_TIMEOUT = 60.0
UNLOCK_LOOKAHEAD_DAYS = 15        # 自然日
EX_RIGHTS_BACKFILL_DAYS = 60      # 盘前按东财分红送配补最近这么多天的除权除息（10-19 前没存调整后的昨收）
UNLOCK_MIN_RATIO = 0.03           # 解禁占解禁前流通市值 ≥ 3% 才算大额（股权激励那种零点几的不拦）
INDUSTRY_REFRESH_ENABLED = True   # 盘前刷新新浪行业（7 天一次，超跌看板块用）
SECTOR_MIN_MEMBERS = 5          # 板块成员（有 5 日涨幅的）不足这么多不算板块涨跌
# 买入（主账户，用户 2026-10-15「结合新闻选板块、冷门板块不要买」）：排名靠前的候选连同所属板块、近期新闻交给 Claude，
# 它只能否决没有资金/消息关注的冷门板块，不能加票；没答上按规则照买。新闻没有历史，靠影子账户对照实盘检验
REBOUND_SECTOR_VETO = True
REBOUND_SECTOR_VETO_TOP = 5     # 一次最多给 Claude 几只候选
REBOUND_SECTOR_VETO_RETRY = 3   # 分钟：Claude 没答上，多久后再问（这期间按规则买）
REBOUND_SECTOR_VETO_TIMEOUT = 35.0   # 秒：10-15 晚实测 6 只候选 + 新闻一次 15~20 秒；受每分钟 TICK_JEV_BUDGET 限制
# 卖出（主账户）：止损确认后、低开反抽触发时问 Jev 今天走向；判成这些就先留着，下一分钟再看
TREND_LEVELS = ["冲高回落", "高开低走", "单边下跌", "震荡", "探底回升", "低开高走"]
REBOUND_TREND_HOLD = ("探底回升", "低开高走")
REBOUND_TREND_BUDGET = 8.0     # 秒：一次走向判断（拉分时 + 问 Jev，含重试）的总时限，到点没问完按原规则卖
REBOUND_FLOW_MINUTES = 3      # 卖出：跟踪止盈触发时看最近几分钟的主买/主卖增量，主买多先留着

# 时间
MARKET_OPEN = time(9, 30)
TAIL_BUY_START = time(14, 50)
BUY_END = time(14, 57)
EOD_SELL = time(14, 55)
SENTIMENT_REFRESH_MIN = 10
SENTIMENT_SETTLE = time(9, 45)  # 这之前「开盘未定」：沿用上一交易日收盘的情绪（Jev 照判，只记录）
SENTIMENT_HYSTERESIS = 0.15     # 情绪迟滞：Jev 四档概率的期望分（冰点 0~火热 3）越过档位分界 0.5/1.5/2.5 再多这么多才换档

# hi189「股票期货」文章（docs/specs/2026-10-04-hi189-articles-design.md）
HI189_ENABLED = True             # 总开关：False = 完全等同没接文章
HI189_FEED_URL = "https://hi189.com/category/gupiaoqihuo/feed"
HI189_ARTICLE_MAX_CHARS = 3000   # 每篇送给 Jev 的最长字数
HI189_FETCH_UNTIL = time(14, 40)  # 这个时刻之后不再联网查新文章（中报 13:00~14:20 发，要赶在 14:50 尾盘买点之前取到）
HI189_MIDDAY_JUDGE = False       # 中报（标题带「(中)」）是否参与情绪判断：False = 只缓存、只记进复盘报告，不交给 Jev（演练期先观察）
HI189_TIMEOUT = 8.0              # 联网超时（秒）

# 自建板块（只记录，不进交易；docs/specs/2026-10-04-own-sectors-design.md）
OWN_SECTORS_ENABLED = True
OWN_SECTORS_BUDGET = 600.0       # 收盘后刷新成员最多花的秒数，没跑完下个交易日接着跑
OWN_SECTORS_LEADERS = 6          # 日快照里记前几名龙头
OWN_SECTORS_CORE_SHARE = 0.5     # 对应产品收入占比 ≥ 此值才算 core（进板块）
OWN_SECTORS_RELATED_SHARE = 0.3  # [此值, CORE) 记为 related（只记录，不入板块）
OWN_SECTORS_REFRESH_DAYS = 30    # 主营构成/介绍缓存多久后重取
OWN_SECTORS_VALID_DAYS = 90      # 成员有效期
OWN_SECTORS_STRONG_PCT = 5.0     # 日快照里「强势」的涨幅线（%）
OWN_SECTORS_JEV_MIN_STRONG = 0   # 板块当天强势成员数 ≥ 此值（或被晚报点名）才问 Jev 强弱；0 = 有成员行情的板块每天都问（用户 2026-10-09：板块情绪全记）
# 盘中板块情绪（用户 2026-10-09：每 30 分钟问一次 Jev），后台线程跑，只记录；约 7 轮 × 有成员的板块数次 Jev/天
OWN_SECTORS_INTRADAY_SLOTS = (time(10, 0), time(10, 30), time(11, 0), time(11, 25),
                              time(13, 30), time(14, 0), time(14, 30))

# 过滤
NOTICE_LOOKBACK_DAYS = 90
NEWS_LOOKBACK_HOURS = 24
MIN_LISTED_TRADING_DAYS = 60
LOW_CONFIDENCE = 0.6

# 公告风险细判（docs/specs/2026-09-26-notice-risk-design.md）
RISK_REJECT = 0.5          # 风险分 ≥ 此值：否决「高风险」
RISK_UNSURE = 0.35         # 风险分落在 [RISK_UNSURE, RISK_REJECT)：否决「拿不准」
RISK_VERSION = 1           # 改判断口径时 +1，旧的 notice_risk 缓存自动失效重判
RISK_MAX_PER_CHECK = 20    # 每票每次最多细判的疑似公告条数
BODY_CHARS = 1500          # 交给 Jev 的正文长度
PREFETCH_CAP = 300         # 早盘预抓正文每次最多条数
PREFETCH_BUDGET = 60.0     # 盘前预抓正文最多等待秒数（到点即返回，不拖交易流程）
PREFETCH_BUDGET_MIDDAY = 20.0  # 13:00 重同步后预抓的等待上限（午盘已开，更短）
RISK_JEV_TIMEOUT = 15.0    # 带正文的 Jev 调用超时（秒）
RISK_BODY_BUDGET = 6.0     # 细判时现抓正文的总等待上限（秒）；到点没回来的按标题判，不堵交易 tick
RISK_JEV_BUDGET = 20.0     # 一次 check 里标题分类、细判 Jev 调用（含重试）各自的总时限（秒）；到点没问完 → error（当日不买）
TICK_JEV_BUDGET = 40.0     # 每分钟所有策略买入阶段合计的 Jev 时间预算（秒）；用完后不再判断新候选，留到下一分钟

# Jev
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
JEV_TIMEOUT = 5.0

DB_PATH = "data/jevq.sqlite"
REPORT_DIR = "reports"

# 策略与账户
STRATEGIES = {
    "board": {"name": "扫首板", "accounts": ("board_main", "board_shadow")},
    "rebound": {"name": "超跌反弹", "accounts": ("rebound_main", "rebound_shadow")},
    # 低吸 2026-10-09 按用户要求重新上线（新账户，旧 main/shadow 的历史不动），给情绪轮动在偏弱/正常时用
    "lowbuy": {"name": "低吸", "accounts": ("lowbuy_main", "lowbuy_shadow")},
    # 情绪轮动（用户 2026-10-09）：按大盘情绪跟上面三个策略主账户的成交，实盘跟它；只有一个账户
    "regime": {"name": "情绪轮动", "accounts": ("regime_main",)},
    # AI 打首板（dj 2026-11-01）：老扫首板的选股和 3 秒扫板 + DeepSeek 开闸（市场不好就空仓）+ 单票不亏损卖出。
    # 新账户，实盘不跟（regime.SOURCE_OF 里没有它）
    "aiboard": {"name": "AI打首板", "accounts": ("aiboard_main", "aiboard_shadow")},
    # 高标竞价接力（dj 2026-11-02）：昨天 3 板以上、竞价高开 3~6%，开盘价买、下一交易日收盘前卖。实盘不跟
    "relay": {"name": "高标接力", "accounts": ("relay_main", "relay_shadow")},
    # 弱转强（dj 2026-11-03，知识库）：昨天炸板的高标、今天竞价高开 0~6%，开盘价买、次日收盘前卖。实盘不跟
    "wts": {"name": "弱转强", "accounts": ("wts_main", "wts_shadow")},
    # 冰点修复（dj 2026-11-03，知识库）：连续两个冰点日（跌停≥10 家且炸板率≥40%）尾盘买一篮子、持有 3 天。实盘不跟
    "ice": {"name": "冰点修复", "accounts": ("ice_main",)},
    # 小市值轮动（dj 2026-11-03「可以」）：对照基线。成交额最小 10% 里选 20 只等权，每 20 个交易日调仓。实盘不跟
    "smallcap": {"name": "小市值轮动", "accounts": ("sc_main",)},
    # 补涨（dj 2026-11-03，research/theme_ladder.py）：题材龙头 ≥5 板，同题材昨日首板，竞价高开 0~3% 买、次日收盘前卖。实盘不跟
    "bz": {"name": "补涨", "accounts": ("bz_main", "bz_shadow")},
    # 灵活账户（dj 2026-11-03「要灵活交易，有时候就得突破一下」）：DeepSeek 自己判断买卖和仓位，只有风控护栏。实盘不跟
    "aiflex": {"name": "AI灵活交易", "accounts": ("aiflex_main",)},
    # 二号交易员（dj 2026-11-05「再加一个 deepseek 交易员」）：同样的执行器和护栏，风格不同——趋势/强势跟随，不打板、不接力。实盘不跟
    "aiflex2": {"name": "AI趋势交易员", "accounts": ("aiflex2_main",)},
}
ACCOUNTS = tuple(a for s in STRATEGIES.values() for a in s["accounts"])
ACCOUNT_NAMES = {
    "main": "低吸·主(已下线)", "shadow": "低吸·影子(已下线)",      # 9-23 版低吸已下线，只给历史成交显示名字
    "board_main": "首板·主(Jev)", "board_shadow": "首板·影子",
    "rebound_main": "超跌·主(Jev)", "rebound_shadow": "超跌·影子",
    "lowbuy_main": "低吸·主(Jev)", "lowbuy_shadow": "低吸·影子",
    "regime_main": "情绪轮动(实盘跟)",
    "aiboard_main": "AI打首板·主(DeepSeek)", "aiboard_shadow": "AI打首板·影子",
    "relay_main": "高标接力·主(Jev)", "relay_shadow": "高标接力·影子",
    "wts_main": "弱转强·主(Jev)", "wts_shadow": "弱转强·影子", "ice_main": "冰点修复·篮子",
    "sc_main": "小市值轮动·篮子",
    "bz_main": "补涨·主(Jev)", "bz_shadow": "补涨·影子", "aiflex_main": "AI灵活交易", "aiflex2_main": "AI趋势交易员",
}
# 高标竞价接力（research/dragon_relay.py：训练 +1.12%/笔 n=268、验证 +1.86%/笔 n=88；单笔最差 −23%）。
# 持有跨两天，两批仓位会重叠：每笔 1/6 本金、最多 6 只，一天最多买 3 只
RELAY_MIN_STREAK = 3
RELAY_GAP_PCT = (3.0, 6.0)        # 竞价开盘涨幅 [3%, 6%)
RELAY_MAX_PER_DAY = 3
RELAY_MAX_POSITIONS = 6
RELAY_CAPITAL = 120_000.0
RELAY_PER_TRADE = RELAY_CAPITAL / RELAY_MAX_POSITIONS
RELAY_BUY_UNTIL = (9, 33)          # 开盘后几分钟内还按开盘价算；更晚启动就不买（价格已经不是竞价价）
RELAY_SELL_AT = (14, 55)
# 弱转强（知识库 + research/kb_events.py：606 笔/450 日，日均 +0.72%，中位 −0.31%，t≈2.6，偏弱的苗头）
WTS_MIN_PRIOR_STREAK = 2          # 炸板前至少几连板
WTS_GAP_PCT = (0.0, 6.0)          # 竞价开盘涨幅 [0%, 6%)
WTS_MAX_PER_DAY = 3
WTS_MAX_POSITIONS = 6
WTS_CAPITAL = 120_000.0
WTS_PER_TRADE = WTS_CAPITAL / WTS_MAX_POSITIONS
WTS_BUY_UNTIL = (9, 33)
WTS_SELL_AT = (14, 55)
# 冰点修复（research/kb_events.py：连续第 2 个冰点日 21 个事件、持有 3 天均 +2.34% 胜 81% 最差 −9.1%，一年 2~3 次）
ICE_DT_MIN = 10                   # 跌停家数 ≥
ICE_BREAK_RATE = 40.0             # 炸板率 % ≥（知识库：超过四成是高风险）
ICE_BASKET = 20                   # 一篮子几只
ICE_CAPITAL = 120_000.0
ICE_PER_TRADE = ICE_CAPITAL / ICE_BASKET
ICE_BUY_AT = (14, 50)             # 尾盘买
ICE_HOLD_DAYS = 3                 # 持有交易日数，第 3 个交易日 ICE_SELL_AT 后卖
ICE_SELL_AT = (14, 55)
ICE_MIN_PRICE = 3.0
# 小市值轮动（research/size_study.py：成交额最小 10%、每 20 个交易日调仓、20 只、扣 0.3% 滑点：年化 +12%、最大回撤 −34%，
# 逐年极不均匀（2021/2022/2025 是大年），未校正退市票的幸存者偏差，真实预期更低）
SC_BASKET = 20
SC_CAPITAL = 120_000.0
SC_PER_TRADE = SC_CAPITAL / SC_BASKET
SC_HOLD_DAYS = 20                 # 每这么多个交易日调仓一次
SC_BOTTOM_PCT = 0.10              # 60 日均成交额最小的 10%
SC_AMOUNT_DAYS = 60
SC_MIN_HISTORY = 40               # 成交额历史不足这么多天的票不参与
SC_MIN_PRICE = 3.0
SC_BUY_AT = (14, 50)
SC_SELL_AT = (14, 50)
# 补涨（research/theme_ladder.py：龙头 ≥5 板的题材里同题材首板，竞价高开 0~3%，次收 +0.51% / +0.83%，n=201/39；高开 3~7% 亏）
BZ_MIN_LEADER = 5                 # 题材龙头至少几板
BZ_GAP_PCT = (0.0, 3.0)           # 竞价开盘涨幅 [0%, 3%)
BZ_MAX_PER_DAY = 3
BZ_MAX_POSITIONS = 6
BZ_CAPITAL = 120_000.0
BZ_PER_TRADE = BZ_CAPITAL / BZ_MAX_POSITIONS
BZ_BUY_UNTIL = (9, 33)
BZ_SELL_AT = (14, 55)
# 灵活账户的护栏（不是选股规则，是让账户活下去；知识库「仓位与风控」）：
FLEX_CAPITAL = 120_000.0
FLEX_MAX_POSITIONS = 5
FLEX_MAX_PCT = 0.30               # 单票最多占账户净值
FLEX_DAY_LOSS = 0.03              # 当日亏到净值的 3% 当天不再开新仓
FLEX_LOSS_STREAK = 3              # 连亏 3 笔，当天不再开新仓
ACCOUNT_CAPITAL = {"relay_main": RELAY_CAPITAL, "relay_shadow": RELAY_CAPITAL,
                   "wts_main": WTS_CAPITAL, "wts_shadow": WTS_CAPITAL, "ice_main": ICE_CAPITAL, "sc_main": SC_CAPITAL,
                   "bz_main": BZ_CAPITAL, "bz_shadow": BZ_CAPITAL, "aiflex_main": FLEX_CAPITAL, "aiflex2_main": FLEX_CAPITAL}
ACCOUNT_MAX_POSITIONS = {"relay_main": RELAY_MAX_POSITIONS, "relay_shadow": RELAY_MAX_POSITIONS,
                         "wts_main": WTS_MAX_POSITIONS, "wts_shadow": WTS_MAX_POSITIONS, "ice_main": ICE_BASKET,
                         "sc_main": SC_BASKET, "bz_main": BZ_MAX_POSITIONS, "bz_shadow": BZ_MAX_POSITIONS,
                         "aiflex_main": FLEX_MAX_POSITIONS, "aiflex2_main": FLEX_MAX_POSITIONS}
STRATEGY_CAPITAL = {"relay": RELAY_CAPITAL, "wts": WTS_CAPITAL, "ice": ICE_CAPITAL, "smallcap": SC_CAPITAL,
                    "bz": BZ_CAPITAL, "aiflex": FLEX_CAPITAL, "aiflex2": FLEX_CAPITAL}
AI_BOARD_ENABLED = True
AI_SCORE_PUSH = False           # dj 2026-11-10：「AI 赛马记分」不再推微信（记分、复盘、经验条照常）
AI_BOARD_FIRST_PCT = 0.5        # 首次建仓只用一半，另一半留给回本（做T/低吸补仓）
# dj 2026-11-02：次日开盘就卖（108 天回测：开盘卖优于等反弹/拿到收盘/止损；「不亏不卖」把亏损放大一倍多）。
# False = 回到单票不亏损卖出（decide_noloss_sell）
AI_BOARD_SELL_OPEN = True
# dj 2026-11-02：闸门改规则（ai_board.rule_gate），DeepSeek 只保留对个股的否决
AI_BOARD_RULE_GATE = True
AI_BOARD_GATE_MAX_AGE = 900     # 秒：DeepSeek 最近一次判断超过这么久就当没有，闸门关（空仓避险）
AI_BOARD_GATE_ACTION = "允许模拟小仓评估"   # DeepSeek 给出这个动作才开闸
AI_DECISIONS_PATH = "dashboard/runtime/ai_decisions.jsonl"
AI_ANSWERS_PATH = "dashboard/runtime/subscription_answers.json"
ACCOUNT_STRATEGY = {a: sid for sid, s in STRATEGIES.items() for a in s["accounts"]}


def builtin_accounts() -> tuple[str, ...]:
    """内置账户（扫首板、超跌反弹、低吸各主/影子 + 情绪轮动 regime_main）。"""
    return ACCOUNTS


# 自定义策略
CUSTOM_PURGE_WHEN_FLAT = ("c2", "c3")   # 收盘后清了仓就删（用户 2026-10-09；c1 当晚已删）
MAX_CUSTOM_ENABLED = 5

# 每日 GitHub 同步 + 收盘后自动上线
SYNC_ENABLED = True
DEPLOY_ENABLED = True

# 涨停补充数据（jevq/data/ztpool.py；dj 2026-10-10 要接）
ZT_EXTRA_ENABLED = True
ZT_EXTRA_TIMEOUT = 5.0            # 秒
ZT_REASON_MIN = 2                 # 同一涨停原因题材 ≥ 这么多家涨停，算主线判断里的「点名」

# 隔夜消息催化 + 人气与资金快照（jevq/data/catalyst.py、market_extra.py；dj 2026-10-10，只记录不进交易）
CATALYST_ENABLED = True
MARKET_EXTRA_ENABLED = True
RSS_TIMEOUT = 10.0                # 秒：每个新闻源
RSS_WORKERS = 12
RSS_BUDGET = 60.0                 # 秒：全部源的总时限（10-10 实测 21 秒抓完 106 个源）
CATALYST_DEADLINE = time(9, 20)   # 过了这个时刻启动（盘中重启）就不抓隔夜新闻：交易循环不能等它
CATALYST_REPORT_TOP = 15          # 复盘报告里列前几个题材
# 题材关键词 → 额外匹配词（英文源、常见别称）。键是题材名里包含的字：「人形机器人」也作用于「人形机器人概念」
CATALYST_ALIASES = {
    "人形机器人": ["humanoid", "optimus", "figure ai", "宇树", "具身智能"],
    "机器人": ["robot", "robotics"],
    "HBM": ["hbm", "high bandwidth memory"],
    "存储": ["dram", "nand", "memory chip"],
    "光模块": ["optical module", "transceiver", "cpo", "silicon photonics", "光通信"],
    "CPO": ["cpo", "co-packaged optics"],
    "算力": ["gpu", "nvidia", "ai chip", "data center", "datacenter", "英伟达", "智算"],
    "芯片": ["semiconductor", "chipmaker", "tsmc", "台积电", "晶圆"],
    "半导体": ["semiconductor", "tsmc", "asml", "台积电", "光刻"],
    "PCB": ["pcb", "printed circuit"],
    "固态电池": ["solid-state battery", "solid state battery"],
    "锂电": ["lithium", "battery"],
    "储能": ["energy storage", "battery storage"],
    "核聚变": ["fusion", "tokamak", "可控核聚变"],
    "核电": ["nuclear power", "nuclear reactor", "smr"],
    "商业航天": ["spacex", "starship", "rocket launch", "satellite", "卫星", "火箭"],
    "卫星": ["satellite", "starlink", "星链"],
    "低空经济": ["evtol", "flying car", "drone", "飞行汽车", "无人机"],
    "无人驾驶": ["autonomous driving", "robotaxi", "self-driving", "waymo", "fsd"],
    "智能驾驶": ["autonomous driving", "robotaxi", "self-driving", "fsd"],
    "创新药": ["fda approval", "clinical trial", "biotech", "glp-1", "adc"],
    "减肥药": ["glp-1", "obesity drug", "semaglutide", "tirzepatide"],
    "量子": ["quantum"],
    "网络安全": ["cybersecurity", "ransomware", "data breach", "黑客"],
    "短剧": ["短剧", "微短剧", "漫剧"],
    "AI应用": ["chatgpt", "openai", "agent", "大模型", "智能体"],
    "人工智能": ["openai", "anthropic", "llm", "大模型", "生成式"],
    "DeepSeek": ["deepseek"],
    "华为": ["huawei", "鸿蒙", "harmonyos"],
    "苹果": ["apple", "iphone"],
    "特斯拉": ["tesla"],
    "脑机接口": ["brain-computer", "neuralink"],
    "稀土": ["rare earth"],
    "光伏": ["solar", "photovoltaic"],
    "风电": ["wind power", "offshore wind"],
}

# 本机 claude 命令行（订阅登录）：jevq/judge/claude_cli.py
CLAUDE_BIN = r"%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
CLAUDE_MODEL = "sonnet"
CLAUDE_PROXY = "http://127.0.0.1:8889"   # 命令行 claude 不走 Windows 系统代理，要显式给（VPN 端口改了这里跟着改）
CLAUDE_JUDGE_TIMEOUT = 25.0             # 秒：大盘/板块情绪单次上限（在买入阶段同步问，不能拖太久；超时退回 Jev）
SENTIMENT_BY_CLAUDE = True              # 用户 2026-10-14：大盘情绪（含外围）改由 Claude 判，失败退回 Jev
SECTOR_STRENGTH_BY_CLAUDE = True        # 用户 2026-10-14：自建板块情绪改由 Claude 判（成员归类仍用 Jev）
CLAUDE_NEWS_MINUTES = 90                # Claude 判大盘情绪时给最近多少分钟的 7x24 快讯（Jev 时是 60 分钟 20 条）
CLAUDE_NEWS_LIMIT = 40
CLAUDE_CATALYST_TOP = 10                # 隔夜消息催化给前几个题材
CLAUDE_TIMEOUT = 60.0                    # 秒；10-14 实测一次 5~9 秒

# 收盘后导出到 Vibe-Research（jevq/sync/vibe.py；dj 2026-10-10 要自动导出导入）
VIBE_ENABLED = True
VIBE_DIR = r"E:\vibe-research-agent"
VIBE_PORT = 8765
VIBE_OUT_DIR = "data/vibe"        # data/ 不进 git：复盘、文章、实盘成交都不能进公开仓库
VIBE_START_TIMEOUT = 60           # 秒：临时起 Vibe API 等它就绪的上限

# 实盘跟单（docs/specs/2026-09-29-live-trading-design.md）
# dj 2026-11-01：老规则（扫首板/超跌/低吸/情绪轮动/自定义）关掉，只卖掉现有持仓，不再开新仓；改由 DeepSeek 模拟验证
LEGACY_BUY_ENABLED = False
LIVE_ACCOUNT = "regime_main"     # 2026-10-09 起实盘跟情绪轮动（原来跟 rebound_main）
AI_ACCOUNT = "ai_experiment"     # 独立 AI 模拟账户；当前只允许桥接到 dryrun，不改变旧实盘跟单来源
AI_TOTAL_BUDGET = 10000.0        # AI 实验本金硬上限，不用旧 LIVE_BUDGET 的全部可用资金口径
LIVE_BUDGET = None               # 实盘单笔最多用多少资金；None = 全部可用资金（2026-10-08 用户允许用全部资金，原来是 2 万）
LIVE_MAX_POSITIONS = 1
LIVE_MAX_BUYS_PER_DAY = 2
LIVE_INTENT_TTL = 60            # 秒：意图超过这么久没处理就作废（买入）
LIVE_BUY_SLIP = 0.005           # 买入限价最多比模拟成交价高 0.5%
LIVE_BUY_WAIT = 30              # 秒：买单挂多久没成交完就撤
LIVE_SELL_DISCOUNT = 0.01       # 卖出限价 = 最新价 × (1 - 1%)，不低于跌停价
LIVE_SELL_WAIT = 20             # 秒：卖单每轮等多久
LIVE_SELL_ROUNDS = 3            # 卖单撤了重挂最多几轮，之后每分钟重试
LIVE_SELL_RETRY = 60            # 秒：几轮都没卖完后，下次重试间隔
LIVE_CASH_BUFFER = 0.002        # 算股数时资金留 0.2% 余量
LIVE_POLL = 5                   # 秒：等成交时查当日委托的间隔
LIVE_CANCEL_WAIT = 30           # 秒：撤单后等「已报待撤」变成终态最多多久，还没变就自动暂停
LIVE_RECONCILE_HOLDING = 5      # 分钟：有持仓时对账间隔
LIVE_RECONCILE_IDLE = 15        # 分钟：空仓时读资金确认在线的间隔
LIVE_READ_FAIL_PAUSE = 3        # 连续读取失败几次自动暂停
LIVE_RECONCILE_RETRY = 1        # 分钟：定时读取失败后多久再读（网关挂了约 2 分钟内就能暂停，不用等 5/15 分钟）
LIVE_REDUCE_CONFIRM = 60        # 秒：客户端股数变少，两次读数至少隔这么久才减账
LIVE_BUY_WINDOWS = ((time(9, 30), time(11, 30)), (time(13, 0), time(14, 57)))
LIVE_SELL_WINDOWS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))
# 连续竞价：只有这里面能撤单（午休、14:57 后的收盘集合竞价都撤不了）
LIVE_CANCEL_WINDOWS = ((time(9, 30), time(11, 30)), (time(13, 0), time(14, 57)))
LIVE_ORDER_MARGIN = 20          # 秒：网关下单/查询/撤单的耗时余量，撤单至少要在连续竞价结束前这么久发出
LIVE_READ_EST = 10              # 秒：网关读一次当日委托的耗时上估（实测约 6 秒），等成交时据此提前收手去撤单
LIVE_MATCH_TOLERANCE = 30       # 秒：没有委托号时按委托时间匹配，容忍电脑时钟比券商快这么多
LIVE_SELL_SETTLE = time(15, 1)  # 收盘后结算挂着的卖单（留 1 分钟等集合竞价成交回显）
LIVE_PAUSE_CLOSE = time(15, 0)        # 「暂停到收盘」的截止
LIVE_RESTART_CHECK = time(15, 10)     # 之后若 git rev-parse HEAD 和启动时不同，live.py 自我重启
LIVE_RESTART_UNTIL = time(17, 0)      # 自我重启检查的截止（15:10–17:00 之间反复查）
LIVE_RESTART_EVERY = 300              # 秒：自我重启检查的间隔（deploy 先跑测试，rebase 常常晚于 15:10）
LIVE_SELF_CHECK = time(9, 5)
LIVE_GATEWAY_PRECHECKS = (time(8, 52), time(9, 0))   # 开盘前查网关 /health（不读客户端）：卡死/没登录/连不上就推送
LIVE_CLOSE_SUMMARY = time(15, 2)
LIVE_CLOSE_SUMMARY_LATEST = time(15, 10)   # 收盘后结算一直读不到时，小结最晚这个点照发（注明有卖单没结算）
LIVE_WATCHDOG_STALE = 120       # 秒：心跳多久没更新算断
LIVE_WATCHDOG_MAX_RESTARTS = 3  # daemon 每天最多替它重启几次
LIVE_HTTP_PORT = 8766
LIVE_BROKER = "gateway"         # "gateway" | "easytrader"：家里实测后定
LIVE_GATEWAY_URL = "http://127.0.0.1:5000"
LIVE_EASYTRADER_EXE = r"C:\同花顺软件\同花顺\xiadan.exe"   # 实测时按实际安装路径改
LIVE_LOCK_NAME = "live.lock"
