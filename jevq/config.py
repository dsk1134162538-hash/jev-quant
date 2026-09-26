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

# 卖出
STOP_LOSS = -3.0
ZERO_LINE = -0.2
REBOUND = 1.5
TAKE_PROFIT = 1.0
LOW_OPEN = -1.0

# 时间
MARKET_OPEN = time(9, 30)
TAIL_BUY_START = time(14, 50)
BUY_END = time(14, 57)
EOD_SELL = time(14, 55)
SENTIMENT_REFRESH_MIN = 30

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

# Jev
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
JEV_TIMEOUT = 5.0

DB_PATH = "data/jevq.sqlite"
REPORT_DIR = "reports"

# 策略与账户
STRATEGIES = {
    "lowbuy": {"name": "低吸", "accounts": ("main", "shadow")},
    "board": {"name": "扫首板", "accounts": ("board_main", "board_shadow")},
}
ACCOUNTS = STRATEGIES["lowbuy"]["accounts"] + STRATEGIES["board"]["accounts"]
ACCOUNT_NAMES = {
    "main": "低吸·主(Jev)", "shadow": "低吸·影子",
    "board_main": "首板·主(Jev)", "board_shadow": "首板·影子",
}
ACCOUNT_STRATEGY = {a: sid for sid, s in STRATEGIES.items() for a in s["accounts"]}


def builtin_accounts() -> tuple[str, ...]:
    """内置四个账户（低吸 main/shadow + 扫首板 board_main/board_shadow）。"""
    return ACCOUNTS


# 自定义策略
MAX_CUSTOM_ENABLED = 5

# 每日 GitHub 同步 + 收盘后自动上线
SYNC_ENABLED = True
DEPLOY_ENABLED = True
