import json

from dashboard.decision_journal import DecisionJournal
from dashboard.server import DashboardService
from research.status import cross_sectional_status


def summary_fixture():
    return {
        "status": "completed",
        "generated_at_utc": "2026-10-27T00:00:00+00:00",
        "deployment_eligible": False,
        "data": {
            "price_end": "2026-10-27",
            "universe_mode": "non_pit_current_constituents",
            "point_in_time_full_market": False,
        },
        "out_of_sample": {
            "rank_ic": {"mean": 0.025, "t_stat": 1.83, "days": 378},
            "coverage": {"ratio": 0.999},
            "quantiles": {"monotonicity_spearman": -0.1},
            "top_quantile_nonoverlapping": {
                "total_net_return": 0.138,
                "max_drawdown": -0.149,
                "period_mean_t_stat": 0.724,
                "average_one_way_turnover": 0.483,
            },
        },
        "folds": [{
            "fold": 1,
            "test": {
                "rank_ic": {"mean": 0.069},
                "quantiles": {"monotonicity_spearman": 0.6},
                "top_quantile_nonoverlapping": {"total_net_return": 0.105},
            },
        }],
    }


def test_missing_artifact_is_explicitly_unavailable(tmp_path):
    status = cross_sectional_status(tmp_path)
    assert status["available"] is False
    assert status["deploymentEligible"] is False
    assert status["tradingConnected"] is False


def test_status_projects_research_without_enabling_trading(tmp_path):
    (tmp_path / "summary.json").write_text(
        json.dumps(summary_fixture()), encoding="utf-8"
    )
    status = cross_sectional_status(tmp_path)
    assert status["rankIc"] == 0.025
    assert status["pointInTimeFullMarket"] is False
    assert status["folds"][0]["netReturn"] == 0.105
    assert status["deploymentEligible"] is False
    assert status["tradingConnected"] is False


def test_pit_status_projects_sample_quantiles_and_fold_windows(tmp_path):
    fixture = summary_fixture()
    fixture["data"] = {
        "universe_mode": "pit_full_market",
        "point_in_time_full_market": True,
        "sample_rows": 6728796,
        "codes": 5777,
        "signal_days": 1304,
        "signal_start": "2021-06-01",
        "signal_end": "2026-10-19",
        "feature_complete_ratio": 0.9466255181461884,
    }
    fixture["out_of_sample"]["quantiles"]["average_gross_returns"] = {
        str(index): value for index, value in enumerate(
            [0.00181254, 0.00595004, 0.00610865, 0.00613311, 0.00456941], start=1
        )
    }
    fixture["out_of_sample"]["top_quantile_nonoverlapping"].update({
        "annualized_net_return": 0.127398777676053,
        "average_positions": 1080.3153846153846,
    })
    fixture["folds"][0]["row"] = {
        "test_signal_start": "2024-01-18T00:00:00",
        "test_signal_end": "2024-07-29T00:00:00",
        "best_iteration": 182,
    }
    fixture["limitations"] = ["Research artifact only; no trading permission."]
    (tmp_path / "summary.json").write_text(json.dumps(fixture), encoding="utf-8")

    status = cross_sectional_status(tmp_path)
    assert status["dataAsOf"] == "2026-10-19"
    assert status["sampleRows"] == 6728796
    assert status["quantileGrossReturns"][4] == 0.00456941
    assert status["annualizedNetReturn"] == 0.127398777676053
    assert status["averagePositions"] == 1080.3153846153846
    assert status["folds"][0]["testSignalStart"] == "2024-01-18T00:00:00"
    assert status["folds"][0]["bestIteration"] == 182
    assert status["limitations"] == ["Research artifact only; no trading permission."]
    assert status["tradingConnected"] is False


def test_dashboard_service_exposes_same_readonly_research_status(tmp_path):
    (tmp_path / "summary.json").write_text(
        json.dumps(summary_fixture()), encoding="utf-8"
    )
    service = DashboardService(
        db_path=tmp_path / "missing.sqlite",
        decision_journal=DecisionJournal(tmp_path / "decisions.jsonl"),
        research_dir=tmp_path,
    )
    assert service.research_status() == cross_sectional_status(tmp_path)
