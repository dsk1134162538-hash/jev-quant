"""自定义策略接入推送：收盘小结覆盖自定义账户；自定义主账户买入推送带命中条件与 Jev 明细。
（运行循环本身的驱动/隔离/只卖测试在 tests/test_run.py 末尾。）"""
import json
from datetime import date, datetime

from jevq import db
from jevq.data import custom_store
from jevq.ledger import Ledger
from jevq.models import Fill
from jevq.notify_content import buy_push_content, close_summary_content

SPEC = {
    "name": "测试策略", "capital": 50000,
    "select": {"mode": "all", "conditions": [{"field": "pct", "op": "between", "value": [-3, 0]}]},
    "entry": {"mode": "all", "conditions": []},
    "exits": [{"when": {"mode": "all", "conditions": [{"field": "pct", "op": "<=", "value": -5}]},
               "reason": "止损"}],
    "jev": {"notices": True, "news": True},
}
DAYS = [date(2026, 9, 23), date(2026, 9, 24)]


def _conn_with_c1():
    conn = db.connect(":memory:")
    assert custom_store.save(conn, SPEC, date(2026, 9, 23), DAYS) == ("c1", 1)      # 09-24 生效
    return conn


def test_close_summary_includes_custom_accounts_with_names_and_own_baseline():
    conn = _conn_with_c1()
    ledger = Ledger(conn)
    ledger.ensure_account("c1_main", 50000)
    ledger.ensure_account("c1_shadow", 50000)
    content = close_summary_content(ledger, date(2026, 9, 24), {}, conn)
    lines = content.splitlines()
    assert len(lines) == 9                                     # 内置 7 个 + c1 主/影子
    assert "测试策略·主(Jev) 净值 50,000.00（+0.00%）今日成交 0 笔" in lines
    assert "测试策略·影子 净值 50,000.00（+0.00%）今日成交 0 笔" in lines


def test_close_summary_without_conn_keeps_builtin_only():
    conn = _conn_with_c1()
    assert len(close_summary_content(Ledger(conn), date(2026, 9, 24), {}).splitlines()) == 7


def test_buy_push_for_custom_main_includes_hits_and_jev():
    conn = _conn_with_c1()
    trace = {"strategy": "c1", "version": 1,
             "candidate": {"name": "甲", "hits": [{"field": "当前涨幅", "value": -1.2, "desc": "当前涨幅介于-3~0%"}]},
             "notices": [],
             "news": {"label": "中性", "probabilities": {"中性": 0.8, "利空": 0.2}, "news": ["一条新闻"],
                      "confidence": 0.9},
             "outcome": {"bought": True}}
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, trace, strategy, version) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("2026-09-24T10:00:00", "600001", "pass", "测试策略", json.dumps(trace, ensure_ascii=False),
                  "c1", 1))
    conn.commit()
    f = Fill("c1_main", "600001", "buy", 9.88, 5000, 5.0, datetime(2026, 9, 24, 10, 0), "测试策略")
    content = buy_push_content(conn, f, "甲")
    assert "▍为什么选它" in content
    assert "当前涨幅介于-3~0%（当时 -1.2）" in content
    assert "▍Jev 消息面：中性" in content
