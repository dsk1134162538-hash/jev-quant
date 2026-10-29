"""第一阶段：固化经过历史上市身份过滤的全 A 行情。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from research.cross_sectional_model import _json_safe
from research.pit_validation import UNIVERSE_REQUIRED, apply_pit_universe


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stock-data", type=Path,
        default=Path("research/artifacts/akshare-daily-backfill/stock_daily.csv"),
    )
    parser.add_argument(
        "--universe-data", type=Path,
        default=Path("research/artifacts/pit-backfill/universe_all_a_history.csv"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("research/artifacts/pit-full-market-baseline/pit_stocks.parquet"),
    )
    args = parser.parse_args()
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
    filtered, report = apply_pit_universe(stocks, universe)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".parquet.tmp")
    filtered.to_parquet(temporary, index=False)
    os.replace(temporary, args.output)
    report_path = args.output.with_name("pit_stocks_report.json")
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(_json_safe(report), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, report_path)
    print(json.dumps(_json_safe({
        "status": "prepared",
        "output": str(args.output.resolve()),
        "report": str(report_path.resolve()),
        "rows": len(filtered),
        "codes": int(filtered["code"].nunique()),
    }), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
