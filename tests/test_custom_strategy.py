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
    """异常隔离：卖出时出错不会让策略崩溃，错误被捕获并打印。"""
    spec = {
        "name": "测试异常隔离",
        "capital": 30000,
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 0}]},
        "entry": {},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -3}]}, "reason": "止损"}],
        "jev": {"notices": False, "news": False}
    }
    strat, conn, ledger = make(spec)

    # 买入 600001
    quotes = {"600001": quote("600001", pct=5.0, amount=2e8, ts=at(10, 0))}
    strat.on_minute(quotes, at(10, 0))

    # 保存收盘价
    save_closes(conn, DAY.date(), {"600001": 10.5})
    next_day_date = DAY.replace(day=25).date()
    save_closes(conn, next_day_date, {})

    # 构造新策略实例
    features = history_features(conn, UNIVERSE.keys(), next_day_date)
    strat2 = CustomStrategy(
        conn, ledger, "c1", 1, spec,
        Judge(), Judge(),
        UNIVERSE, features, CONCEPT_MAP,
        lambda: {"sentiment": "正常", "overseas": "中性"}, DAY.date(), can_buy=True
    )

    # 第二天，行情触发卖出但人为破坏数据库让 sell 抛异常
    next_day = at(10, 0, DAY.replace(day=25))
    quotes_next = {"600001": quote("600001", pct=-9.5, pre_close=10.5, amount=2e8, ts=next_day)}

    # 删除 cash 表触发异常
    conn.execute("DROP TABLE cash")
    conn.commit()

    # 调用 on_minute 不应该崩溃
    fills = strat2.on_minute(quotes_next, next_day)

    # 异常被捕获，返回空列表
    assert fills == []


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


def test_time_condition_hit_is_json_serializable():
    """用了「时间」条件的策略：hits 里的当时取值（datetime.time）必须能写进决策 trace，否则永远买不进。"""
    spec = {
        "name": "尾盘",
        "capital": 30000,
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 5.0}]},
        "entry": {"conditions": [{"field": "time", "op": ">=", "value": "09:45"}]},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
        "jev": {"notices": False, "news": False}
    }
    strat, conn, ledger = make(spec)
    quotes = {"600001": quote("600001", pct=6.0, amount=2e8, turnover=5.0, ts=DAY)}
    strat.on_minute(quotes, at(10, 0))

    row = conn.execute("SELECT trace FROM decisions WHERE code='600001' AND verdict='pass'").fetchone()
    assert row is not None
    hits = json.loads(row[0])["candidate"]["hits"]
    assert any(h["value"] == "10:00" for h in hits)
    assert ledger.positions("c1_main")


# ---- leader_rank：题材内龙头名次，与扫首板 board.leader 同口径 ----
from factories import quote as _q  # noqa: E402

from jevq.rules.board import leader  # noqa: E402
from jevq.strategies.custom import leader_ranks, make_getter, minute_context, preview, _fmt_value  # noqa: E402


def _feat(ret5):
    return {"ret": {5: ret5}}


def test_leader_rank_matches_board_leader():
    quotes = {"600001": _q("600001", pct=9.0, amount=5e8), "600002": _q("600002", pct=7.0, amount=9e8),
              "600003": _q("600003", pct=6.0, amount=1e8), "600004": _q("600004", pct=2.0, amount=9e9)}
    cmap = {c: ["AI"] for c in quotes}
    features = {"600001": _feat(20.0), "600002": _feat(5.0), "600003": _feat(1.0), "600004": _feat(50.0)}
    ranks = leader_ranks(quotes, cmap, features)
    ret5 = {c: f["ret"][5] for c, f in features.items()}
    assert ranks[leader("AI", ["600001", "600002", "600003"], quotes, ret5).code] == 1
    assert ranks == {"600001": 1, "600002": 2, "600003": 3}
    assert "600004" not in ranks                         # 涨幅不到 5%，不是强势成员 → 没有名次


