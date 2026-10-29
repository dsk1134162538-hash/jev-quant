"""使用 AKShare／新浪逐股续传全 A 前复权日线，独立于 iFinD 数据。"""

from __future__ import annotations

import argparse
import ast
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Callable, Sequence

import pandas as pd

from research.ifind_backfill import DEFAULT_UNIVERSE, load_tickers

DEFAULT_OUTPUT = Path("research/artifacts/akshare-daily-backfill")
START = "2021-06-01"
END = "2026-10-27"
COLUMNS = ["date", "code", "open", "high", "low", "close", "volume"]
Fetch = Callable[[str, str, str], pd.DataFrame]


def fetch_daily(code: str, start: str, end: str) -> pd.DataFrame:
    import py_mini_racer
    import requests
    from akshare.stock.cons import (
        hk_js_decode, zh_sina_a_stock_hist_url, zh_sina_a_stock_qfq_url,
    )

    if code.startswith(("4", "8", "920")):
        symbol = "bj" + code
    else:
        symbol = ("sh" if code.startswith(("5", "6", "9")) else "sz") + code
    response = requests.get(zh_sina_a_stock_hist_url.format(symbol), timeout=30)
    response.raise_for_status()
    js_code = py_mini_racer.MiniRacer()
    js_code.eval(hk_js_decode)
    rows = js_code.call("d", response.text.split("=")[1].split(";")[0].replace('"', ""))
    bars = pd.DataFrame(rows)
    if bars.empty:
        return pd.DataFrame(columns=COLUMNS)
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce")
    bars = bars.dropna(subset=["date"]).set_index("date")
    bars = bars[["open", "high", "low", "close", "volume"]].astype(float)

    factor_response = requests.get(zh_sina_a_stock_qfq_url.format(symbol), timeout=30)
    factor_response.raise_for_status()
    factor_payload = ast.literal_eval(factor_response.text.split("=")[1].split("\n")[0])
    factors = pd.DataFrame(factor_payload["data"])
    if factors.empty:
        raise ValueError(f"{code} 前复权因子为空，不能擅自按不复权处理")
    factors.columns = ["date", "qfq_factor"]
    factors["date"] = pd.to_datetime(factors["date"], errors="coerce")
    factors = factors.dropna(subset=["date"]).set_index("date")
    bars.index = bars.index.tz_localize(None)
    factors.index = factors.index.tz_localize(None)
    merged = (
        bars.merge(factors, left_index=True, right_index=True, how="outer")
        .ffill().dropna().astype(float)
    )
    for column in ("open", "high", "low", "close"):
        merged[column] = (merged[column] / merged["qfq_factor"]).round(2)
    merged = _drop_nontrading_placeholders(merged)
    merged = merged[
        (merged.index >= pd.Timestamp(start)) & (merged.index <= pd.Timestamp(end))
    ]
    result = merged.reset_index()[["date", "open", "high", "low", "close", "volume"]]
    result["code"] = code
    return result[COLUMNS]


def _drop_nontrading_placeholders(frame: pd.DataFrame) -> pd.DataFrame:
    """仅剔除零成交量且任一 OHLC 非正的停牌占位，其他异常仍由校验拒绝。"""
    invalid_price = frame[["open", "high", "low", "close"]].le(0).any(axis=1)
    placeholder = frame["volume"].eq(0) & invalid_price
    result = frame.loc[~placeholder].copy()
    result.attrs["nontrading_placeholder_rows_dropped"] = int(placeholder.sum())
    return result


