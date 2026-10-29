import numpy as np
import pandas as pd
import pytest

from research.kronos_cross_sectional import (
    forecast_score,
    future_timestamps,
    history_context,
    select_test_samples,
)


def test_history_context_stops_at_signal_and_keeps_actual_ohlcv():
    bars = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=8, freq="D"),
        "code": ["000001"] * 8,
        "open": np.arange(1, 9),
        "high": np.arange(2, 10),
        "low": np.arange(0, 8),
        "close": np.arange(1.5, 9.5),
        "volume": np.arange(100, 900, 100),
    })
    context = history_context(bars, pd.Timestamp("2024-01-05"), lookback=3)
    assert context["date"].tolist() == list(pd.date_range("2024-01-03", periods=3, freq="D"))
    assert context["open"].tolist() == [3, 4, 5]
    assert (context["date"] <= pd.Timestamp("2024-01-05")).all()


def test_future_timestamps_uses_market_sessions_without_price_data():
    calendar = pd.DatetimeIndex(["2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12", "2024-01-15"])
    future = future_timestamps(pd.Timestamp("2024-01-05"), calendar)
    assert len(future) == 6
    assert future.iloc[0] == pd.Timestamp("2024-01-08")
    assert future.iloc[-1] == pd.Timestamp("2024-01-15")


def test_forecast_score_matches_entry_and_exit_open_contract():
    forecast = pd.DataFrame({"open": [10, 11, 12, 13, 14, 15]})
    score, entry, exit_ = forecast_score(forecast)
    assert score == pytest.approx(0.5)
    assert entry == 10
    assert exit_ == 15


def test_forecast_score_rejects_non_positive_prices():
    with pytest.raises(ValueError, match="有限正数"):
        forecast_score(pd.DataFrame({"open": [10, 11, 12, 13, 14, 0]}))


def test_smoke_selection_is_deterministic_inside_original_test_fold():
    dates = pd.bdate_range("2020-01-01", periods=800)
    samples = pd.DataFrame([
        {"signal_date": date, "code": f"{code:06d}"}
        for date in dates for code in range(1, 11)
    ])
    first = select_test_samples(samples, fold=1, smoke_days=2, smoke_names=6)
    second = select_test_samples(samples, fold=1, smoke_days=2, smoke_names=6)
    pd.testing.assert_frame_equal(first, second)
    assert first["signal_date"].nunique() == 2
    assert first.groupby("signal_date").size().tolist() == [6, 6]
    full = select_test_samples(samples, fold=1)
    assert set(zip(first["signal_date"], first["code"])).issubset(set(zip(full["signal_date"], full["code"])))


def _batch_fixture():
    from research.kronos_cross_sectional import KronosConfig, prepare_histories

    dates = pd.bdate_range("2024-01-01", periods=12)
    stock_rows = []
    test_rows = []
    for number in range(8):
        code = f"{number:06d}"
        history_dates = dates[1:4] if number < 4 else dates[0:4]
        for offset, date in enumerate(history_dates):
            stock_rows.append({
                "date": date, "code": code, "open": 10 + offset,
                "high": 11 + offset, "low": 9 + offset,
                "close": 10.5 + offset, "volume": 1000 + offset,
            })
        for date in dates[2:4]:
            test_rows.append({"code": code, "signal_date": date})
    stocks = pd.DataFrame(stock_rows).sample(frac=1, random_state=7).reset_index(drop=True)
    test = pd.DataFrame(test_rows).sort_values(["signal_date", "code"]).reset_index(drop=True)
    return test, prepare_histories(stocks), dates, KronosConfig(lookback=4, batch_size=8)


def test_prediction_batches_pack_across_dates_without_mixing_lengths_or_lookahead():
    from research.kronos_cross_sectional import _prediction_batches

    test, histories, calendar, config = _batch_fixture()
    batches = list(_prediction_batches(test, histories, calendar, config))
    assert [len(batch) for batch in batches] == [4, 8, 4]
    keys = []
    for batch in batches:
        lengths = set()
        for index, frame, x_stamp, y_stamp in batch:
            row = test.loc[index]
            signal_date = pd.Timestamp(row["signal_date"])
            keys.append((row["code"], str(signal_date.date())))
            lengths.add(len(frame))
            assert len(x_stamp) == len(frame)
            assert x_stamp.iloc[-1] == signal_date
            assert y_stamp.iloc[0] > signal_date
        assert len(lengths) == 1
    assert len(keys) == len(set(keys)) == len(test)


def test_prediction_batches_keep_resume_skip_and_unique_keys():
    from research.kronos_cross_sectional import _prediction_batches

    test, histories, calendar, config = _batch_fixture()
    skipped = {("000000", str(calendar[2].date()))}
    batches = list(_prediction_batches(test, histories, calendar, config, skipped))
    keys = [
        (test.loc[index, "code"], str(pd.Timestamp(test.loc[index, "signal_date"]).date()))
        for batch in batches for index, *_ in batch
    ]
    assert skipped.isdisjoint(keys)
    assert len(keys) == len(set(keys)) == len(test) - 1
