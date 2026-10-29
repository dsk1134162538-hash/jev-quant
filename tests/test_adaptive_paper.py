import json
import sqlite3
from datetime import datetime, timedelta

from dashboard.adaptive_paper import AdaptivePaperEngine, INITIAL_CAPITAL
from dashboard.kimi_judge import KimiJudge
from jevq.config import AI_ACCOUNT
from jevq.models import Quote

NOW = datetime(2026, 10, 27, 10, 30)


def quote(code="600001", price=10.0, ts=NOW, name="甲公司"):
    return Quote(code=code, name=name, price=price, pre_close=10.0, open=10.0, high=10.2, low=9.8,
                 volume=10000, amount=1_000_000, limit_up=11.0, limit_down=9.0, ts=ts)


def context():
    return {"snapshot": {"phase": "盘中"},
            "marketBreadth": {"complete": True, "stale": False, "up": 3000, "down": 2000},
            "sectorBreadth": {"complete": True, "stale": False,
                              "leaders": [{"leaderCode": "sh600001", "name": "机器人"}]},
            "observations": []}


def record(action="允许模拟小仓评估", suffix="001"):
    return {"id": f"AI-20261027-{suffix}", "action": action, "regime": "修复观察",
            "model": "kimi-for-coding", "confidence": 0.8}


class Judge:
    model_name = "kimi-test"

    def __init__(self, action="模拟买入", confidence=0.9):
        self.action = action
        self.confidence = confidence
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        codes = questions["portfolio"]["criteria"]["codes"]
        return {"portfolio": {"type": "portfolio", "posture": "平衡", "positionPct": 0.2,
                              "orders": {code: {"action": self.action if code == "600001" else "观察",
                                                "confidence": self.confidence, "reason": "测试证据"}
                                         for code in codes}}}


def engine(tmp_path, judge=None, clock=lambda: NOW):
    quotes = {"600001": quote()}
    return AdaptivePaperEngine(tmp_path / "paper.sqlite", judge=judge or Judge(),
                               quote_provider=lambda codes: {code: quotes[code] for code in codes}, clock=clock)


def test_dynamic_strategy_produces_reconciled_simulated_buy(tmp_path):
    judge = Judge()
    paper = engine(tmp_path, judge)
    paper.process_record(record(), context())
    status = paper.status()
    assert status["account"]["cash"] < INITIAL_CAPITAL
    assert status["account"]["positions"] == 1
    assert status["account"]["positionDetails"][0]["shares"] == 100
    assert status["account"]["fillsToday"] == 1
    assert status["reconciled"] is True and status["liveTrading"] is False
    assert status["latestStrategy"]["model"] == "kimi-test"
    assert status["latestStrategy"]["execution"]["fills"][0]["side"] == "buy"


def test_same_decision_is_idempotent(tmp_path):
    judge = Judge()
    paper = engine(tmp_path, judge)
    paper.process_record(record(), context())
    paper.process_record(record(), context())
    assert judge.calls == 1
    assert paper.status()["account"]["fillsToday"] == 1


def test_research_observation_vetoes_simulated_buy(tmp_path):
    paper = engine(tmp_path)
    paper.process_record(record(action="仅观察候选"), context())
    status = paper.status()
    assert status["account"]["positions"] == 0
    assert "veto" in status["latestStrategy"]["execution"]["skipped"][0]


def test_t_plus_one_blocks_same_day_sell_and_allows_next_day(tmp_path):
    current = [NOW]
    judge = Judge()
    paper = engine(tmp_path, judge, clock=lambda: current[0])
    paper.process_record(record(), context())
    judge.action = "模拟卖出"
    paper.process_record(record(suffix="002"), context())
    assert paper.status()["account"]["positions"] == 1
    assert "T+1" in "".join(paper.status()["latestStrategy"]["execution"]["skipped"])

    current[0] = NOW + timedelta(days=1)
    tomorrow_quote = quote(ts=current[0])
    paper.quote_provider = lambda codes: {code: tomorrow_quote for code in codes}
    paper.process_record(record(suffix="003"), context())
    status = paper.status()
    assert status["account"]["positions"] == 0
    assert status["account"]["fillsToday"] == 1
    assert status["reconciled"] is True


def test_low_confidence_and_stale_quotes_do_not_trade(tmp_path):
    paper = engine(tmp_path, Judge(confidence=0.5))
    paper.process_record(record(), context())
    assert paper.status()["account"]["positions"] == 0

    judge = Judge()
    stale = quote(ts=NOW - timedelta(minutes=10))
    paper = AdaptivePaperEngine(tmp_path / "stale.sqlite", judge=judge,
                                quote_provider=lambda codes: {code: stale for code in codes}, clock=lambda: NOW)
    paper.process_record(record(), context())
    assert judge.calls == 0
    assert paper.status()["account"]["fillsToday"] == 0
    assert "过期" in paper.status()["error"]


def test_cash_tampering_pauses_before_another_ai_call(tmp_path):
    judge = Judge()
    paper = engine(tmp_path, judge)
    paper.process_record(record(), context())
    conn = sqlite3.connect(paper.db_path)
    conn.execute("UPDATE cash SET amount=amount+1 WHERE account=?", (AI_ACCOUNT,))
    conn.commit()
    conn.close()
    paper.process_record(record(suffix="002"), context())
    status = paper.status()
    assert judge.calls == 1
    assert status["reconciled"] is False
    assert "现金" in status["paused"]
    assert status["account"]["fillsToday"] == 1


def test_kimi_portfolio_schema_is_enforced(tmp_path):
    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({"portfolio": {
                "posture": "平衡", "positionPct": 0.2,
                "orders": {"600001": {"action": "模拟买入", "confidence": 0.9, "reason": "行业与量价共振"}}
            }})}}]}

    class Session:
        def post(self, url, **kwargs):
            return Response()

    judge = KimiJudge("secret", session=Session())
    paper = engine(tmp_path, judge)
    paper.process_record(record(), context())
    status = paper.status()
    assert status["latestStrategy"]["status"] == "done"
    assert status["account"]["fillsToday"] == 1
    assert status["latestStrategy"]["portfolio"]["orders"]["600001"]["action"] == "模拟买入"


def test_relative_database_path_can_be_read_after_working_directory_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paper = AdaptivePaperEngine("relative/paper.sqlite", judge=Judge())
    assert paper.status()["reconciled"] is True
