"""CustomStrategy 测试：选股/排序/买点/卖出、Jev 否决、缺数据、T+1、异常隔离、trace 含 hits、can_buy=False 只卖。"""
import json
from datetime import date, datetime

from factories import quote

from jevq import db
from jevq.data.closes import save_closes
from jevq.data.features import history_features
from jevq.ledger import Ledger
from jevq.strategies.custom import CustomStrategy

DAY = datetime(2026, 9, 24)
PREV_DAY = date(2026, 9, 23)


def at(h, m, d=DAY):
    return d.replace(hour=h, minute=m)


class Judge:
    def __init__(self, verdicts=None):
        self.verdicts, self.calls = verdicts or {}, []
        self.last_trace = None

    def check(self, code, *args):
        self.calls.append(code)
        verdict, detail = self.verdicts.get(code, ("pass", "ok"))
        self.last_trace = {"test": "trace"} if verdict != "pass" else None
        return verdict, detail


UNIVERSE = {c: f"股票{c}" for c in ["600001", "600002", "600003", "600004"]}
CONCEPT_MAP = {"600001": ["AI"], "600002": ["AI"], "600003": [], "600004": []}


def make(spec, notice=None, news=None, features=None, can_buy=True):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)

    # 准备历史数据
    save_closes(conn, PREV_DAY, {"600001": 10.0, "600002": 10.0, "600003": 10.0})

    if features is None:
        features = history_features(conn, UNIVERSE.keys(), DAY.date())

    env_provider = lambda: {"sentiment": "正常", "overseas": "中性"}

    strat = CustomStrategy(
        conn, ledger, "c1", 1, spec,
        notice or Judge(), news or Judge(),
        UNIVERSE, features, CONCEPT_MAP,
        env_provider, PREV_DAY, can_buy=can_buy
    )
    return strat, conn, ledger


def test_select_filters_candidates():
    """选股：只有满足 select 条件的股票才进入候选。"""
    spec = {
        "name": "测试选股",
        "capital": 30000,
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 5.0}]},
        "entry": {},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
        "jev": {"notices": False, "news": False}
    }
    strat, conn, ledger = make(spec)

    quotes = {
        "600001": quote("600001", pct=6.0, amount=2e8, ts=DAY),
        "600002": quote("600002", pct=3.0, amount=1e8, ts=DAY),  # pct 不满足
    }

    fills = strat.on_minute(quotes, at(10, 0))

    # 只有 600001 满足选股条件，主账户和影子账户都买入（Jev 关闭）
    assert len(fills) == 2
    assert all(f.code == "600001" for f in fills)
    assert {f.account for f in fills} == {"c1_main", "c1_shadow"}


def test_rank_orders_candidates():
    """排序：按指定字段排序候选股票。"""
    spec = {
        "name": "测试排序",
        "capital": 30000,
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 3.0}]},
        "entry": {},
        "rank": {"field": "amount", "order": "desc"},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
        "jev": {"notices": False, "news": False}
    }
    strat, conn, ledger = make(spec)

    quotes = {
        "600001": quote("600001", pct=5.0, amount=1e8, ts=DAY),
        "600002": quote("600002", pct=4.0, amount=3e8, ts=DAY),  # 成交额最大
    }

    fills = strat.on_minute(quotes, at(10, 0))

    # 主账户和影子账户都买排名第一的（成交额最大）
    assert len(fills) == 2
    assert all(f.code == "600002" for f in fills)


def test_entry_filters_before_buy():
    """买点：entry 条件过滤，不满足则不买。"""
    spec = {
        "name": "测试买点",
        "capital": 30000,
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 3.0}]},
        "entry": {"conditions": [{"field": "amount", "op": ">=", "value": 2.0}]},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
        "jev": {"notices": False, "news": False}
    }
    strat, conn, ledger = make(spec)

    quotes = {
        "600001": quote("600001", pct=5.0, amount=1e8, ts=DAY),  # 成交额 1 亿，不满足 entry
        "600002": quote("600002", pct=4.0, amount=3e8, ts=DAY),  # 成交额 3 亿，满足
    }

    fills = strat.on_minute(quotes, at(10, 0))

    # 只有 600002 满足 entry 条件，主账户和影子账户都买入
    assert len(fills) == 2
    assert all(f.code == "600002" for f in fills)


def test_exit_triggers_sell():
    """卖出：exit 条件命中时卖出。"""
    spec = {
        "name": "测试卖出",
        "capital": 30000,
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 0}]},
        "entry": {},
        "exits": [
            {"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -3}]}, "reason": "止损"},
            {"when": {"conditions": [{"field": "hold_days", "op": ">=", "value": 2}]}, "reason": "持有超期"}
        ],
        "jev": {"notices": False, "news": False}
    }
    strat, conn, ledger = make(spec)

    # 先买入
    quotes = {"600001": quote("600001", pct=5.0, amount=2e8, ts=DAY)}
    strat.on_minute(quotes, at(10, 0))

    # 第二天跌破止损线
    next_day = at(10, 0, DAY.replace(day=25))
    quotes_next = {"600001": quote("600001", pct=-9.5, pre_close=10.5, amount=2e8, ts=next_day)}

    fills = strat.on_minute(quotes_next, next_day)

    # 触发止损卖出
    assert len(fills) == 2  # 主账户和影子账户各一个
    assert all(f.side == "sell" for f in fills)
    assert all(f.reason == "止损" for f in fills)


