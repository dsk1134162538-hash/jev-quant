import json

from research.kronos_status import kronos_status


def test_kronos_missing_is_not_started(tmp_path):
    status = kronos_status(tmp_path / "full", tmp_path / "smoke")
    assert status["available"] is False
    assert status["comparisonEligible"] is False
    assert status["tradingConnected"] is False


def test_kronos_smoke_is_explicitly_not_comparable(tmp_path):
    metrics = {
        "status": "smoke_completed",
        "generated_at_utc": "2026-10-27T23:40:13+00:00",
        "model": {"model_revision": "fixed-revision"},
        "smoke": {"days": 2, "names_per_day": 16, "rows": 32},
        "test": {"coverage": {"ratio": 0.003}, "rank_ic": {"days": 2}},
    }
    path = tmp_path / "smoke" / "folds" / "fold_01" / "metrics.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(metrics), encoding="utf-8")
    status = kronos_status(tmp_path / "full", tmp_path / "smoke")
    assert status["scope"] == "smoke_only"
    assert status["smoke"]["rows"] == 32
    assert status["coverage"] == 0.003
    assert status["comparisonEligible"] is False
    assert status["deploymentEligible"] is False
    assert status["tradingConnected"] is False


def test_kronos_full_result_takes_precedence(tmp_path):
    summary = {
        "status": "completed",
        "comparison_eligible": True,
        "generated_at_utc": "2026-10-28T00:00:00+00:00",
        "model": {"model_revision": "fixed-revision"},
        "out_of_sample": {"coverage": {"ratio": 1}, "rank_ic": {"days": 630}},
    }
    path = tmp_path / "full" / "summary.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(summary), encoding="utf-8")
    status = kronos_status(tmp_path / "full", tmp_path / "smoke")
    assert status["scope"] == "full_walk_forward"
    assert status["comparisonEligible"] is True
    assert status["rankIcDays"] == 630
    assert status["tradingConnected"] is False
