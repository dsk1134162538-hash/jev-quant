import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research import cross_sectional_model as cm


def make_bars(rows=120, code="000001", offset=0.0):
    dates = pd.bdate_range("2024-01-01", periods=rows)
    close = 10 + offset + np.arange(rows) * 0.03 + np.sin(np.arange(rows) / 4) * 0.15
    return pd.DataFrame({
        "date": dates,
        "code": code,
        "open": close * 0.998,
        "high": close * 1.015,
        "low": close * 0.985,
        "close": close,
        "volume": 1_000_000 + np.arange(rows) * 100,
    })


def make_market(rows=120, codes=8):
    stocks = pd.concat(
        [make_bars(rows, f"{index:06d}", index * 0.05) for index in range(codes)],
        ignore_index=True,
    )
    benchmark = make_bars(rows).drop(columns="code")
    return stocks, benchmark


def test_five_day_label_uses_next_open_and_sixth_open():
    bars = make_bars()
    result = cm._forward_labels(bars)
    row = result.iloc[65]
    assert row["entry_date"] == bars["date"].iloc[66]
    assert row["exit_date"] == bars["date"].iloc[71]
    assert row["gross_return"] == pytest.approx(
        bars["open"].iloc[71] / bars["open"].iloc[66] - 1.0
    )


def test_features_at_t_do_not_change_when_future_bars_change():
    stocks, benchmark = make_market()
    changed = stocks.copy()
    future = changed["date"] > pd.Timestamp("2024-03-22")
    changed.loc[future, ["open", "high", "low", "close", "volume"]] *= 3
    first = cm.build_samples(stocks, benchmark)
    second = cm.build_samples(changed, benchmark)
    first = first[first["signal_date"] == pd.Timestamp("2024-03-22")].sort_values("code")
    second = second[second["signal_date"] == pd.Timestamp("2024-03-22")].sort_values("code")
    pd.testing.assert_frame_equal(
        first[["code", *cm.FEATURE_COLUMNS]].reset_index(drop=True),
        second[["code", *cm.FEATURE_COLUMNS]].reset_index(drop=True),
    )


def test_target_is_cross_sectional_rank_not_absolute_return():
    stocks, benchmark = make_market()
    samples = cm.build_samples(stocks, benchmark)
    _, group = next(iter(samples.groupby("signal_date")))
    expected = group["excess_return"].rank(method="average", pct=True)
    pd.testing.assert_series_equal(group["target_rank"], expected, check_names=False)
    assert group["target_rank"].between(0, 1).all()
    assert (group["raw_universe_size"] == 8).all()


def test_walk_forward_tests_do_not_overlap_and_labels_are_purged():
    stocks, benchmark = make_market(rows=360)
    samples = cm.build_samples(stocks, benchmark)
    config = cm.WalkForwardConfig(
        train_days=100, validation_days=30, test_days=30,
        step_days=30, embargo_days=6,
    )
    splits = cm.make_walk_forward_splits(samples["signal_date"].unique(), config)
    assert len(splits) > 2
    previous_test_dates = set()
    for split in splits:
        frames = cm.purged_fold_samples(samples, split)
        assert frames["train"]["exit_date"].max() < frames["validation"]["entry_date"].min()
        assert frames["validation"]["exit_date"].max() < frames["test"]["entry_date"].min()
        test_dates = set(frames["test"]["signal_date"])
        assert test_dates.isdisjoint(previous_test_dates)
        previous_test_dates.update(test_dates)


def test_rank_ic_is_average_of_daily_cross_sections_not_pooled():
    frame = pd.DataFrame({
        "signal_date": pd.to_datetime(["2026-01-05"] * 5 + ["2026-01-06"] * 5),
        "entry_date": pd.to_datetime(["2026-01-06"] * 5 + ["2026-01-07"] * 5),
        "exit_date": pd.to_datetime(["2026-01-13"] * 5 + ["2026-01-14"] * 5),
        "code": list("ABCDE") * 2,
        "score": [1, 2, 3, 4, 5] * 2,
        "gross_return": [0.01] * 10,
        "excess_return": [1, 2, 3, 4, 5] + [5, 4, 3, 2, 1],
        "benchmark_return": [0.0] * 10,
        "raw_universe_size": [5] * 10,
    })
    metrics, artifacts = cm.evaluate_scores(frame)
    assert artifacts["daily_ic"]["rank_ic"].tolist() == pytest.approx([1.0, -1.0])
    assert metrics["rank_ic"]["mean"] == pytest.approx(0.0)
    assert metrics["rank_ic"]["days"] == 2
    assert metrics["coverage"]["ratio"] == pytest.approx(1.0)


