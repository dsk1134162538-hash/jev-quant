"""研究产物的只读状态投影；不触发训练，不连接账户或交易执行器。"""

from __future__ import annotations

import json
from pathlib import Path


def cross_sectional_status(artifact_dir: Path) -> dict:
    artifact_dir = Path(artifact_dir)
    summary_path = artifact_dir / "summary.json"
    if not summary_path.exists():
        return {
            "available": False,
            "status": "not_trained",
            "deploymentEligible": False,
            "tradingConnected": False,
        }
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    result = summary["out_of_sample"]
    portfolio = result["top_quantile_nonoverlapping"]
    data = summary["data"]
    quantiles = result["quantiles"]
    gross_returns = quantiles.get("average_gross_returns", {})
    folds = []
    for fold in summary["folds"]:
        test = fold["test"]
        row = fold.get("row", {})
        folds.append({
            "fold": fold["fold"],
            "rankIc": test["rank_ic"]["mean"],
            "netReturn": test["top_quantile_nonoverlapping"]["total_net_return"],
            "quantileMonotonicity": test["quantiles"]["monotonicity_spearman"],
            "testSignalStart": row.get("test_signal_start"),
            "testSignalEnd": row.get("test_signal_end"),
            "bestIteration": row.get("best_iteration"),
        })
    return {
        "available": True,
        "status": summary["status"],
        "model": "five-day-cross-sectional-lightgbm",
        "generatedAtUtc": summary["generated_at_utc"],
        "dataAsOf": data.get("price_end") or data.get("signal_end"),
        "universeMode": data["universe_mode"],
        "pointInTimeFullMarket": data["point_in_time_full_market"],
        "sampleRows": data.get("sample_rows"),
        "codes": data.get("codes"),
        "signalDays": data.get("signal_days"),
        "signalStart": data.get("signal_start"),
        "signalEnd": data.get("signal_end"),
        "featureCompleteRatio": data.get("feature_complete_ratio"),
        "rankIc": result["rank_ic"]["mean"],
        "rankIcTStat": result["rank_ic"]["t_stat"],
        "rankIcDays": result["rank_ic"]["days"],
        "coverage": result["coverage"]["ratio"],
        "quantileMonotonicity": quantiles["monotonicity_spearman"],
        "quantileGrossReturns": [gross_returns[str(index)] for index in range(1, 6)] if gross_returns else [],
        "netReturn": portfolio["total_net_return"],
        "annualizedNetReturn": portfolio.get("annualized_net_return"),
        "maxDrawdown": portfolio["max_drawdown"],
        "periodMeanTStat": portfolio["period_mean_t_stat"],
        "averageTurnover": portfolio["average_one_way_turnover"],
        "averagePositions": portfolio.get("average_positions"),
        "walkForward": summary.get("walk_forward", {}),
        "limitations": summary.get("limitations", []),
        "folds": folds,
        "deploymentEligible": summary["deployment_eligible"],
        "tradingConnected": False,
        "artifactDir": str(artifact_dir.resolve()),
    }