def test_jev_reject_blocks_main_not_shadow():
    """Jev 否决：主账户被否决，影子账户照常买入。"""
    spec = {
        "name": "测试 Jev",
        "capital": 30000,
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 3.0}]},
        "entry": {},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
        "jev": {"notices": True, "news": True}
    }

    notice = Judge({"600001": ("reject", "新增减持")})
    strat, conn, ledger = make(spec, notice=notice)

    quotes = {"600001": quote("600001", pct=5.0, amount=2e8, ts=DAY)}
    fills = strat.on_minute(quotes, at(10, 0))

    # 影子账户买入，主账户被否决
    assert len(fills) == 1
    assert fills[0].account == "c1_shadow"

    # 检查决策记录
    row = conn.execute("SELECT verdict, detail FROM decisions WHERE code='600001'").fetchone()
    assert row[0] == "reject"
    assert row[1] == "新增减持"


def test_missing_data_field_not_matched():
    """缺数据：字段值为 None 时条件不命中。"""
    spec = {
        "name": "测试缺数据",
        "capital": 30000,
        "select": {"conditions": [{"field": "pe_ttm", "op": "<=", "value": 50}]},  # pe_ttm 缺失
        "entry": {},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
        "jev": {"notices": False, "news": False}
    }
    strat, conn, ledger = make(spec)

    quotes = {"600001": quote("600001", pct=5.0, amount=2e8, ts=DAY)}  # pe_ttm 默认为 0，视为缺失
    fills = strat.on_minute(quotes, at(10, 0))

    # pe_ttm 缺失（默认为 0），选股条件不命中，不买入
    assert len(fills) == 0


def test_t1_no_sell_on_buy_day():
    """T+1：买入当天不卖出。"""
    spec = {
        "name": "测试 T+1",
        "capital": 30000,
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 0}]},
        "entry": {},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
        "jev": {"notices": False, "news": False}
    }
    strat, conn, ledger = make(spec)

    # 买入
    quotes = {"600001": quote("600001", pct=5.0, amount=2e8, ts=at(10, 0))}
    strat.on_minute(quotes, at(10, 0))

    # 同一天内跌破止损线
    quotes_later = {"600001": quote("600001", pct=-14.3, pre_close=10.5, amount=2e8, ts=at(14, 0))}
    fills = strat.on_minute(quotes_later, at(14, 0))

    # T+1 限制，当天不卖出
    assert len(fills) == 0


def test_stale_quotes_skipped():
    """非当日行情：跳过旧行情，不触发卖出。"""
    spec = {
        "name": "测试旧行情",
        "capital": 30000,
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 0}]},
        "entry": {},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
        "jev": {"notices": False, "news": False}
    }
    strat, conn, ledger = make(spec)

    # 买入
    quotes = {"600001": quote("600001", pct=5.0, amount=2e8, ts=at(10, 0))}
    strat.on_minute(quotes, at(10, 0))

    # 第二天，但行情时间戳是昨天的
    next_day = at(10, 0, DAY.replace(day=25))
    stale_quotes = {"600001": quote("600001", pct=-14.3, pre_close=10.5, amount=2e8, ts=at(14, 0))}

    fills = strat.on_minute(stale_quotes, next_day)

    # 旧行情被跳过，不卖出
    assert len(fills) == 0


def test_exception_isolated_per_position():
    """异常隔离：单个持仓出错不影响其他持仓卖出。"""
    spec = {
        "name": "测试异常隔离",
        "capital": 100000,  # 增大资金以便买两只
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 0}]},
        "entry": {},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -3}]}, "reason": "止损"}],
        "jev": {"notices": False, "news": False}
    }
    strat, conn, ledger = make(spec)

    # 买入两只
    quotes = {
        "600001": quote("600001", pct=5.0, amount=2e8, ts=at(10, 0)),
        "600002": quote("600002", pct=4.0, amount=2e8, ts=at(10, 0))
    }
    strat.on_minute(quotes, at(10, 0))

    # 保存当天收盘价，并添加第二天到 closes 表（用于计算 hold_days）
    save_closes(conn, DAY.date(), {"600001": 10.5, "600002": 10.4})
    next_day_date = DAY.replace(day=25).date()
    save_closes(conn, next_day_date, {"600002": 9.41})

    # 重新构造策略实例以获取新的 features（包含第二天）
    features = history_features(conn, UNIVERSE.keys(), next_day_date)
    strat2 = CustomStrategy(
        conn, ledger, "c1", 1, spec,
        Judge(), Judge(),
        UNIVERSE, features, CONCEPT_MAP,
        lambda: {"sentiment": "正常", "overseas": "中性"}, DAY.date(), can_buy=True
    )

    # 第二天，600001 行情缺失（模拟异常），600002 正常卖出
    next_day = at(10, 0, DAY.replace(day=25))
    quotes_next = {
        "600002": quote("600002", pct=-9.5, pre_close=10.4, amount=2e8, ts=next_day)
    }

    fills = strat2.on_minute(quotes_next, next_day)

    # 600002 正常卖出（主账户和影子账户各一个）
    assert len(fills) == 2
    assert all(f.code == "600002" for f in fills)


