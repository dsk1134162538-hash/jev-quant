import pandas as pd
import pytest

from research import akshare_backfill as ab


def prices(code="000001", dates=None):
    dates = dates or ["2021-06-01", "2021-06-02"]
    count = len(dates)
    return pd.DataFrame({
        "date": dates, "code": [code] * count,
        "open": [10.0] * count, "high": [11.0] * count,
        "low": [9.0] * count, "close": [10.5] * count,
        "volume": [1000] * count,
    })


def test_validate_daily_rejects_wrong_code_and_duplicate_date():
    with pytest.raises(ValueError, match="混入其他代码"):
        ab.validate_daily(prices("600000"), "000001")
    duplicate = prices(dates=["2021-06-01", "2021-06-01"])
    with pytest.raises(ValueError, match="日期无效或重复"):
        ab.validate_daily(duplicate, "000001")


def test_only_zero_volume_nonpositive_price_placeholders_are_dropped():
    frame = prices(dates=["2021-06-01", "2021-06-02", "2021-06-03"])
    frame.loc[1, ["open", "high", "low", "volume"]] = 0
    frame.loc[2, "volume"] = 0
    result = ab._drop_nontrading_placeholders(frame)
    assert result["date"].tolist() == ["2021-06-01", "2021-06-03"]
    assert result.attrs["nontrading_placeholder_rows_dropped"] == 1
    ab.validate_daily(result, "000001")


def test_resume_empty_and_consolidate(tmp_path):
    calls = []

    def fetch(code, start, end):
        calls.append(code)
        if code == "600000":
            return pd.DataFrame(columns=ab.COLUMNS)
        return prices(code)

    runner = ab.AkshareBackfillRunner(
        ["000001", "600000", "920002"], tmp_path / "out", fetcher=fetch,
    )
    first = runner.execute(max_requests=2)
    assert first["saved_codes"] == 2
    assert first["missing_codes"] == 1
    assert first["empty_codes"] == 1
    second = runner.execute(max_requests=1)
    assert second["status"] == "complete"
    assert calls == ["000001", "600000", "920002"]
    runner.execute(max_requests=0)
    assert len(calls) == 3
    output = runner.consolidate()
    result = pd.read_csv(output, dtype={"code": str})
    assert set(result["code"]) == {"000001", "920002"}
    assert len(result) == 4


def test_invalid_response_does_not_create_saved_file(tmp_path):
    def bad_fetch(code, start, end):
        return prices(code, ["2020-01-01"])

    runner = ab.AkshareBackfillRunner(["000001"], tmp_path / "out", fetcher=bad_fetch)
    with pytest.raises(ValueError, match="超出请求区间"):
        runner.execute(max_requests=1)
    assert not (tmp_path / "out/daily/000001.csv").exists()
    assert runner.prepare()["missing_codes"] == 1


def test_parallel_execution_keeps_valid_ledger(tmp_path):
    def fetch(code, start, end):
        return prices(code)

    runner = ab.AkshareBackfillRunner(
        ["000001", "600000", "920002"], tmp_path / "out", fetcher=fetch,
    )
    manifest = runner.execute(max_requests=3, workers=2)
    assert manifest["new_requests"] == 3
    lines = (tmp_path / "out/request_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert all('"status": "completed"' in line for line in lines)
