import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from research import ifind_backfill as ib


def universe_row(code, market, list_date="2020-01-01", delist_date=""):
    return {
        "STK_CODE": code,
        "STK_UNI_CODE": f"id-{code}-{list_date}",
        "SEC_MAR_PAR": market,
        "LIST_DATE": list_date,
        "DELIST_DATE": delist_date,
    }


def price_rows(tickers, date="20220104"):
    return pd.DataFrame({
        "open": [10.0] * len(tickers),
        "high": [11.0] * len(tickers),
        "low": [9.0] * len(tickers),
        "close": [10.5] * len(tickers),
        "volume": [1000] * len(tickers),
        "thscode": list(tickers),
        "time": [date] * len(tickers),
    })


def test_load_tickers_filters_inactive_and_deduplicates_code(tmp_path):
    path = tmp_path / "universe.csv"
    pd.DataFrame([
        universe_row("600018", "上海证券交易所", "2006-10-26"),
        universe_row("600018", "上海证券交易所", "2000-07-19", "2006-10-20"),
        universe_row("000001", "深圳证券交易所"),
        universe_row("920002", "北京证券交易所", "2024-05-30"),
        universe_row("300001", "深圳证券交易所", "2027-01-01"),
    ]).to_csv(path, index=False)
    assert ib.load_tickers(path, "2021-06-01", "2026-10-27") == [
        "000001.SZ", "600018.SH", "920002.BJ",
    ]


def test_validate_prices_rejects_future_or_duplicate_rows():
    window = ib.Window("test", "2022-01-01", "2022-12-31")
    with pytest.raises(ValueError, match="超出请求时间窗口"):
        ib.validate_prices(price_rows(["000001.SZ"], "20230101"), ["000001.SZ"], window)
    duplicate = pd.concat([price_rows(["000001.SZ"])] * 2, ignore_index=True)
    with pytest.raises(ValueError, match="重复行情"):
        ib.validate_prices(duplicate, ["000001.SZ"], window)


def test_execute_chunks_resumes_and_consolidates(tmp_path):
    tickers = ["000001.SZ", "600000.SH", "600018.SH", "920002.BJ"]
    helper = tmp_path / "ifind.py"
    helper.touch()
    calls = []

    def fake_call(command):
        calls.append(command)
        params = json.loads(command[command.index("--params-json") + 1])
        output = Path(params["file_path"])
        requested = params["ticker"].split(",")
        price_rows(requested).to_csv(output, index=False)
        return subprocess.CompletedProcess(command, 0, stdout="OK", stderr="")

    window = ib.Window("test", "2022-01-01", "2022-12-31")
    runner = ib.IfindBackfillRunner(
        tickers, tmp_path / "out", helper=helper, caller=fake_call, windows=[window],
    )
    first = runner.execute(max_requests=1)
    assert first["saved_requests"] == 1
    assert first["missing_requests"] == 1
    assert len(calls) == 1
    second = runner.execute(max_requests=1)
    assert second["status"] == "complete"
    assert len(calls) == 2
    runner.execute(max_requests=0)
    assert len(calls) == 2
    output = runner.consolidate()
    result = pd.read_csv(output, dtype={"code": str})
    assert len(result) == 4
    assert set(result["ticker"]) == set(tickers)
    assert result["code"].str.len().eq(6).all()


def test_empty_data_is_terminal_and_does_not_retry(tmp_path):
    helper = tmp_path / "ifind.py"
    helper.touch()
    calls = []

    def empty_call(command):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="EMPTY_DATA - 查询无数据返回", stderr="")

    window = ib.Window("test", "2022-01-01", "2022-12-31")
    runner = ib.IfindBackfillRunner(
        ["000001.SZ"], tmp_path / "out", helper=helper,
        caller=empty_call, windows=[window],
    )
    manifest = runner.execute(max_requests=1)
    assert manifest["status"] == "complete"
    assert manifest["returned_rows"] == 0
    runner.execute(max_requests=0)
    assert len(calls) == 1
    ledger = (tmp_path / "out/request_ledger.jsonl").read_text(encoding="utf-8")
    assert '"status": "empty"' in ledger


def test_execute_does_not_replace_saved_batch_with_invalid_response(tmp_path):
    helper = tmp_path / "ifind.py"
    helper.touch()

    def bad_call(command):
        params = json.loads(command[command.index("--params-json") + 1])
        price_rows(params["ticker"].split(","), "20230101").to_csv(params["file_path"], index=False)
        return subprocess.CompletedProcess(command, 0, stdout="OK", stderr="")

    window = ib.Window("test", "2022-01-01", "2022-12-31")
    runner = ib.IfindBackfillRunner(
        ["000001.SZ"], tmp_path / "out", helper=helper,
        caller=bad_call, windows=[window],
    )
    with pytest.raises(ValueError, match="超出请求时间窗口"):
        runner.execute(max_requests=1)
    assert not (tmp_path / "out/batches/test/batch-00001.csv").exists()
    assert runner.prepare()["missing_requests"] == 1