def test_turnover_costs_are_charged_on_nonoverlapping_top_portfolio():
    dates = pd.to_datetime(["2026-01-05"] * 5 + ["2026-01-12"] * 5)
    frame = pd.DataFrame({
        "signal_date": dates,
        "entry_date": dates + pd.Timedelta(days=1),
        "exit_date": dates + pd.Timedelta(days=7),
        "code": list("ABCDE") * 2,
        "score": [1, 2, 3, 4, 5, 5, 4, 3, 2, 1],
        "gross_return": [0.01] * 10,
        "excess_return": [0.01] * 10,
        "benchmark_return": [0.0] * 10,
        "raw_universe_size": [5] * 10,
    })
    config = cm.EvaluationConfig(
        quantiles=5, rebalance_every=1, buy_cost=0.0005, sell_cost=0.0015
    )
    metrics, artifacts = cm.evaluate_scores(frame, config)
    portfolio = artifacts["portfolio"]
    assert portfolio["turnover"].tolist() == pytest.approx([1.0, 1.0])
    assert portfolio["net_return"].tolist() == pytest.approx([0.008, 0.008])
    assert metrics["top_quantile_nonoverlapping"]["total_net_return"] == pytest.approx(
        1.008 ** 2 - 1
    )
    assert metrics["quantiles"]["executable_long_short_assumed"] is False


def test_non_pit_data_requires_explicit_opt_in(tmp_path):
    with pytest.raises(ValueError, match="非 PIT"):
        cm.run(
            Path("stocks.csv"), Path("benchmark.csv"), Path("universe.csv"),
            tmp_path, allow_non_pit_sample=False,
        )


def test_research_model_does_not_import_application_or_trading_code():
    tree = ast.parse(Path(cm.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.split(".")[0] in {"jevq", "dashboard"}}


def test_labels_follow_market_calendar_and_drop_suspended_execution_days():
    bars = make_bars()
    calendar = bars["date"].copy()
    suspended_entry = calendar.iloc[66]
    bars = bars.drop(index=66).reset_index(drop=True)
    result = cm._forward_labels(bars, calendar)
    row = result.loc[result["date"] == calendar.iloc[65]].iloc[0]
    assert row["entry_date"] == suspended_entry
    assert row["exit_date"] == calendar.iloc[71]
    assert pd.isna(row["gross_return"])


def test_ipo_with_incomplete_long_features_remains_in_cross_section():
    stocks, benchmark = make_market()
    stocks = stocks[~((stocks["code"] == "000000") & (stocks.index < 70))].copy()
    samples = cm.build_samples(stocks, benchmark)
    ipo = samples[
        (samples["code"] == "000000")
        & (samples["signal_date"] == benchmark["date"].iloc[70])
    ]
    assert len(ipo) == 1
    assert not ipo["feature_complete"].iloc[0]
    assert pd.isna(ipo["ma_gap_60"].iloc[0])
    assert ipo["raw_universe_size"].iloc[0] == 8


def test_relisting_identity_does_not_inherit_old_listing_features():
    old = make_bars(rows=70).iloc[:60].copy()
    old["STK_UNI_CODE"] = "old"
    new = make_bars(rows=120).iloc[80:].copy()
    new["STK_UNI_CODE"] = "new"
    others = []
    for index in range(5):
        item = make_bars(rows=120, code=f"10000{index}", offset=index * 0.05)
        item["STK_UNI_CODE"] = f"active-{index}"
        others.append(item)
    stocks = pd.concat([old, new, *others], ignore_index=True)
    benchmark = make_bars(rows=120).drop(columns="code")
    samples = cm.build_samples(stocks, benchmark)
    first_new = samples[
        (samples["code"] == "000001") & (samples["signal_date"] == new["date"].iloc[0])
    ]
    assert len(first_new) == 1
    assert pd.isna(first_new["ret_1"].iloc[0])
    assert first_new["gross_return"].notna().iloc[0]
