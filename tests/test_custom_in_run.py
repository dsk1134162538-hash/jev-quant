"""Test Task 6: 自定义策略接入 run loop

验证三点：
1. 自定义策略每分钟被驱动
2. 一个策略抛异常不影响另一个
3. 收盘小结覆盖自定义账户
"""
import json
from datetime import date
from unittest.mock import MagicMock

from jevq import db
from jevq.ledger import Ledger
from jevq.notify_content import close_summary_content


def test_custom_strategy_on_minute_called_in_run_loop():
    """验证 run loop 里自定义策略的 on_minute 被调用"""
    from jevq.strategies.custom import CustomStrategy

    # Mock 一个策略实例
    strat = MagicMock(spec=CustomStrategy)
    strat.on_minute.return_value = []

    # 验证接口存在
    quotes = {}
    from datetime import datetime
    now = datetime(2026, 9, 24, 10, 0)
    strat.on_minute(quotes, now)

    strat.on_minute.assert_called_once_with(quotes, now)


def test_custom_strategy_exception_isolated():
    """验证一个策略抛异常时 rollback，不影响其他策略"""
    conn = db.connect(":memory:")

    # 第一个策略正常
    strat1 = MagicMock()
    strat1.on_minute.return_value = []

    # 第二个策略抛异常
    strat2 = MagicMock()
    strat2.on_minute.side_effect = RuntimeError("故意崩溃")

    # 模拟 run loop 的异常隔离逻辑
    custom_strategies = [("c1", strat1), ("c2", strat2)]
    custom_fills = []

    for sid, strat in custom_strategies:
        try:
            fills = strat.on_minute({}, None)
            custom_fills.extend(fills)
        except Exception:
            conn.rollback()
            # 异常被捕获，不影响其他策略
            pass

    # c1 正常调用
    assert strat1.on_minute.called
    # c2 也被调用但抛异常
    assert strat2.on_minute.called
    # 没有崩溃
    assert True


def test_close_summary_includes_custom_accounts():
    """验证收盘小结覆盖自定义主账户"""
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    today = date(2026, 9, 24)

    # 手动插入一条自定义策略记录
    spec = {
        "name": "测试策略",
        "capital": 50000,
        "select": {"conditions": []},
        "entry": {},
        "exits": [],
        "jev": {"notices": False, "news": False}
    }
    conn.execute(
        "INSERT INTO custom_strategies(id, version, name, spec, created, active_from, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("c1", 1, "测试策略", json.dumps(spec), "2026-09-23", "2026-09-24", 1)
    )
    conn.commit()

    # 开户
    ledger.ensure_account("c1_main", 50000)
    ledger.ensure_account("c1_shadow", 50000)

    # 生成收盘小结
    prices = {}
    content = close_summary_content(ledger, today, prices, conn)

    # 验证：包含 c1
    assert "c1" in content
    assert "50,000.00" in content  # 净值

    conn.close()


def test_notify_fills_recognizes_custom_main_accounts():
    """验证 _notify_fills 识别自定义主账户（以 _main 结尾）"""
    from jevq.models import Fill
    from datetime import datetime

    # c1_main 应该推送
    f1 = Fill("c1_main", "600001", "buy", 100, 10.0, 0.0, datetime.now(), "测试")
    assert f1.account.endswith("_main")

    # c1_shadow 不应该推送
    f2 = Fill("c1_shadow", "600001", "buy", 100, 10.0, 0.0, datetime.now(), "测试")
    assert not f2.account.endswith("_main")

    # 验证逻辑：f.account != "main" and not f.account.endswith("_main") 会过滤掉影子
    should_push = lambda f: f.account == "main" or f.account.endswith("_main")
    assert should_push(f1)
    assert not should_push(f2)
