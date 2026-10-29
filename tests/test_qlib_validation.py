import ast
from pathlib import Path

import pandas as pd
import pytest

from research import qlib_validation as qv


def bars(opens, closes=None):
    dates = pd.date_range("2024-01-01", periods=len(opens), freq="D")
    if closes is None:
        closes = opens
    return pd.DataFrame({
        "date": dates,
        "open": opens,
        "high": closes,
        "low": opens,
        "close": closes,
        "volume": 1000,
    })


def test_close_signal_only_becomes_position_next_session():
    frame = bars([10, 10, 11, 12, 13], [10, 10, 11, 12, 13])
    positions = qv.momentum_positions(frame, lookback=1)
    assert positions.tolist() == [0.0, 0.0, 0.0, 1.0, 1.0]


def test_turnover_costs_are_deducted_on_position_changes():
    frame = bars(
        [10, 10, 11, 12, 12, 11, 10],
        [10, 10, 11, 12, 10, 9, 8],
    )
    result = qv.simulate_symbol(frame, lookback=1, buy_cost=0.001, sell_cost=0.002)
    assert result["position"].tolist() == [0.0, 0.0, 0.0, 1.0, 1.0, 0.0]
    assert result["cost"].tolist() == pytest.approx([0.0, 0.0, 0.0, 0.001, 0.0, 0.002])
    assert result["net_return"].iloc[3] == pytest.approx(-0.001)
    assert result["net_return"].iloc[4] == pytest.approx(11 / 12 - 1)
    assert result["net_return"].iloc[5] == pytest.approx(-0.002)


def test_training_and_validation_are_ordered_and_do_not_overlap():
    stock = bars(
        [10 + index * 0.2 for index in range(100)],
        [10 + index * 0.2 + (0.1 if index % 5 == 0 else 0) for index in range(100)],
    )
    stock["code"] = "000001"
    benchmark = bars([100 + index for index in range(100)])
    summary, daily = qv.run_backtest(stock, benchmark, split="2024-03-01", lookbacks=(1, 5))
    train_end = pd.Timestamp(summary["split"]["train_return_dates"][1])
    validation_start = pd.Timestamp(summary["split"]["validation_return_dates"][0])
    assert train_end < pd.Timestamp("2024-03-01")
    assert validation_start >= pd.Timestamp("2024-03-01")
    assert train_end < validation_start
    assert daily["date"].min() == validation_start
    assert summary["selected_lookback"] in (1, 5)


def test_empty_and_single_return_metrics_do_not_invent_risk_values():
    with pytest.raises(ValueError, match="为空"):
        qv.qlib_risk_metrics(pd.Series(dtype=float))
    metrics = qv.qlib_risk_metrics(pd.Series([0.01]))
    assert metrics["observations"] == 1
    assert metrics["total_return"] == pytest.approx(0.01)
    assert metrics["information_ratio"] is None
    assert metrics["max_drawdown"] is None or metrics["max_drawdown"] <= 0


def test_research_module_does_not_import_application_or_trading_code():
    path = Path(qv.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.split(".")[0] in {"jevq", "dashboard"}}
