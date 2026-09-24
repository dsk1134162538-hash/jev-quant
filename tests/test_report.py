from datetime import date, datetime

from factories import quote
from jevq import db
from jevq.data import custom_store
from jevq.ledger import Ledger
from jevq.report import write_report


def test_report(tmp_path):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    t = datetime(2026, 9, 23, 14, 50)
    ledger.buy("main", quote("600001", pct=-1.0), t, "低吸-1.0%")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail) VALUES (?, ?, ?, ?)",
                 (t.isoformat(), "600002", "reject", "新增减持：2026-09-01 甲:减持公告"))
    path = write_report(conn, ledger, date(2026, 9, 23), {"600001": 10.0}, "中性", "偏弱", tmp_path)
    text = path.read_text(encoding="utf-8")
    assert path.name == "2026-09-23.md"
    assert "600001" in text and "低吸-1.0%" in text
    assert "600002" in text and "新增减持" in text
    assert "外围：中性" in text and "情绪：偏弱" in text
    # 四个账户（低吸 main/shadow + 扫首板 board_main/board_shadow）各写一行净值记录
    assert conn.execute("SELECT COUNT(*) FROM equity").fetchone()[0] == 4
    assert "### 低吸" in text and "### 扫首板" in text


def test_report_includes_board_bucket_distribution(tmp_path):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    t = datetime(2026, 9, 23, 14, 50)
    from jevq.data.board_events import record_day
    record_day(conn, date(2026, 9, 23), [
        {"代码": "600001", "名称": "甲", "首次封板时间": "093000", "连板数": 1},
        {"代码": "600002", "名称": "乙", "首次封板时间": "103500", "连板数": 1},
    ], {})
    path = write_report(conn, ledger, date(2026, 9, 23), {}, "中性", "正常", tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "## 今日首板时段分布" in text
    assert "| 早盘 | 1 |" in text
    assert "| 上午 | 1 |" in text


def test_report_shows_names(tmp_path):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    t = datetime(2026, 9, 23, 14, 50)
    ledger.buy("main", quote("600001", pct=-1.0), t, "低吸")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail) VALUES (?, ?, ?, ?)",
                 (t.isoformat(), "600002", "reject", "新增减持"))
    path = write_report(conn, ledger, date(2026, 9, 23), {"600001": 10.0}, "中性", "正常", tmp_path,
                        names={"600001": "股票甲", "600002": "股票乙"})
    text = path.read_text(encoding="utf-8")
    assert "| 600001 | 股票甲 |" in text
    assert "| 600002 | 股票乙 |" in text
    assert "600001 股票甲" in text                   # 持仓列


def test_report_includes_active_custom_strategy_account_and_name(tmp_path):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    day = date(2026, 9, 24)
    spec = {"name": "尾盘弱转强", "capital": 30000,
            "select": {"conditions": [{"field": "pct", "op": "between", "value": [-2, 0]}]},
            "entry": {"conditions": []},
            "exits": [{"when": {"conditions": [{"field": "pct", "op": "<=", "value": -3}]}, "reason": "止损"}],
            "jev": {"notices": True, "news": True}}
    custom_store.save(conn, spec, date(2026, 9, 23), [date(2026, 9, 23), day], sid=None)
    ledger.ensure_account("c1_main", 30000)
    ledger.ensure_account("c1_shadow", 30000)
    text = write_report(conn, ledger, day, {}, "中性", "正常", tmp_path).read_text(encoding="utf-8")
    assert "### 尾盘弱转强" in text
    assert "尾盘弱转强·主(Jev)" in text and "尾盘弱转强·影子" in text
    # 内置两个策略 + 新增的自定义策略各两个账户 = 6 条净值记录
    assert conn.execute("SELECT COUNT(*) FROM equity").fetchone()[0] == 6


def test_report_check_table_has_strategy_column(tmp_path):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    t = datetime(2026, 9, 23, 9, 50).isoformat()
    conn.executemany("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES (?, ?, ?, ?, ?)", [
        (t, "600002", "reject", "低吸否决", "lowbuy"),
        (t, "600003", "skip", "快照已封死，视为排队未买到", "board"),
        (t, "600004", "reject", "旧记录", None),
    ])
    text = write_report(conn, ledger, date(2026, 9, 23), {}, "中性", "正常", tmp_path).read_text(encoding="utf-8")
    assert "| 时间 | 策略 | 代码 | 名称 | 结果 | 原因 |" in text
    assert "| 09:50 | 低吸 | 600002 |" in text
    assert "| 09:50 | 扫首板 | 600003 |" in text
    assert "| 09:50 | 低吸 | 600004 |" in text                 # 老记录没有 strategy，算低吸
