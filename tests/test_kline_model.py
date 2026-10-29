import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research import kline_model as km


def make_bars(rows=100, code="000001"):
    dates = pd.bdate_range("2024-01-01", periods=rows)
    close = 10 + np.arange(rows) * 0.03 + np.sin(np.arange(rows) / 4) * 0.15
    return pd.DataFrame({
        "date": dates, "code": code, "open": close * 0.998,
        "high": close * 1.015, "low": close * 0.985, "close": close,
        "volume": 1_000_000 + np.arange(rows) * 100,
    })


def test_features_at_t_do_not_change_when_future_bars_change():
    original = make_bars()
    cutoff = 79
    changed = original.copy()
    changed.loc[cutoff + 1:, ["open", "high", "low", "close", "volume"]] *= 4
    first = km._stock_features(original).loc[cutoff, km.FEATURE_COLUMNS[:-9]]
    second = km._stock_features(changed).loc[cutoff, km.FEATURE_COLUMNS[:-9]]
    pd.testing.assert_series_equal(first, second)


def test_label_uses_next_two_opens_and_round_trip_cost():
    bars = make_bars()
    result = km._stock_features(bars)
    row = result.iloc[65]
    expected = bars["open"].iloc[67] / bars["open"].iloc[66] - 1 - km.ROUND_TRIP_COST
    assert row["entry_date"] == bars["date"].iloc[66]
    assert row["exit_date"] == bars["date"].iloc[67]
    assert row["target_net_return"] == pytest.approx(expected)


def test_period_split_uses_entry_date_without_overlap():
    bars = make_bars(rows=150)
    benchmark = make_bars(rows=150).drop(columns="code")
    samples = km.build_samples(bars, benchmark)
    samples["signal_date"] = samples["date"]
    train = km._period(samples, None, "2024-04-01")
    validation = km._period(samples, "2024-04-01", None)
    assert train["entry_date"].max() < validation["entry_date"].min()
    assert set(train.index).isdisjoint(set(validation.index))


def test_backtest_selects_at_most_top_k_and_keeps_cash_days():
    bars = make_bars(rows=150)
    benchmark = make_bars(rows=150).drop(columns="code")
    samples = km.build_samples(bars, benchmark)
    samples["signal_date"] = samples["date"]
    frame = km._period(samples, "2024-04-01", None).copy()
    frame["score"] = np.linspace(0, 1, len(frame))
    calendar = pd.DatetimeIndex(sorted(frame["entry_date"].unique()))
    metrics, daily, selected = km.evaluate_period(
        frame, calendar, km._benchmark_returns(benchmark), quantile=0.0)
    assert selected.groupby("signal_date").size().max() <= km.TOP_K
    assert len(daily) == len(calendar)
    assert metrics["model"]["trades"] == len(selected)
    assert metrics["model"]["total_return"] == pytest.approx((1 + daily["model_net"]).prod() - 1)
    assert metrics["model"]["daily_mean_t_stat"] == pytest.approx(
        daily["model_net"].mean() / (daily["model_net"].std(ddof=1) / np.sqrt(len(daily))))


def test_backtest_uses_fixed_validation_score_cutoff():
    frame = pd.DataFrame({
        "signal_date": pd.to_datetime(["2026-01-02", "2026-01-02", "2026-01-05"]),
        "entry_date": pd.to_datetime(["2026-01-05", "2026-01-05", "2026-01-06"]),
        "code": ["000001", "000002", "000001"],
        "score": [0.70, 0.60, 0.95],
        "target_net_return": [0.01, 0.02, 0.03],
    })
    selected = km._selected_rows(frame, quantile=0.90, score_cutoff=0.65)
    assert selected["code"].tolist() == ["000001", "000001"]
    # 若在测试区间重算 90% 分位，0.70 会被未来更高的 0.95 排除。
    assert 0.60 not in selected["score"].tolist()


def test_quality_uses_positional_scores_with_nonzero_frame_index():
    actual = np.linspace(-0.02, 0.03, 100)
    score = np.linspace(0.0, 1.0, 100)
    frame = pd.DataFrame({"target_net_return": actual}, index=np.arange(100) + 500)
    quality = km._quality(frame, score)
    expected = pd.Series(actual).corr(pd.Series(score), method="spearman")
    assert quality["spearman_ic"] == pytest.approx(expected)
    assert quality["top_decile_average_net_return"] == pytest.approx(actual[-10:].mean())


def test_research_model_does_not_import_application_or_trading_code():
    tree = ast.parse(Path(km.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.split(".")[0] in {"jevq", "dashboard"}}
