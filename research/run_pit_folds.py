"""逐折运行并汇总全 A PIT 滚动 LightGBM 基线，支持中断续跑。"""

from __future__ import annotations

import argparse
import gc
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from research.cross_sectional_model import (
    EvaluationConfig,
    FEATURE_COLUMNS,
    HOLDING_DAYS,
    WalkForwardConfig,
    _file_hash,
    _json_safe,
    _summarize_evaluation,
    evaluate_scores,
    fit_model,
    make_walk_forward_splits,
    purged_fold_samples,
)

DEFAULT_OUTPUT = Path("research/artifacts/pit-full-market-baseline")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _fold_row(fold: int, frames: dict[str, pd.DataFrame], model, validation_metrics: dict, test_metrics: dict) -> dict:
    return {
        "fold": fold,
        "train_signal_start": frames["train"]["signal_date"].min(),
        "train_signal_end": frames["train"]["signal_date"].max(),
        "validation_signal_start": frames["validation"]["signal_date"].min(),
        "validation_signal_end": frames["validation"]["signal_date"].max(),
        "test_signal_start": frames["test"]["signal_date"].min(),
        "test_signal_end": frames["test"]["signal_date"].max(),
        "train_rows_after_purge": len(frames["train"]),
        "validation_rows_after_purge": len(frames["validation"]),
        "test_rows": len(frames["test"]),
        "best_iteration": int(model.best_iteration_ or model.n_estimators),
        "validation_rank_ic": validation_metrics["rank_ic"]["mean"],
        "test_rank_ic": test_metrics["rank_ic"]["mean"],
        "test_net_return": test_metrics["top_quantile_nonoverlapping"]["total_net_return"],
    }


def run_fold(
    samples_path: Path,
    output_dir: Path,
    fold: int,
    walk_config: WalkForwardConfig,
    evaluation_config: EvaluationConfig,
) -> dict:
    samples = pd.read_pickle(samples_path)
    splits = make_walk_forward_splits(samples["signal_date"].unique(), walk_config)
    if not 1 <= fold <= len(splits):
        raise ValueError(f"折号必须在 1—{len(splits)} 之间")
    frames = purged_fold_samples(samples, splits[fold - 1])
    del samples
    gc.collect()
    model = fit_model(frames["train"], frames["validation"])
    validation = frames["validation"].copy()
    test = frames["test"].copy()
    validation["score"] = model.predict(validation[FEATURE_COLUMNS])
    test["score"] = model.predict(test[FEATURE_COLUMNS])
    test["fold"] = fold
    validation_metrics, _ = evaluate_scores(validation, evaluation_config)
    test_metrics, artifacts = evaluate_scores(test, evaluation_config)

    fold_dir = output_dir / "folds" / f"fold_{fold:02d}"
    fold_dir.mkdir(parents=True, exist_ok=True)
    model.booster_.save_model(str(fold_dir / "model.txt"))
    test.to_pickle(fold_dir / "predictions.pkl")
    artifacts["daily_ic"].to_csv(fold_dir / "daily_ic.csv", index=False)
    artifacts["quantile_returns"].to_csv(fold_dir / "quantile_returns.csv", index=False)
    artifacts["portfolio"].to_csv(fold_dir / "portfolio_returns.csv", index=False)
    artifacts["selected"].to_pickle(fold_dir / "selected.pkl")
    importance = pd.DataFrame({
        "feature": FEATURE_COLUMNS,
        "gain": model.booster_.feature_importance(importance_type="gain"),
        "fold": fold,
    })
    importance.to_csv(fold_dir / "feature_importance.csv", index=False)
    row = _fold_row(fold, frames, model, validation_metrics, test_metrics)
    result = {
        "fold": fold,
        "fold_count": len(splits),
        "row": row,
        "validation": validation_metrics,
        "test": test_metrics,
        "model_params": model.get_params(),
    }
    _atomic_json(fold_dir / "metrics.json", result)
    _atomic_json(fold_dir / "completed.json", {
        "status": "completed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "fold": fold,
    })
    return result


