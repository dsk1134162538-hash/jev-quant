"""固化全 A PIT 横截面训练样本，供滚动折独立续跑。"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from research.cross_sectional_model import FEATURE_COLUMNS, _file_hash, _json_safe, build_samples
from research.pit_validation import UNIVERSE_REQUIRED, apply_pit_universe

DEFAULT_OUTPUT = Path("research/artifacts/pit-full-market-baseline")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stock-data", type=Path,
        default=Path("research/artifacts/akshare-daily-backfill/stock_daily.csv"),
    )
    parser.add_argument(
        "--benchmark-data", type=Path,
        default=Path("research/artifacts/akshare-daily-backfill/benchmark_daily.csv"),
    )
    parser.add_argument(
        "--universe-data", type=Path,
        default=Path("research/artifacts/pit-backfill/universe_all_a_history.csv"),
    )
    parser.add_argument(
        "--pit-stocks-data", type=Path,
        help="可选的第一阶段缓存；传入后不重复执行上市身份过滤",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    benchmark = pd.read_csv(args.benchmark_data)
    if args.pit_stocks_data:
        stocks = pd.read_parquet(args.pit_stocks_data)
        report_path = args.pit_stocks_data.with_name("pit_stocks_report.json")
        pit_report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        price_columns = ["open", "high", "low", "close", "volume"]
        stocks = pd.read_csv(
            args.stock_data,
            dtype={"code": str, **{column: np.float32 for column in price_columns}},
        )
        universe = pd.read_csv(
            args.universe_data,
            usecols=lambda column: column in UNIVERSE_REQUIRED,
            dtype={"code": str, "STK_CODE": str, "STK_UNI_CODE": str},
        )
        stocks, pit_report = apply_pit_universe(stocks, universe)
        del universe
    samples = build_samples(stocks, benchmark)
    del stocks
    for column in FEATURE_COLUMNS:
        samples[column] = samples[column].astype(np.float32)

    if samples[["signal_date", "entry_date", "exit_date"]].isna().any().any():
        raise ValueError("样本信号、建仓或退出日期存在缺失")
    if not (samples["signal_date"] < samples["entry_date"]).all():
        raise ValueError("样本建仓日必须晚于信号日")
    if not (samples["entry_date"] < samples["exit_date"]).all():
        raise ValueError("样本退出日必须晚于建仓日")
    if not samples["target_rank"].between(0, 1).all():
        raise ValueError("横截面标签超出 0—1")
    if (samples["gross_return"] < -1).any():
        raise ValueError("样本五日收益低于 -100%")

    args.output.mkdir(parents=True, exist_ok=True)
    sample_path = args.output / "samples.pkl"
    temporary = sample_path.with_suffix(".pkl.tmp")
    samples.to_pickle(temporary)
    os.replace(temporary, sample_path)
    summary = {
        "status": "prepared",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "samples_path": str(sample_path.resolve()),
        "stock_path": str(args.stock_data.resolve()),
        "benchmark_path": str(args.benchmark_data.resolve()),
        "universe_path": str(args.universe_data.resolve()),
        "input_sha256": _file_hash((args.stock_data, args.benchmark_data, args.universe_data)),
        "universe_mode": "pit_full_market",
        "point_in_time_full_market": True,
        "pit_validation": pit_report,
        "codes": int(samples["code"].nunique()),
        "sample_rows": int(len(samples)),
        "signal_days": int(samples["signal_date"].nunique()),
        "signal_start": str(samples["signal_date"].min().date()),
        "signal_end": str(samples["signal_date"].max().date()),
        "feature_complete_rows": int(samples["feature_complete"].sum()),
        "feature_complete_ratio": float(samples["feature_complete"].mean()),
        "minimum_raw_cross_section": int(samples["raw_universe_size"].min()),
        "price_adjustment": "Sina raw OHLCV combined with same-day qfq factor; no future factor",
        "label": "T close -> T+1 market-day open -> T+6 market-day open; suspended boundary dropped",
    }
    summary_path = args.output / "data_summary.json"
    temporary = summary_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(_json_safe(summary), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, summary_path)
    print(json.dumps(_json_safe(summary), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
