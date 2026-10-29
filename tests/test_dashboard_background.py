import time

from dashboard import server
from dashboard.decision_journal import DecisionJournal


class EmptyMarket:
    def payload(self):
        return {"marketBreadth": {"available": False, "complete": False, "total": 0},
                "sectorBreadth": {"available": False, "complete": False, "total": 0}}


def test_default_judge_never_falls_back_to_jev_or_claude(monkeypatch, tmp_path):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "must-not-be-used")
    assert server.DashboardService._default_decision_judge() is None

    sentinel = object()
    monkeypatch.setenv("KIMI_API_KEY", "configured")
    monkeypatch.delenv("DASHBOARD_DECISION_SOURCE", raising=False)
    monkeypatch.setattr(server, "KimiJudge", lambda session: sentinel)
    assert server.DashboardService._default_decision_judge() is None

    monkeypatch.setenv("DASHBOARD_DECISION_SOURCE", "api")
    assert server.DashboardService._default_decision_judge() is sentinel


def test_background_loop_runs_without_dashboard_requests(tmp_path):
    calls = []

    def fetch(_):
        calls.append(1)
        return {}

    service = server.DashboardService(
        db_path=tmp_path / "missing.sqlite", quote_fetch=fetch, cache_seconds=0,
        market_service=EmptyMarket(), decision_journal=DecisionJournal(tmp_path / "ai.jsonl"))
    service.start_background(poll_seconds=0.01)
    try:
        deadline = time.monotonic() + 2
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) >= 2
        assert service.background_status()["running"] is True
        assert service.background_status()["lastSuccess"] is not None
    finally:
        service.stop_background()
    assert service.background_status()["running"] is False