def finalize(
    samples_path: Path,
    data_summary_path: Path,
    output_dir: Path,
    walk_config: WalkForwardConfig,
    evaluation_config: EvaluationConfig,
) -> dict:
    samples = pd.read_pickle(samples_path, )
    splits = make_walk_forward_splits(samples["signal_date"].unique(), walk_config)
    del samples
    fold_results = []
    prediction_frames = []
    daily_frames = []
    quantile_frames = []
    portfolio_frames = []
    selected_frames = []
    importance_frames = []
    for fold in range(1, len(splits) + 1):
        fold_dir = output_dir / "folds" / f"fold_{fold:02d}"
        marker = fold_dir / "completed.json"
        if not marker.is_file():
            raise RuntimeError(f"第 {fold} 折尚未完成，拒绝汇总不完整结果")
        metrics = json.loads((fold_dir / "metrics.json").read_text(encoding="utf-8"))
        fold_results.append(metrics)
        prediction_frames.append(pd.read_pickle(fold_dir / "predictions.pkl"))
        daily_frames.append(pd.read_csv(fold_dir / "daily_ic.csv", parse_dates=["signal_date"]))
        quantile_frames.append(pd.read_csv(fold_dir / "quantile_returns.csv", parse_dates=["signal_date"]))
        portfolio_frames.append(pd.read_csv(
            fold_dir / "portfolio_returns.csv",
            parse_dates=["signal_date", "entry_date", "exit_date"],
        ))
        selected_frames.append(pd.read_pickle(fold_dir / "selected.pkl"))
        importance_frames.append(pd.read_csv(fold_dir / "feature_importance.csv"))

    predictions = pd.concat(prediction_frames, ignore_index=True)
    daily_ic = pd.concat(daily_frames, ignore_index=True)
    quantile_returns = pd.concat(quantile_frames, ignore_index=True)
    portfolio = pd.concat(portfolio_frames, ignore_index=True)
    selected = pd.concat(selected_frames, ignore_index=True)
    if predictions["signal_date"].duplicated().any():
        # 同一天不同股票合法；同一股票同一天跨折重复不合法。
        if predictions.duplicated(["signal_date", "code"]).any():
            raise AssertionError("滚动测试信号日与股票组合发生重叠")
    aggregate = _summarize_evaluation(
        predictions, daily_ic, quantile_returns, portfolio, evaluation_config
    )
    folds = pd.DataFrame([result["row"] for result in fold_results])
    if folds["test_signal_start"].duplicated().any():
        raise AssertionError("滚动测试信号日发生重叠")
    importance = (
        pd.concat(importance_frames, ignore_index=True)
        .groupby("feature", as_index=False)["gain"].mean()
        .sort_values("gain", ascending=False)
    )
    predictions.to_pickle(output_dir / "predictions.pkl")
    selected.to_pickle(output_dir / "selected.pkl")
    daily_ic.to_csv(output_dir / "daily_ic.csv", index=False)
    quantile_returns.to_csv(output_dir / "quantile_returns.csv", index=False)
    portfolio.to_csv(output_dir / "portfolio_returns.csv", index=False)
    folds.to_csv(output_dir / "folds.csv", index=False)
    importance.to_csv(output_dir / "feature_importance.csv", index=False)
    data_summary = json.loads(data_summary_path.read_text(encoding="utf-8"))
    summary = {
        "status": "completed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "deployment_eligible": False,
        "model": {
            "algorithm": "LightGBM regression with validation-only early stopping",
            "features": FEATURE_COLUMNS,
            "label": "T close -> T+1 open entry -> T+6 open exit; benchmark-relative return ranked within signal date",
            "holding_days": HOLDING_DAYS,
            "stock_code_used_as_feature": False,
            "hyperparameters_fixed_before_test": True,
            "random_state": 42,
        },
        "walk_forward": asdict(walk_config),
        "evaluation": asdict(evaluation_config),
        "folds": fold_results,
        "out_of_sample": aggregate,
        "data": data_summary,
        "code_sha256": _file_hash((Path(__file__), Path(__file__).with_name("cross_sectional_model.py"))),
        "acceptance": {
            "research_pipeline_completed": True,
            "live_or_paper_trading_approved": False,
            "reason": "PIT baseline evidence is not a profitability guarantee; execution and robustness review remain required.",
        },
        "limitations": [
            "Daily bars only; no intraday liquidity, limit-up/down execution, partial fills or minimum commission simulation.",
            "Top-minus-bottom quantile spread is diagnostic, not an executable A-share long-short return.",
            "Non-overlapping top-quantile portfolio uses approximate turnover costs and no market-impact model.",
            "Signals at entry or exit suspension are dropped; intermediate suspension and limit-lock execution are not modeled.",
            "Research artifact only; no automatic replacement of the existing model and no trading permission.",
        ],
    }
    _atomic_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=DEFAULT_OUTPUT / "samples.pkl")
    parser.add_argument("--data-summary", type=Path, default=DEFAULT_OUTPUT / "data_summary.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--fold", type=int)
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    if (args.fold is None) == (not args.finalize):
        parser.error("必须且只能指定 --fold N 或 --finalize")
    walk_config = WalkForwardConfig()
    evaluation_config = EvaluationConfig()
    if args.finalize:
        result = finalize(args.samples, args.data_summary, args.output, walk_config, evaluation_config)
        printable = {
            "status": result["status"],
            "out_of_sample": result["out_of_sample"],
            "deployment_eligible": result["deployment_eligible"],
        }
    else:
        result = run_fold(args.samples, args.output, args.fold, walk_config, evaluation_config)
        printable = {
            "fold": result["fold"],
            "fold_count": result["fold_count"],
            "row": result["row"],
            "validation_rank_ic": result["validation"]["rank_ic"],
            "test_rank_ic": result["test"]["rank_ic"],
            "test_top_quantile": result["test"]["top_quantile_nonoverlapping"],
        }
    print(json.dumps(_json_safe(printable), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
