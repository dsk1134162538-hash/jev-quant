from datetime import datetime

from dashboard.adaptive_paper import AdaptivePaperEngine
from dashboard.decision_journal import DecisionJournal
from jevq.config import AI_ACCOUNT
from jevq.live import store as live_store
from jevq.live.executor import Executor
from jevq.models import Quote
from tests.live_fakes import FakeBroker, FakeClock, FakeNotifier

NOW = datetime(2026, 10, 27, 10, 30)


def quote(code="600001"):
    return Quote(code=code, name="甲公司", price=9.0, pre_close=10.0, open=10.0, high=10.2, low=8.8,
                 volume=10000, amount=1_000_000, limit_up=11.0, limit_down=8.5, ts=NOW)


def context():
    return {"snapshot": {"phase": "盘中"},
            "marketBreadth": {"complete": True, "stale": False, "up": 3000, "down": 2000, "upRatio": 60.0,
                              "total": 5000, "universe": 5000, "limitUp": 50, "limitDown": 5},
            "sectorBreadth": {"complete": True, "stale": False, "up": 30, "down": 19,
                              "leaders": [{"leaderCode": "sh600001", "name": "机器人", "change": 2.3}]},
            "observations": []}


class Judge:
    model_name = "kimi-integration-test"

    def ask(self, state, questions):
        if "regime" in questions:
            return {"regime": {"choice": "修复观察", "confidence": 0.8},
                    "action": {"choice": "允许模拟小仓评估", "confidence": 0.8}}
        codes = questions["portfolio"]["criteria"]["codes"]
        return {"portfolio": {"posture": "平衡", "positionPct": 0.2,
                              "orders": {code: {"action": "模拟买入", "confidence": 0.9, "reason": "集成测试"}
                                         for code in codes}}}


def test_journal_to_paper_to_existing_dryrun_executor(tmp_path):
    q = quote()
    paper = AdaptivePaperEngine(tmp_path / "paper.sqlite", judge=Judge(),
                                quote_provider=lambda codes: {code: q for code in codes}, clock=lambda: NOW)
    journal = DecisionJournal(tmp_path / "decisions.jsonl", judge=Judge(), clock=lambda: NOW,
                              on_record=paper.process_record)
    journal.update_now(context())

    assert journal.status()["error"] == ""
    assert journal.status()["listenerError"] == ""
    assert paper.status()["account"]["fillsToday"] == 1
    assert paper.status()["reconciled"] is True

    conn = paper._connect()
    clock = FakeClock(NOW)
    broker = FakeBroker(clock, cash=50_000)
    live_store.set_mode(conn, "dryrun")
    live_store.set_state(conn, "trading_day", NOW.date().isoformat())
    live_store.set_state(conn, "self_check_day", NOW.date().isoformat())
    executor = Executor(conn, broker, FakeNotifier(), clock=clock.now, sleep=clock.sleep,
                        quotes=lambda codes: {code: q for code in codes})
    executor._last_reconcile = NOW
    executor.step()

    assert not [call for call in broker.calls if call[0] == "place"]
    intent = conn.execute("SELECT source,status,note FROM live_intents WHERE source='ai'").fetchone()
    assert tuple(intent) == ("ai", "done", "演练")
    order = conn.execute("SELECT status,shares FROM live_orders").fetchone()
    assert order["status"] == "dryrun" and order["shares"] == 100
    assert conn.execute("SELECT COUNT(*) FROM fills WHERE account=?", (AI_ACCOUNT,)).fetchone()[0] == 1
    conn.close()


def test_listener_failure_does_not_erase_journal(tmp_path):
    def broken_listener(record, context):
        raise RuntimeError("listener failed")

    journal = DecisionJournal(tmp_path / "decisions.jsonl", judge=Judge(), clock=lambda: NOW,
                              on_record=broken_listener)
    journal.update_now(context())
    assert len(journal.records()) == 1
    assert journal.status()["error"] == ""
    assert journal.status()["listenerError"] == "listener failed"
