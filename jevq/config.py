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
REBOUND_STOP_CONFIRM = 2       # 卖出（主账户）：现价连续几分钟在止损线下才算止损，防瞬间下探（用户 2026-10-09 定）
REBOUND_HARD_STOP = -3.5       # 卖出（主账户）：硬止损（%），到了直接卖，不等确认、不问 Jev
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

# hi189「股票期货」文章（docs/specs/2026-10-04-hi189-articles-design.md）
HI189_ENABLED = True             # 总开关：False = 完全等同没接文章
HI189_FEED_URL = "https://hi189.com/category/gupiaoqihuo/feed"
HI189_ARTICLE_MAX_CHARS = 3000   # 每篇送给 Jev 的最长字数
HI189_FETCH_UNTIL = time(14, 40)  # 这个时刻之后不再联网查新文章（中报 13:00~14:20 发，要赶在 14:50 尾盘买点之前取到）
HI189_MIDDAY_JUDGE = False       # 中报（标题带「(中)」）是否参与情绪判断：False = 只缓存、只记进复盘报告，不交给 Jev（演练期先观察）
HI189_TIMEOUT = 8.0              # 联网超时（秒）

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
}
ACCOUNTS = tuple(a for s in STRATEGIES.values() for a in s["accounts"])
ACCOUNT_NAMES = {
    "main": "低吸·主(已下线)", "shadow": "低吸·影子(已下线)",      # 低吸已下线，只给历史成交显示名字
    "board_main": "首板·主(Jev)", "board_shadow": "首板·影子",
    "rebound_main": "超跌·主(Jev)", "rebound_shadow": "超跌·影子",
}
ACCOUNT_STRATEGY = {a: sid for sid, s in STRATEGIES.items() for a in s["accounts"]}


def builtin_accounts() -> tuple[str, ...]:
    """内置账户（扫首板 board_main/board_shadow + 超跌反弹 rebound_main/rebound_shadow）。"""
    return ACCOUNTS


# 自定义策略
MAX_CUSTOM_ENABLED = 5

# 每日 GitHub 同步 + 收盘后自动上线
SYNC_ENABLED = True
DEPLOY_ENABLED = True

# 实盘跟单（docs/specs/2026-09-29-live-trading-design.md）
LIVE_ACCOUNT = "rebound_main"
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
LIVE_CLOSE_SUMMARY = time(15, 2)
LIVE_CLOSE_SUMMARY_LATEST = time(15, 10)   # 收盘后结算一直读不到时，小结最晚这个点照发（注明有卖单没结算）
LIVE_WATCHDOG_STALE = 120       # 秒：心跳多久没更新算断
LIVE_WATCHDOG_MAX_RESTARTS = 3  # daemon 每天最多替它重启几次
LIVE_HTTP_PORT = 8766
LIVE_BROKER = "gateway"         # "gateway" | "easytrader"：家里实测后定
LIVE_GATEWAY_URL = "http://127.0.0.1:5000"
LIVE_EASYTRADER_EXE = r"C:\同花顺软件\同花顺\xiadan.exe"   # 实测时按实际安装路径改
LIVE_LOCK_NAME = "live.lock"