def test_leader_rank_excludes_st_missing_ret5_and_uses_hottest_concept():
    quotes = {"600001": _q("600001", pct=9.0, amount=1e8), "600002": _q("600002", pct=8.0, amount=2e8),
              "600003": _q("600003", pct=7.0, amount=3e8), "600005": _q("600005", pct=9.5, name="*ST甲"),
              "600006": _q("600006", pct=9.9, amount=9e9)}
    cmap = {"600001": ["小", "大"], "600002": ["大"], "600003": ["大"], "600005": ["大"], "600006": ["小"]}
    features = {"600001": _feat(1.0), "600002": _feat(30.0), "600003": _feat(20.0), "600005": _feat(99.0)}
    ranks = leader_ranks(quotes, cmap, features)
    assert "600005" not in ranks and "600006" not in ranks      # ST、缺 5 日涨幅都不参与
    # 600001 在「小」里是唯一有名次的（第 1），但「大」有 3 只强势成员更热 → 取「大」里的名次
    assert ranks["600001"] == 3 and ranks["600002"] == 1


def test_leader_rank_field_reaches_getter_and_preview():
    quotes = {"600001": _q("600001", pct=9.0, amount=5e8), "600002": _q("600002", pct=7.0, amount=1e8)}
    universe = {"600001": "甲", "600002": "乙"}
    cmap = {"600001": ["AI"], "600002": ["AI"]}
    features = {"600001": _feat(20.0), "600002": _feat(5.0)}
    amount_ranks, heats, leaders = minute_context(quotes, universe, cmap, features)
    get = make_getter("600002", quotes["600002"], features=features["600002"], amount_ranks=amount_ranks,
                      concept_heats=heats, env={}, t=DAY.time(), leader_ranks=leaders)
    assert get("leader_rank") == 2
    spec = {"select": {"conditions": [{"field": "leader_rank", "op": "<=", "value": 1}]}}
    result = preview(spec, quotes, universe, features, cmap, {}, DAY)
    assert result["total"] == 1 and result["rows"][0]["code"] == "600001"
    assert result["rows"][0]["values"] == {"题材内龙头名次": "1名"}