def test_trace_contains_hits():
    """trace 含 hits：决策记录包含命中的条件描述。"""
    spec = {
        "name": "测试 trace",
        "capital": 30000,
        "select": {"conditions": [
            {"field": "pct", "op": ">=", "value": 5.0},
            {"field": "amount", "op": ">=", "value": 1.0}
        ]},
        "entry": {"conditions": [{"field": "turnover", "op": ">=", "value": 3.0}]},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
        "jev": {"notices": False, "news": False}
    }
    strat, conn, ledger = make(spec)

    quotes = {"600001": quote("600001", pct=6.0, amount=2e8, turnover=5.0, ts=DAY)}
    strat.on_minute(quotes, at(10, 0))

    # 检查主账户的决策记录
    row = conn.execute(
        "SELECT trace FROM decisions WHERE code='600001' AND verdict='pass'"
    ).fetchone()

    trace = json.loads(row[0])
    hits = trace["candidate"]["hits"]

    # 应该包含选股和买点的命中条件
    assert len(hits) >= 2
    assert any("当前涨幅" in h["desc"] for h in hits)
    assert any("成交额" in h["desc"] for h in hits)


def test_can_buy_false_only_sells():
    """can_buy=False：只卖出，不买入。"""
    spec = {
        "name": "测试只卖",
        "capital": 30000,
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 0}]},
        "entry": {},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -3}]}, "reason": "止损"}],
        "jev": {"notices": False, "news": False}
    }

    # 先 can_buy=True 买入
    strat_buy, conn, ledger = make(spec, can_buy=True)
    quotes = {"600001": quote("600001", pct=5.0, amount=2e8, ts=at(10, 0))}
    strat_buy.on_minute(quotes, at(10, 0))

    # 重新构造策略，can_buy=False
    strat_sell = CustomStrategy(
        conn, ledger, "c1", 1, spec,
        Judge(), Judge(),
        UNIVERSE, history_features(conn, UNIVERSE.keys(), DAY.date()), CONCEPT_MAP,
        lambda: {"sentiment": "正常", "overseas": "中性"}, PREV_DAY, can_buy=False
    )

    # 有新候选股票，但不买入
    quotes_new = {
        "600001": quote("600001", pct=-9.5, pre_close=10.5, amount=2e8, ts=at(10, 0, DAY.replace(day=25))),
        "600002": quote("600002", pct=5.0, amount=2e8, ts=at(10, 0, DAY.replace(day=25)))
    }

    fills = strat_sell.on_minute(quotes_new, at(10, 0, DAY.replace(day=25)))

    # 只卖出持仓的 600001，不买入 600002
    assert len(fills) == 2
    assert all(f.side == "sell" for f in fills)
    assert all(f.code == "600001" for f in fills)


def test_check_limit_10_per_minute():
    """每分钟最多检查 10 只股票。"""
    spec = {
        "name": "测试检查限制",
        "capital": 300000,  # 增大资金
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 0}]},
        "entry": {},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
        "jev": {"notices": True, "news": True}
    }

    notice = Judge()
    strat, conn, ledger = make(spec, notice=notice)

    # 准备 15 只候选股票
    quotes = {f"60000{i}": quote(f"60000{i}", pct=5.0, amount=2e8, ts=DAY) for i in range(15)}
    for code in quotes:
        UNIVERSE[code] = f"股票{code}"

    strat.on_minute(quotes, at(10, 0))

    # 最多检查 10 只（但只有前面几只会真正调用 Jev）
    assert strat.check_count <= 10


def test_error_count_halts_at_3():
    """错误 3 次熔断。"""
    spec = {
        "name": "测试错误熔断",
        "capital": 30000,
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 0}]},
        "entry": {},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
        "jev": {"notices": True, "news": True}
    }

    # 模拟 notice judge 抛异常
    class ErrorJudge:
        def check(self, *args):
            raise ValueError("测试异常")

    strat, conn, ledger = make(spec, notice=ErrorJudge())

    # 准备多只候选股票
    quotes = {f"60000{i}": quote(f"60000{i}", pct=5.0, amount=2e8, ts=DAY) for i in range(10)}
    for code in quotes:
        UNIVERSE[code] = f"股票{code}"

    strat.on_minute(quotes, at(10, 0))

    # 错误计数达到 3 次后停止
    assert strat.error_count == 3
