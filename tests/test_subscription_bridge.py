import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta

import pytest

from dashboard.decision_journal import DecisionJournal
from dashboard.subscription_bridge import create_snapshot, submit_answers
from jevq.config import AI_ACCOUNT
from jevq.models import Quote

NOW = datetime(2026, 10, 28, 10, 30)


def _quote():
    return Quote(code="600001", name="测试公司", price=9.0, pre_close=10.0, open=9.8,
                 high=10.2, low=8.8, volume=10000, amount=1_000_000,
                 limit_up=11.0, limit_down=9.0, ts=NOW)


def _context():
    return {"snapshot": {"phase": "盘中"},
            "marketBreadth": {"complete": True, "stale": False, "up": 3000, "down": 2000,
                              "upRatio": 60.0, "total": 5000, "universe": 5000,
                              "limitUp": 50, "limitDown": 5},
            "sectorBreadth": {"complete": True, "stale": False, "up": 30, "down": 19,
                              "leaders": [{"leaderCode": "sh600001", "name": "机器人", "change": 2.3}]},
            "observations": []}


def _files(tmp_path):
    quote = _quote()
    raw_quote = asdict(quote)
    raw_quote["ts"] = quote.ts.isoformat()
    snapshot = {"snapshotId": NOW.isoformat(), "createdAt": NOW.isoformat(),
                "context": _context(), "quotes": {"600001": raw_quote}}
    answers = {"snapshotId": NOW.isoformat(),
               "regime": {"choice": "修复观察", "confidence": 0.8},
               "action": {"choice": "允许模拟小仓评估", "confidence": 0.8},
               "portfolio": {"posture": "平衡", "positionPct": 0.2,
                             "orders": {"600001": {"action": "模拟买入", "confidence": 0.9,
                                                  "reason": "订阅集成测试"}}}}
    snapshot_path = tmp_path / "snapshot.json"
    answers_path = tmp_path / "answers.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    answers_path.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
    return snapshot_path, answers_path


class FakeNotifier:
    enabled = True

    def __init__(self, fail=False):
        self.messages = []
        self.flushed = False
        self.fail = fail

    def send(self, title, content):
        if self.fail:
            raise RuntimeError("push unavailable")
        self.messages.append((title, content))

    def flush(self, timeout):
        self.flushed = True


def test_subscription_answers_reuse_local_paper_without_api(tmp_path):
    snapshot, answers = _files(tmp_path)
    notifier = FakeNotifier()
    result = submit_answers(snapshot, answers, tmp_path / "decisions.jsonl", tmp_path / "paper.sqlite",
                            clock=lambda: NOW, notifier=notifier)
    assert result["ok"] is True and result["reconciled"] is True
    assert result["model"] == "kimi-work-subscription" and result["liveTrading"] is False
    assert len(result["execution"]["fills"]) == 1
    assert notifier.flushed is True and result["notificationEnabled"] is True
    assert len(notifier.messages) == 2
    assert "📊" in notifier.messages[0][0] and "订阅集成测试" in notifier.messages[0][1]
    assert "模拟买入" in notifier.messages[1][0] and "200 股 @9.00" in notifier.messages[1][1]
    conn = sqlite3.connect(tmp_path / "paper.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM fills WHERE account=?", (AI_ACCOUNT,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0] == 0
    conn.close()


def test_notification_failure_does_not_roll_back_paper_fill(tmp_path):
    snapshot, answers = _files(tmp_path)
    result = submit_answers(snapshot, answers, tmp_path / "decisions.jsonl", tmp_path / "paper.sqlite",
                            clock=lambda: NOW, notifier=FakeNotifier(fail=True))
    assert result["ok"] is True and result["notificationError"] == "push unavailable"
    assert len(result["execution"]["fills"]) == 1


def test_subscription_snapshot_mismatch_and_staleness_are_rejected(tmp_path):
    snapshot, answers = _files(tmp_path)
    value = json.loads(answers.read_text(encoding="utf-8"))
    value["snapshotId"] = "wrong"
    answers.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="编号不一致"):
        submit_answers(snapshot, answers, tmp_path / "decisions.jsonl", tmp_path / "paper.sqlite",
                       clock=lambda: NOW)

    _files(tmp_path)
    with pytest.raises(ValueError, match="有效期"):
        submit_answers(snapshot, answers, tmp_path / "decisions.jsonl", tmp_path / "paper.sqlite",
                       clock=lambda: NOW + timedelta(seconds=181))


