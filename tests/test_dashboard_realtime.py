import inspect
from datetime import datetime

from dashboard import server
from dashboard.adaptive_paper import AdaptivePaperEngine
from dashboard.decision_journal import DecisionJournal
from dashboard.market_snapshot import MarketSnapshotService
from jevq.models import Quote

NOW = datetime(2026, 10, 27, 10, 30)


def quote(code="600001", price=10.0, ts=NOW):
    return Quote(code=code, name="测试公司", price=price, pre_close=10.0, open=10.0, high=10.2, low=9.8,
                 volume=10000, amount=1_000_000, limit_up=11.0, limit_down=9.0, ts=ts)


def context():
    return {"snapshot": {"phase": "盘中"},
            "marketBreadth": {"complete": True, "stale": False, "up": 3000, "down": 2000, "upRatio": 55.0,
                              "total": 5500, "universe": 5577, "limitUp": 50, "limitDown": 10},
            "sectorBreadth": {"complete": True, "stale": False, "up": 30, "down": 19,
                              "leaders": [{"name": "机器人", "change": 2.3}]},
            "observations": [{"code": "000559.SZ", "price": "10.00"}]}


class Judge:
    model_name = "kimi-test"

    def ask(self, state, questions):
        if "portfolio" in questions:
            codes = questions["portfolio"]["criteria"]["codes"]
            return {"portfolio": {"posture": "平衡", "positionPct": 0.2,
                                  "orders": {code: {"action": "模拟买入" if code == "600001" else "观察",
                                                    "confidence": 0.9, "reason": "测试"} for code in codes}}}
        return {"regime": {"choice": "修复观察", "confidence": 0.8},
                "action": {"choice": "仅观察候选", "confidence": 0.8}}


def test_layered_refresh_defaults():
    assert inspect.signature(server.DashboardService.start_background).parameters["poll_seconds"].default == 1
    assert inspect.signature(server.DashboardService).parameters["cache_seconds"].default == 1
    assert MarketSnapshotService().cache_seconds == 30


def test_fast_channel_covers_sector_leaders_and_paper_positions(tmp_path):
    class Market:
        def payload(self):
            return {"sectorBreadth": {"leaders": [{"leaderCode": "sh600001"}]}}

    class Paper:
        def status(self):
            return {"account": {"positionDetails": [{"code": "000559"}]}}

    service = server.DashboardService(db_path=tmp_path / "missing.sqlite", market_service=Market(),
                                      decision_journal=DecisionJournal(tmp_path / "ai.jsonl"), paper_engine=Paper())
    symbols = service._fast_symbols()
    assert "sh600001" in symbols and "sz000559" in symbols
    assert "sh000001" in symbols and len(symbols) == len(set(symbols))


def test_paper_quotes_prefer_fast_channel(tmp_path):
    old, fast = quote(price=9.9), quote(price=10.1)

    class Market:
        def quotes_for(self, codes):
            return {"600001": old}

    service = server.DashboardService(db_path=tmp_path / "missing.sqlite", market_service=Market(),
                                      decision_journal=DecisionJournal(tmp_path / "ai.jsonl"))
    service._quotes = {"600001": fast}
    assert service._paper_quotes(["600001"])["600001"] is fast


def test_market_anomaly_is_detected_against_last_successful_decision(tmp_path):
    journal = DecisionJournal(tmp_path / "ai.jsonl", judge=Judge(), clock=lambda: NOW)
    journal.update_now(context())
    assert journal._significant_event(context()) is False

    broad_move = context()
    broad_move["marketBreadth"]["upRatio"] = 60.0
    assert journal._significant_event(broad_move) is True

    price_move = context()
    price_move["observations"][0]["price"] = "10.10"
    assert journal._significant_event(price_move) is True
    assert journal.status()["eventCooldownSeconds"] == 60


def test_fast_risk_flags_limit_down_without_real_trading(tmp_path):
    paper = AdaptivePaperEngine(tmp_path / "paper.sqlite", judge=Judge(),
                                quote_provider=lambda codes: {code: quote(code=code) for code in codes},
                                clock=lambda: NOW)
    record = {"id": "AI-20261027-REALTIME", "action": "允许模拟小仓评估", "regime": "修复观察",
              "model": "kimi-test", "confidence": 0.9}
    strategy_context = context()
    strategy_context["sectorBreadth"]["leaders"] = [{"leaderCode": "sh600001", "name": "测试行业"}]
    paper.process_record(record, strategy_context)
    risk = paper.check_fast_risk({"600001": quote(price=9.0)})
    assert risk["status"] == "attention"
    assert "跌停" in risk["alerts"][0]
    assert risk["liveTrading"] is False
    assert paper.status()["fastRisk"]["checkedAt"] == NOW.isoformat(timespec="seconds")
    assert paper.status()["liveTrading"] is False
