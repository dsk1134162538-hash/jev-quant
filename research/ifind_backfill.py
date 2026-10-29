"""按全 A 历史主数据续传 iFinD 前复权日线；不连接交易链路。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import pandas as pd

DEFAULT_UNIVERSE = Path("research/artifacts/pit-backfill/universe_all_a_history.csv")
DEFAULT_OUTPUT = Path("research/artifacts/ifind-daily-backfill")
DEFAULT_HELPER = (
    Path.home()
    / "AppData/Roaming/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home"
    / "plugins/managed/ifind/scripts/ifind_tool.py"
)
PRICE_COLUMNS = ["open", "high", "low", "close", "volume", "thscode", "time"]
MARKET_SUFFIXES = {
    "上海证券交易所": "SH",
    "深圳证券交易所": "SZ",
    "北京证券交易所": "BJ",
}


@dataclass(frozen=True)
class Window:
    name: str
    start: str
    end: str


WINDOWS = (
    Window("warmup_2021_2024", "2021-06-01", "2024-05-31"),
    Window("recent_2024_2026", "2024-06-01", "2026-10-27"),
)


def ticker_for(row: pd.Series) -> str:
    market = row["SEC_MAR_PAR"]
    if market not in MARKET_SUFFIXES:
        raise ValueError(f"不支持的证券市场：{market}")
    return f"{row['STK_CODE']}.{MARKET_SUFFIXES[market]}"


def load_tickers(universe_path: Path, start: str, end: str) -> list[str]:
    universe = pd.read_csv(universe_path, dtype=str, keep_default_na=False)
    required = {"STK_CODE", "STK_UNI_CODE", "SEC_MAR_PAR", "LIST_DATE", "DELIST_DATE"}
    if not required.issubset(universe.columns):
        raise ValueError(f"历史主数据缺少字段：{sorted(required.difference(universe.columns))}")
    if universe["STK_UNI_CODE"].duplicated().any():
        raise ValueError("历史主数据统一编码重复")
    listed = pd.to_datetime(universe["LIST_DATE"], errors="coerce")
    delisted = pd.to_datetime(universe["DELIST_DATE"], errors="coerce")
    active = (listed.isna() | (listed <= pd.Timestamp(end))) & (
        delisted.isna() | (delisted >= pd.Timestamp(start))
    )
    tickers = [ticker_for(row) for _, row in universe.loc[active].iterrows()]
    # 600018 等代码可能被前后两个非重叠历史主体使用；行情按代码去重，PIT 校验按日期区分主体。
    return sorted(set(tickers))


def make_batches(tickers: Sequence[str], size: int = 3) -> list[list[str]]:
    if size != 3:
        raise ValueError("iFinD 历史行情每批必须恰好使用三只上限")
    return [list(tickers[index:index + size]) for index in range(0, len(tickers), size)]


def validate_prices(
    frame: pd.DataFrame,
    requested_tickers: Sequence[str],
    window: Window,
    allow_empty: bool = False,
) -> pd.DataFrame:
    missing = set(PRICE_COLUMNS).difference(frame.columns)
    if missing:
        raise ValueError(f"行情缺少字段：{sorted(missing)}")
    if frame.empty:
        if allow_empty:
            return frame.copy()
        raise ValueError("行情为空且响应未明确标记 EMPTY_DATA")
    result = frame.copy()
    result["thscode"] = result["thscode"].astype(str).str.upper()
    unexpected = set(result["thscode"]).difference(requested_tickers)
    if unexpected:
        raise ValueError(f"返回未请求证券：{sorted(unexpected)}")
    dates = pd.to_datetime(result["time"].astype(str), format="%Y%m%d", errors="coerce")
    if dates.isna().any():
        raise ValueError("行情日期格式错误")
    if ((dates < pd.Timestamp(window.start)) | (dates > pd.Timestamp(window.end))).any():
        raise ValueError("行情超出请求时间窗口")
    result["date"] = dates
    for column in ("open", "high", "low", "close", "volume"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    if result[["open", "high", "low", "close", "volume"]].isna().any().any():
        raise ValueError("行情价格或成交量存在非数值")
    if (result[["open", "high", "low", "close"]] <= 0).any().any() or (result["volume"] < 0).any():
        raise ValueError("行情价格必须为正且成交量不能为负")
    if (result["high"] < result[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError("最高价低于其他价格")
    if (result["low"] > result[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("最低价高于其他价格")
    if result.duplicated(["thscode", "date"]).any():
        raise ValueError("同一证券与日期出现重复行情")
    return result.sort_values(["thscode", "date"]).reset_index(drop=True)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


PriceCaller = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def call_subprocess(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, timeout=120,
    )


class IfindBackfillRunner:
    def __init__(
        self,
        tickers: Sequence[str],
        output_dir: Path,
        helper: Path = DEFAULT_HELPER,
        caller: PriceCaller = call_subprocess,
        windows: Sequence[Window] = WINDOWS,
    ) -> None:
        self.tickers = list(tickers)
        self.batches = make_batches(self.tickers)
        self.output_dir = output_dir
        self.helper = helper
        self.caller = caller
        self.windows = list(windows)

    def _path(self, batch_number: int, window: Window) -> Path:
        return self.output_dir / "batches" / window.name / f"batch-{batch_number:05d}.csv"

    def jobs(self) -> list[tuple[int, list[str], Window]]:
        return [
            (number, batch, window)
            for window in self.windows
            for number, batch in enumerate(self.batches, start=1)
        ]

    def _valid_saved(self, path: Path, tickers: Sequence[str], window: Window) -> bool:
        if not path.is_file():
            return False
        try:
            frame = pd.read_csv(path, dtype={"thscode": str, "time": str})
            validate_prices(frame, tickers, window, allow_empty=True)
        except (OSError, ValueError, pd.errors.ParserError):
            return False
        return True

    def prepare(self) -> dict:
        missing = []
        saved = 0
        returned_rows = 0
        for number, batch, window in self.jobs():
            path = self._path(number, window)
            if self._valid_saved(path, batch, window):
                saved += 1
                returned_rows += len(pd.read_csv(path))
            else:
                missing.append({"batch": number, "window": window.name})
        total = len(self.batches) * len(self.windows)
        manifest = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "complete" if not missing else "awaiting_download",
            "adjust": "forward",
            "interval": "D",
            "tickers": len(self.tickers),
            "batches_per_window": len(self.batches),
            "windows": [asdict(window) for window in self.windows],
            "total_requests": total,
            "saved_requests": saved,
            "missing_requests": len(missing),
            "returned_rows": returned_rows,
            "missing_jobs": missing,
        }
        _atomic_json(self.output_dir / "manifest.json", manifest)
        return manifest

    def execute(self, max_requests: int, workers: int = 1) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Lock

        if max_requests < 0:
            raise ValueError("请求上限不能为负")
        if not 1 <= workers <= 4:
            raise ValueError("并发数必须在 1—4 之间")
        pending = [
            (number, batch, window)
            for number, batch, window in self.jobs()
            if not self._valid_saved(self._path(number, window), batch, window)
        ]
        if pending and max_requests == 0:
            raise RuntimeError(f"仍有 {len(pending)} 次请求，但本轮上限为 0")
        if pending and not self.helper.is_file():
            raise FileNotFoundError(f"iFinD 调用脚本不存在：{self.helper}")
        completed = 0
        ledger_lock = Lock()

        def download(number: int, batch: list[str], window: Window) -> None:
            target = self._path(number, window)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_csv = target.with_suffix(".csv.tmp")
            temporary_log = target.with_suffix(".log.tmp")
            params = {
                "ticker": ",".join(batch),
                "start_date": window.start,
                "end_date": window.end,
                "interval": "D",
                "adjust": "forward",
                "file_path": str(temporary_csv.resolve()),
            }
            command = [
                sys.executable, str(self.helper), "call",
                "--api-name", "ifind_get_price",
                "--params-json", json.dumps(params, ensure_ascii=False),
            ]
            attempt = {
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "batch": number, "window": window.name, "tickers": batch,
                "status": "failed",
            }
            try:
                result = self.caller(command)
                stdout = (result.stdout or "") + (result.stderr or "")
                temporary_log.write_text(stdout, encoding="utf-8")
                attempt["return_code"] = result.returncode
                empty = "EMPTY_DATA" in stdout and not temporary_csv.is_file()
                if empty:
                    temporary_csv.write_text(",".join(PRICE_COLUMNS) + "\n", encoding="utf-8")
                elif result.returncode != 0:
                    raise RuntimeError(f"iFinD 请求失败：{stdout[-800:]}")
                frame = pd.read_csv(temporary_csv, dtype={"thscode": str, "time": str})
                validated = validate_prices(frame, batch, window, allow_empty=empty)
                validated[PRICE_COLUMNS].to_csv(temporary_csv, index=False)
                os.replace(temporary_csv, target)
                os.replace(temporary_log, target.with_suffix(".log"))
                attempt["status"] = "empty" if empty else "completed"
                attempt["rows"] = len(validated)
            except Exception as exc:
                attempt["error"] = str(exc)[-1000:]
                raise
            finally:
                attempt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
                ledger = self.output_dir / "request_ledger.jsonl"
                ledger.parent.mkdir(parents=True, exist_ok=True)
                with ledger_lock:
                    with ledger.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(attempt, ensure_ascii=False, allow_nan=False) + "\n")
                temporary_csv.unlink(missing_ok=True)

        selected = pending[:max_requests]
        if workers == 1:
            for number, batch, window in selected:
                download(number, batch, window)
                completed += 1
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(download, number, batch, window)
                    for number, batch, window in selected
                ]
                for future in futures:
                    future.result()
                    completed += 1
        manifest = self.prepare()
        manifest["new_requests"] = completed
        _atomic_json(self.output_dir / "manifest.json", manifest)
        return manifest

    def consolidate(self) -> Path:
        manifest = self.prepare()
        if manifest["missing_requests"]:
            raise RuntimeError(f"仍有 {manifest['missing_requests']} 次行情请求未完成")
        target = self.output_dir / "stock_daily.csv"
        temporary = target.with_suffix(".csv.tmp")
        first = True
        coverage = []
        for number, batch, window in self.jobs():
            path = self._path(number, window)
            frame = pd.read_csv(path, dtype={"thscode": str, "time": str})
            frame = validate_prices(frame, batch, window, allow_empty=True)
            if frame.empty:
                continue
            output = frame.rename(columns={"thscode": "ticker"})[
                ["date", "ticker", "open", "high", "low", "close", "volume"]
            ].copy()
            output["code"] = output["ticker"].str[:6]
            output = output[["date", "code", "ticker", "open", "high", "low", "close", "volume"]]
            output.to_csv(temporary, mode="w" if first else "a", header=first, index=False)
            first = False
            for ticker, group in output.groupby("ticker"):
                coverage.append({
                    "ticker": ticker, "window": window.name, "rows": len(group),
                    "first_date": str(group["date"].min().date()),
                    "last_date": str(group["date"].max().date()),
                })
        if first:
            raise ValueError("所有批次均无行情，拒绝生成训练输入")
        os.replace(temporary, target)
        coverage_frame = pd.DataFrame(coverage)
        coverage_frame.to_csv(self.output_dir / "coverage_by_window.csv", index=False)
        return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--helper", type=Path, default=DEFAULT_HELPER)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-requests", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 5))
    args = parser.parse_args()
    start = min(window.start for window in WINDOWS)
    end = max(window.end for window in WINDOWS)
    tickers = load_tickers(args.universe, start, end)
    runner = IfindBackfillRunner(tickers, args.output, args.helper)
    manifest = runner.execute(args.max_requests, args.workers) if args.execute else runner.prepare()
    result = {key: value for key, value in manifest.items() if key != "missing_jobs"}
    if args.execute and manifest["status"] == "complete":
        result["stock_daily"] = str(runner.consolidate().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