def test_journal_reloads_external_subscription_records(tmp_path):
    path = tmp_path / "decisions.jsonl"
    journal = DecisionJournal(path, external=True)
    path.write_text('{"id":"AI-SUB-1","ts":"2026-10-28T10:30:00"}\n', encoding="utf-8")
    assert [row["id"] for row in journal.records()] == ["AI-SUB-1"]
    assert journal.status()["enabled"] is True
    assert journal.status()["mode"] == "subscription"


def test_snapshot_waits_for_market_breadth_before_eligibility(monkeypatch, tmp_path):
    class Market:
        def __init__(self):
            self.ready = False
            self.blocking_calls = 0

        def payload(self, blocking=False):
            if blocking:
                self.blocking_calls += 1
                self.ready = True
            return {"marketBreadth": {"complete": self.ready, "stale": False},
                    "sectorBreadth": {"complete": self.ready, "stale": False}}

    class Connection:
        def close(self):
            pass

    class Paper:
        def _connect(self):
            return Connection()

        def _reconcile(self, conn, now):
            pass

        def _candidate_codes(self, conn, context):
            return []

    class Service:
        def __init__(self):
            self.market = Market()
            self.paper = Paper()

        def payload(self):
            return {"snapshot": {"phase": "盘中"}, **self.market.payload()}

        def _paper_quotes(self, codes):
            return {}

    service = Service()
    monkeypatch.setattr("dashboard.subscription_bridge.DashboardService", lambda: service)
    result = create_snapshot(tmp_path / "snapshot.json", clock=lambda: NOW)
    assert service.market.blocking_calls == 1
    assert result["eligible"] is False
    assert result["reason"].startswith("候选代码为空")


def test_analysis_push_is_detailed_with_names_sectors_and_next_steps():
    from dashboard.subscription_bridge import notify_result
    record = {"id": "AI-1", "ts": "2026-11-02T10:58:38", "regime": "修复观察", "action": "空仓等待",
              "confidence": 0.58, "market": "全市场上涨 2879 家", "sector": "行业上涨 36 个",
              "analysis": {"market": "修复延续、没有主线", "sectors": "传媒娱乐有量", "picks": "一只都不碰",
                           "forecast": "涨停 50-65 震荡", "review": "上一轮方向对", "plan": "盯涨停 70 家"}}
    latest = {"portfolio": {"posture": "防守", "positionPct": 0,
                            "orders": {"002292": {"action": "观察", "confidence": 0.72, "reason": "已封板",
                                                  "sector": "传媒娱乐", "next": "明天竞价高开 3% 以内且放量再评估"}}},
              "execution": {"fills": [], "skipped": []}}
    quotes = {"002292": Quote(code="002292", name="奥飞娱乐", price=8.71, pre_close=7.92, open=7.95,
                              high=8.71, low=7.9, volume=1, amount=6.59e8, limit_up=8.71, limit_down=7.13, ts=NOW)}
    notifier = FakeNotifier()
    notify_result(notifier, record, latest, True, quotes=quotes)
    title, body = notifier.messages[0]
    assert "空仓等待" in title
    for text in ("奥飞娱乐", "传媒娱乐", "8.71", "+9.97%", "已封板", "明天竞价高开 3% 以内且放量再评估",
                 "修复延续、没有主线", "传媒娱乐有量", "涨停 50-65 震荡", "盯涨停 70 家", "上一轮方向对",
                 "闸门关", "09:30-10:30"):
        assert text in body, text


def test_portfolio_answer_keeps_sector_and_next_step():
    from dashboard.kimi_judge import _convert_portfolio
    criteria = {"posture": ["防守"], "positionPct": [0], "codes": {"002292": "奥飞娱乐"}, "actions": ["观察"]}
    out = _convert_portfolio(criteria, {"posture": "防守", "positionPct": 0, "orders": {
        "002292": {"action": "观察", "confidence": 0.7, "reason": "封板", "sector": "传媒娱乐", "next": "竞价再看"}}})
    assert out["orders"]["002292"]["sector"] == "传媒娱乐" and out["orders"]["002292"]["next"] == "竞价再看"


def test_only_shanghai_shenzhen_main_board_codes_reach_deepseek():
    # dj 2026-11-02：只看沪深主板，创业板/科创板/北交所买不了
    from dashboard.subscription_bridge import _main_board_only
    assert _main_board_only(["600779", "300085", "688981", "002292", "000559", "301234", "920001", "830799", "603081"]) \
        == ["600779", "002292", "000559", "603081"]
