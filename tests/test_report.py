from datetime import date, datetime

from factories import quote
from jevq import db
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
