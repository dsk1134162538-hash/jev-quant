from datetime import datetime
from types import SimpleNamespace

from dashboard.decision_journal import DecisionJournal
from dashboard.market_snapshot import (MarketSnapshotService, market_symbol, parse_industries, summarize_industries,
                                       summarize_market)

NOW = datetime(2026, 10, 27, 13, 30)


def quote(code, pct, price=10.0):
    return SimpleNamespace(code=code, pct=pct, price=price, pre_close=10.0, amount=1_000_000.0,
                           limit_up=11.0, limit_down=9.0, suspended=False, ts=NOW)


def context():
    return {
        "snapshot": {"phase": "盘中"},
        "marketBreadth": {"complete": True, "stale": False, "up": 3000, "down": 2000, "upRatio": 55.0,
                          "total": 5500, "universe": 5577, "limitUp": 50, "limitDown": 10},
        "sectorBreadth": {"complete": True, "stale": False, "up": 30, "down": 19,
                          "leaders": [{"name": "机器人", "change": 2.3}]},
    }


class Judge:
    def ask(self, state, questions):
        assert state["marketBreadth"]["total"] == 5500
        assert set(questions) == {"regime", "action"}
        return {"regime": {"choice": "修复观察", "confidence": 0.72},
                "action": {"choice": "仅观察候选", "confidence": 0.81}}


def test_market_symbol_includes_beijing():
    assert market_symbol("600000") == "sh600000"
    assert market_symbol("000001") == "sz000001"
    assert market_symbol("920982") == "bj920982"


def test_market_summary_counts_full_a_share_coverage():
    universe = {str(index): str(index) for index in range(1000)}
    quotes = {code: quote(code, 1.0 if index < 600 else -1.0 if index < 900 else 0.0)
              for index, code in enumerate(universe)}
    result = summarize_market(quotes, universe, NOW)
    assert result["complete"] is True
    assert (result["up"], result["down"], result["flat"]) == (600, 300, 100)
    assert result["upRatio"] == 60.0
    assert result["amount"] == 1_000_000_000.0


def test_industry_parser_and_summary():
    rows = parse_industries('{"new_a":"new_a,机器人,20,10,0.2,2.05,1000,2000000,sz000001,5.1,10,0.5,领涨股"}')
    result = summarize_industries(rows, NOW)
    assert result["leaders"][0]["name"] == "机器人"
    assert result["up"] == 1 and result["available"] is True


def test_market_failure_keeps_last_good_snapshot():
    universe = {"600000": "浦发银行"}
    service = MarketSnapshotService(universe_fetch=lambda: universe,
                                    quote_fetch=lambda _: {"600000": quote("600000", 1.0)},
                                    industry_fetch=lambda: [{"name": "银行", "companies": 40, "change": 1.0,
                                                             "amount": 1, "leaderCode": "600000", "leaderName": "浦发",
                                                             "leaderChange": 1.0}], clock=lambda: NOW)
    service.refresh_now()
    assert service.payload()["marketBreadth"]["up"] == 1
    service.quote_fetch = lambda _: (_ for _ in ()).throw(RuntimeError("network down"))
    service.refresh_now()
    result = service.payload()
    assert result["marketBreadth"]["up"] == 1
    assert result["marketBreadth"]["stale"] is True
    assert result["marketBreadth"]["error"] == "network down"
    assert result["sectorBreadth"]["available"] is True


def test_ai_decision_is_append_only_and_reloads(tmp_path):
    path = tmp_path / "runtime" / "ai_decisions.jsonl"
    journal = DecisionJournal(path, judge=Judge(), clock=lambda: NOW)
    journal.update_now(context())
    journal.update_now(context())
    restored = DecisionJournal(path)
    assert len(restored.records()) == 2
    assert restored.records()[0]["regime"] == "修复观察"
    assert restored.records()[0]["persisted"] is True
    assert "quote.json" in restored.records()[0]["stock"]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_ai_waits_for_complete_intraday_data(tmp_path):
    journal = DecisionJournal(tmp_path / "ai.jsonl", judge=Judge())
    incomplete = context()
    incomplete["marketBreadth"]["complete"] = False
    journal.maybe_update(incomplete)
    assert journal.status()["count"] == 0


def test_invalid_ai_answer_does_not_create_record(tmp_path):
    class BadJudge:
        def ask(self, state, questions):
            return {"regime": {"choice": "追涨"}, "action": {"choice": "自动买入"}}

    journal = DecisionJournal(tmp_path / "ai.jsonl", judge=BadJudge())
    journal.update_now(context())
    assert journal.records() == []
    assert "未定义" in journal.status()["error"]