def test_leader_rank_used_by_custom_strategy_on_minute():
    spec = {"name": "龙头", "capital": 30000,
            "select": {"conditions": [{"field": "leader_rank", "op": "<=", "value": 1}]},
            "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -3}]}, "reason": "止损"}]}
    features = {c: _feat(v) for c, v in {"600001": 20.0, "600002": 5.0, "600003": 0.0, "600004": 0.0}.items()}
    strat, conn, ledger = make(spec, features=features)
    quotes = {"600001": _q("600001", pct=6.0, amount=5e8, ts=at(10, 0)),
              "600002": _q("600002", pct=7.0, amount=1e8, ts=at(10, 0))}
    strat.on_minute(quotes, at(10, 0))
    assert [p.code for p in ledger.positions("c1_shadow")] == ["600001"]


def test_preview_values_use_two_decimals_for_floats():
    assert _fmt_value(-2.6, "%") == "-2.60%" and _fmt_value(-2.21, "%") == "-2.21%"
    assert _fmt_value(3, "名") == "3名" and _fmt_value(True, "") == "是" and _fmt_value(None, "%") == "—"


# ---- Task 4b：卖出/买入拆成两个阶段；买入阶段受每分钟 Jev 预算管 ----

PHASE_SPEC = {
    "name": "阶段测试", "capital": 30000,
    "select": {"conditions": [{"field": "pct", "op": ">=", "value": 5.0}]},
    "entry": {},
    "exits": [{"when": {"conditions": [{"field": "pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
    "jev": {"notices": True, "news": True},
}


def _custom_scenario(step):
    notice = Judge({"600001": ("reject", "减持")})
    strat, conn, ledger = make(PHASE_SPEC, notice=notice)
    ledger.buy("c1_main", quote("600004", pct=0.0, ts=datetime(2026, 9, 23, 10, 0)),
               datetime(2026, 9, 23, 10, 0), "x")
    quotes = {"600001": quote("600001", pct=6.0, amount=3e8, ts=DAY),
              "600002": quote("600002", pct=7.0, amount=2e8, ts=DAY),
              "600004": quote("600004", pct=-6.0, ts=DAY)}
    fills = [step(strat, quotes, at(10, 0)), step(strat, quotes, at(10, 1))]
    rows = conn.execute("SELECT ts, code, verdict, detail FROM decisions").fetchall()
    return [[(f.account, f.code, f.side, f.reason) for f in fs] for fs in fills], rows, notice.calls


def test_custom_sell_step_then_buy_step_equals_on_minute():
    by_minute = _custom_scenario(lambda s, q, t: s.on_minute(q, t))
    by_phase = _custom_scenario(lambda s, q, t: s.sell_step(q, t) + s.buy_step(q, t, None))
    assert by_phase == by_minute
    sides = {f[2] for f in by_minute[0][0]}
    assert sides == {"buy", "sell"}                    # 场景里确实有卖有买


def test_custom_sell_step_does_not_call_judge():
    notice, news = Judge(), Judge()
    strat, _, ledger = make(PHASE_SPEC, notice=notice, news=news)
    strat.sell_step({"600001": quote("600001", pct=6.0, ts=DAY)}, at(10, 0))
    assert notice.calls == [] and news.calls == []
    assert ledger.positions("c1_main") == [] and ledger.positions("c1_shadow") == []


class ClockJudge(Judge):
    def __init__(self, clock, step, verdicts=None):
        super().__init__(verdicts)
        self.clock, self.step = clock, step

    def check(self, code, *args):
        self.clock[0] += self.step
        return super().check(code, *args)


def test_custom_deadline_leaves_rest_undecided_and_checks_next_minute(monkeypatch):
    import jevq.strategies.custom as custom_mod
    clock = [0.0]
    monkeypatch.setattr(custom_mod, "monotonic", lambda: clock[0])
    quotes = {c: quote(c, pct=6.0, amount=a, ts=DAY)
              for c, a in [("600001", 4e8), ("600002", 3e8), ("600003", 2e8)]}
    notice = ClockJudge(clock, 30.0, {c: ("reject", "x") for c in quotes})
    spec = dict(PHASE_SPEC, jev={"notices": True, "news": False})
    strat, conn, ledger = make(spec, notice=notice)
    strat.sell_step(quotes, at(10, 0))
    strat.buy_step(quotes, at(10, 0), deadline=clock[0] + 40.0)
    assert len(notice.calls) == 2                                 # 0→30、30→60；第三只时已过时限
    left = (set(quotes) - set(notice.calls)).pop()
    assert left not in strat.rejected
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE code=?", (left,)).fetchone()[0] == 0
    assert len(ledger.positions("c1_shadow")) == 1                # 影子不走 Jev，照买
    strat.sell_step(quotes, at(10, 1))
    strat.buy_step(quotes, at(10, 1), deadline=clock[0] + 40.0)
    assert notice.calls[2:] == [left]                             # 下一分钟接着问


def test_check_limit_resets_every_minute():
    """「每分钟最多检查 10 只」是每分钟：第一分钟问满 10 只，下一分钟接着问剩下的（不是当天只问 10 只）。"""
    codes = [f"6100{i:02d}" for i in range(15)]
    quotes = {c: quote(c, pct=6.0, amount=1e8 + i, ts=DAY) for i, c in enumerate(codes)}
    notice = Judge({c: ("reject", "x") for c in codes})
    spec = dict(PHASE_SPEC, jev={"notices": True, "news": False})
    strat, _, _ = make(spec, notice=notice)
    strat.universe = {c: f"股票{c}" for c in codes}          # 不改全局 UNIVERSE
    strat.on_minute(quotes, at(10, 0))
    assert len(notice.calls) == 10
    strat.on_minute(quotes, at(10, 1))
    assert len(notice.calls) == 15
    assert set(notice.calls) == set(codes)


def test_main_skips_unbuyable_candidates_before_jev():
    """前排是涨停/买不起一手的票：主账户不问 Jev、不占检查名额，越过它们买后面的（与影子一致）。"""
    spec = {
        "name": "越过买不进",
        "capital": 30000,
        "select": {"conditions": [{"field": "pct", "op": ">=", "value": 0}]},
        "rank": {"field": "pct", "order": "desc"},
        "entry": {},
        "exits": [{"when": {"conditions": [{"field": "pnl_pct", "op": "<=", "value": -5}]}, "reason": "止损"}],
    }
    notice = Judge()
    strat, conn, ledger = make(spec, notice=notice)
    quotes = {
        "600001": quote("600001", pct=10.0, ts=DAY),                    # 已涨停，买不进
        "600002": quote("600002", pct=5.0, pre_close=400.0, ts=DAY),    # 420 元一手 4.2 万，3 万买不起
        "600003": quote("600003", pct=1.0, ts=DAY),
    }

    fills = strat.on_minute(quotes, at(10, 0))

    assert notice.calls == ["600003"]
    assert strat.check_count == 1
    assert {(f.account, f.code) for f in fills} == {("c1_main", "600003"), ("c1_shadow", "600003")}
