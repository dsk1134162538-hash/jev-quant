"""下载并校验 PIT 研究所需的沪深 300 未复权日线。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pandas as pd

from research.qlib_validation import _validate_bars

DEFAULT_OUTPUT = Path("research/artifacts/akshare-daily-backfill/benchmark_daily.csv")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2021-06-01")
    parser.add_argument("--end", default="2026-10-27")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    import akshare as ak

    frame = ak.stock_zh_index_daily(symbol="sh000300")
    frame = _validate_bars(frame, ("open", "high", "low", "close", "volume"))
    frame = frame[
        frame["date"].between(pd.Timestamp(args.start), pd.Timestamp(args.end))
    ].reset_index(drop=True)
    if frame.empty:
        raise ValueError("指定区间内沪深 300 数据为空")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".csv.tmp")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, args.output)
    print(json.dumps({
        "output": str(args.output.resolve()),
        "source": "AKShare Sina stock_zh_index_daily sh000300",
        "adjust": "none",
        "rows": len(frame),
        "first_date": str(frame["date"].min().date()),
        "last_date": str(frame["date"].max().date()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
