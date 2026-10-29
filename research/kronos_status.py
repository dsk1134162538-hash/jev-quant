"""Kronos 对照实验的只读状态；全量结果优先，冒烟结果不得视为可比业绩。"""

from __future__ import annotations

import json
from pathlib import Path


def kronos_status(full_dir: Path, smoke_dir: Path) -> dict:
    full_path = Path(full_dir) / "summary.json"
    smoke_path = Path(smoke_dir) / "folds" / "fold_01" / "metrics.json"
    if full_path.exists():
        summary = json.loads(full_path.read_text(encoding="utf-8"))
        result = summary["out_of_sample"]
        return {
            "available": True,
            "status": summary["status"],
            "scope": "full_walk_forward",
            "comparisonEligible": bool(summary.get("comparison_eligible")),
            "deploymentEligible": False,
            "tradingConnected": False,
            "generatedAtUtc": summary["generated_at_utc"],
            "modelRevision": summary["model"]["model_revision"],
            "coverage": result["coverage"]["ratio"],
            "rankIcDays": result["rank_ic"]["days"],
        }
    if smoke_path.exists():
        summary = json.loads(smoke_path.read_text(encoding="utf-8"))
        return {
            "available": True,
            "status": summary["status"],
            "scope": "smoke_only",
            "comparisonEligible": False,
            "deploymentEligible": False,
            "tradingConnected": False,
            "generatedAtUtc": summary["generated_at_utc"],
            "modelRevision": summary["model"]["model_revision"],
            "coverage": summary["test"]["coverage"]["ratio"],
            "rankIcDays": summary["test"]["rank_ic"]["days"],
            "smoke": summary["smoke"],
        }
    return {
        "available": False,
        "status": "not_started",
        "scope": "none",
        "comparisonEligible": False,
        "deploymentEligible": False,
        "tradingConnected": False,
    }