def validate_daily(frame: pd.DataFrame, code: str, start: str = START, end: str = END) -> pd.DataFrame:
    if set(frame.columns) != set(COLUMNS):
        raise ValueError(f"{code} 行情字段不符合契约：{frame.columns.tolist()}")
    if frame.empty:
        return frame.copy()
    result = frame.copy()
    result["code"] = result["code"].astype(str).str.zfill(6)
    if set(result["code"]) != {code}:
        raise ValueError(f"{code} 行情混入其他代码")
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    if result["date"].isna().any() or result["date"].duplicated().any():
        raise ValueError(f"{code} 日期无效或重复")
    if not result["date"].between(pd.Timestamp(start), pd.Timestamp(end)).all():
        raise ValueError(f"{code} 行情超出请求区间")
    for column in ("open", "high", "low", "close", "volume"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    if result[["open", "high", "low", "close", "volume"]].isna().any().any():
        raise ValueError(f"{code} 价格或成交量存在非数值")
    if (result[["open", "high", "low", "close"]] <= 0).any().any() or (result["volume"] < 0).any():
        raise ValueError(f"{code} 价格必须为正且成交量不能为负")
    if (result["high"] < result[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError(f"{code} 最高价低于其他价格")
    if (result["low"] > result[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError(f"{code} 最低价高于其他价格")
    return result.sort_values("date").reset_index(drop=True)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class AkshareBackfillRunner:
    def __init__(
        self,
        codes: Sequence[str],
        output_dir: Path,
        fetcher: Fetch = fetch_daily,
        start: str = START,
        end: str = END,
    ) -> None:
        self.codes = list(codes)
        self.output_dir = output_dir
        self.fetcher = fetcher
        self.start = start
        self.end = end

    def _path(self, code: str) -> Path:
        return self.output_dir / "daily" / f"{code}.csv"

    def _valid_saved(self, code: str) -> bool:
        path = self._path(code)
        if not path.is_file():
            return False
        try:
            validate_daily(pd.read_csv(path, dtype={"code": str}), code, self.start, self.end)
        except (OSError, ValueError, pd.errors.ParserError):
            return False
        return True

    def prepare(self) -> dict:
        missing = [code for code in self.codes if not self._valid_saved(code)]
        rows = 0
        empty = 0
        for code in self.codes:
            if code not in missing:
                length = len(pd.read_csv(self._path(code)))
                rows += length
                empty += int(length == 0)
        manifest = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "complete" if not missing else "awaiting_download",
            "source": "AKShare Sina daily + qfq factor (OHLCV only)",
            "adjust": "qfq",
            "start": self.start,
            "end": self.end,
            "codes": len(self.codes),
            "saved_codes": len(self.codes) - len(missing),
            "missing_codes": len(missing),
            "empty_codes": empty,
            "returned_rows": rows,
            "missing_code_list": missing,
        }
        _atomic_json(self.output_dir / "manifest.json", manifest)
        return manifest

    def execute(self, max_requests: int, workers: int = 1) -> dict:
        if max_requests < 0 or not 1 <= workers <= 4:
            raise ValueError("请求上限不能为负，并发数必须在 1—4 之间")
        if workers != 1 and self.fetcher is fetch_daily:
            raise ValueError("新浪 JS 解码器不是线程安全的，真实下载必须使用单线程")
        pending = [code for code in self.codes if not self._valid_saved(code)]
        if pending and max_requests == 0:
            raise RuntimeError(f"仍有 {len(pending)} 次请求，但本轮上限为 0")
        ledger_lock = Lock()
        completed = 0

        def download(code: str) -> None:
            target = self._path(code)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".csv.tmp")
            attempt = {
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "code": code, "status": "failed",
            }
            try:
                frame = self.fetcher(code, self.start, self.end)
                validated = validate_daily(frame, code, self.start, self.end)
                validated.to_csv(temporary, index=False)
                os.replace(temporary, target)
                attempt["status"] = "empty" if validated.empty else "completed"
                attempt["rows"] = len(validated)
                attempt["nontrading_placeholder_rows_dropped"] = int(
                    validated.attrs.get("nontrading_placeholder_rows_dropped", 0)
                )
            except Exception as exc:
                attempt["error"] = f"{type(exc).__name__}: {exc}"[-1000:]
                raise
            finally:
                attempt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
                ledger = self.output_dir / "request_ledger.jsonl"
                ledger.parent.mkdir(parents=True, exist_ok=True)
                with ledger_lock:
                    with ledger.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(attempt, ensure_ascii=False, allow_nan=False) + "\n")
                temporary.unlink(missing_ok=True)

        selected = pending[:max_requests]
        if workers == 1:
            for code in selected:
                download(code)
                completed += 1
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(download, code) for code in selected]
                for future in futures:
                    future.result()
                    completed += 1
        manifest = self.prepare()
        manifest["new_requests"] = completed
        _atomic_json(self.output_dir / "manifest.json", manifest)
        return manifest

    def consolidate(self) -> Path:
        manifest = self.prepare()
        if manifest["missing_codes"]:
            raise RuntimeError(f"仍有 {manifest['missing_codes']} 只证券未完成")
        target = self.output_dir / "stock_daily.csv"
        temporary = target.with_suffix(".csv.tmp")
        first = True
        coverage = []
        for code in self.codes:
            frame = validate_daily(
                pd.read_csv(self._path(code), dtype={"code": str}), code, self.start, self.end
            )
            if frame.empty:
                continue
            frame.to_csv(temporary, mode="w" if first else "a", header=first, index=False)
            first = False
            coverage.append({
                "code": code, "rows": len(frame),
                "first_date": str(frame["date"].min().date()),
                "last_date": str(frame["date"].max().date()),
            })
        if first:
            raise ValueError("所有证券均无行情，拒绝生成训练输入")
        os.replace(temporary, target)
        pd.DataFrame(coverage).to_csv(self.output_dir / "coverage.csv", index=False)
        return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-requests", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 5))
    args = parser.parse_args()
    tickers = load_tickers(args.universe, START, END)
    codes = [ticker[:6] for ticker in tickers]
    runner = AkshareBackfillRunner(codes, args.output)
    manifest = runner.execute(args.max_requests, args.workers) if args.execute else runner.prepare()
    result = {key: value for key, value in manifest.items() if key != "missing_code_list"}
    if args.execute and manifest["status"] == "complete":
        result["stock_daily"] = str(runner.consolidate().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
