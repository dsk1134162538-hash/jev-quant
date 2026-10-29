"""根据真实 Kronos 冒烟运行计时生成速度估算；不生成或修改业绩结论。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from research.kronos_cross_sectional import MODEL_REVISION, TOKENIZER_REVISION
from research.run_pit_folds import _atomic_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("--rows", type=int, default=512)
    parser.add_argument("--batches", type=int, default=100)
    parser.add_argument("--baseline", type=Path, default=Path("research/artifacts/pit-full-market-baseline/summary.json"))
    parser.add_argument("--benchmark", type=Path, default=Path("research/artifacts/pit-kronos-small-benchmark/folds/fold_01/metrics.json"))
    parser.add_argument("--output", type=Path, default=Path("research/artifacts/pit-kronos-small-benchmark/benchmark.json"))
    args = parser.parse_args()
    if args.seconds <= 0 or args.rows != 512:
        raise ValueError("本记录器要求真实的 512 条基准和正数秒数")
    benchmark = json.loads(args.benchmark.read_text(encoding="utf-8"))
    if benchmark["smoke"]["rows"] != args.rows or benchmark["status"] != "smoke_completed":
        raise ValueError("Kronos 基准产物不完整或行数不符")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    rate = args.rows / args.seconds
    folds = []
    for fold in baseline["folds"]:
        rows = int(fold["row"]["test_rows"])
        seconds = rows / rate
        folds.append({
            "fold": fold["fold"], "rows": rows,
            "estimated_seconds": seconds, "estimated_days_continuous": seconds / 86400,
        })
    total_rows = int(baseline["out_of_sample"]["coverage"]["scored_rows"])
    total_seconds = total_rows / rate
    result = {
        "status": "completed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "speed_benchmark_only",
        "comparison_eligible": False,
        "deployment_eligible": False,
        "trading_connected": False,
        "device": "CPU",
        "model_revision": MODEL_REVISION,
        "tokenizer_revision": TOKENIZER_REVISION,
        "configuration": {"lookback": 512, "prediction_days": 6, "batch_size": 16, "top_k": 1, "sample_count": 1},
        "measured": {
            "rows": args.rows, "signal_days": 2, "names_per_day": 256,
            "batches": args.batches, "total_seconds": args.seconds,
            "segments_seconds": [300, args.seconds - 300],
            "rows_per_second": rate, "seconds_per_row": 1 / rate,
        },
        "estimates": {
            "folds": folds,
            "full_rows": total_rows,
            "full_seconds_continuous": total_seconds,
            "full_days_continuous": total_seconds / 86400,
            "assumption": "Same CPU, batch size and context-length mix, running continuously without other workloads; actual full-run time may be longer.",
        },
        "notes": [
            "Variable historical lengths create partial batches; throughput is not inferred from the 32-row smoke run.",
            "Timing includes data loading, inference, scoring and evaluation; interrupted run was resumed without recomputing saved rows.",
            "Speed benchmark only; smoke RankIC and portfolio returns are not model-comparison evidence.",
        ],
    }
    _atomic_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
