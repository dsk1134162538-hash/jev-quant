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
    ledger.buy("rebound_main", quote("600001", pct=-1.0), t, "超跌-1.0%")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES (?, ?, ?, ?, ?)",
                 (t.isoformat(), "600002", "reject", "新增减持：2026-09-01 甲:减持公告", "rebound"))
    path = write_report(conn, ledger, date(2026, 9, 23), {"600001": 10.0}, "中性", "偏弱", tmp_path)
    text = path.read_text(encoding="utf-8")
    assert path.name == "2026-09-23.md"
    assert "600001" in text and "超跌-1.0%" in text
    assert "600002" in text and "新增减持" in text
    assert "外围：中性" in text and "情绪：偏弱" in text
    # 四个账户（扫首板 board_main/board_shadow + 超跌反弹 rebound_main/rebound_shadow）各写一行净值记录
    assert conn.execute("SELECT COUNT(*) FROM equity").fetchone()[0] == 14
    assert "### 扫首板" in text and "### 超跌反弹" in text


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
    ledger.buy("rebound_main", quote("600001", pct=-1.0), t, "超跌")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES (?, ?, ?, ?, ?)",
                 (t.isoformat(), "600002", "reject", "新增减持", "rebound"))
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
    assert conn.execute("SELECT COUNT(*) FROM equity").fetchone()[0] == 16


def test_report_after_lowbuy_retired_keeps_history_names_only(tmp_path):
    """9-23 版低吸下线：main/shadow 不再写净值；前一天它卖出的卖飞回看照常，账户显示「已下线」。
    10-09 低吸以新账户 lowbuy_main/lowbuy_shadow 重新上线，有自己的一节。"""
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO cash VALUES ('main', 30500.0), ('shadow', 30200.0)")
    conn.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
                 "VALUES ('main', '600009', 'sell', 10.0, 1000, 12.0, '2026-09-22T10:00:00', '高开止盈')")
    fid = conn.execute("SELECT id FROM fills").fetchone()[0]
    conn.execute("INSERT INTO sell_followups VALUES (?, 'main', '600009', '2026-09-22', 10.0, 10.2, 10.1, "
                 "'2026-09-23', 10.3, 10.2)", (fid,))
    conn.commit()
    ledger = Ledger(conn)
    text = write_report(conn, ledger, date(2026, 9, 23), {}, "中性", "正常", tmp_path).read_text(encoding="utf-8")
    assert "### 低吸" in text and "### 扫首板" in text and "### 超跌反弹" in text and "### 情绪轮动" in text
    assert {a for (a,) in conn.execute("SELECT account FROM equity")} == {
        "board_main", "board_shadow", "rebound_main", "rebound_shadow", "lowbuy_main", "lowbuy_shadow", "regime_main",
        "aiboard_main", "aiboard_shadow", "relay_main", "relay_shadow", "wts_main", "wts_shadow", "ice_main"}
    assert "| 2026-09-22 | 低吸·主(已下线) | 600009 |" in text


def test_report_check_table_has_strategy_column(tmp_path):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    t = datetime(2026, 9, 23, 9, 50).isoformat()
    conn.executemany("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES (?, ?, ?, ?, ?)", [
        (t, "600002", "reject", "超跌否决", "rebound"),
        (t, "600003", "skip", "快照已封死，视为排队未买到", "board"),
    ])
    text = write_report(conn, ledger, date(2026, 9, 23), {}, "中性", "正常", tmp_path).read_text(encoding="utf-8")
    assert "| 时间 | 策略 | 代码 | 名称 | 结果 | 原因 |" in text
    assert "| 09:50 | 超跌反弹 | 600002 |" in text
    assert "| 09:50 | 扫首板 | 600003 |" in text


def test_report_check_table_labels_null_strategy_as_unknown(tmp_path):
    """decisions.strategy 是 NULL 的旧数据行，检查表里不该露出字面的 None。"""
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    t = datetime(2026, 9, 23, 9, 50).isoformat()
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail) VALUES (?, ?, ?, ?)",
                (t, "600004", "reject", "旧数据没打策略标签"))
    text = write_report(conn, ledger, date(2026, 9, 23), {}, "中性", "正常", tmp_path).read_text(encoding="utf-8")
    assert "| 09:50 | 未知 | 600004 |" in text


def test_report_shows_verdicts_in_chinese(tmp_path):
    """检查记录的结果列显示「否决/通过」，不露出 reject/pass。"""
    from datetime import datetime
    from jevq import db
    from jevq.ledger import Ledger
    from jevq.report import write_report
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    t = datetime(2026, 9, 24, 14, 50)
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail) VALUES (?, ?, ?, ?)",
                 (t.isoformat(), "600002", "reject", "新增减持"))
    path = write_report(conn, ledger, t.date(), {}, "中性", "正常", tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "| 否决 |" in text and "| reject |" not in text


def test_report_notice_risk_stats(tmp_path):
    import json
    from jevq.report import notice_risk_stats
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    t = datetime(2026, 9, 28, 14, 50).isoformat()
    s1 = {"id": "AN1", "category": "减持", "verdict": "reject", "body_read": True}
    s2 = {"id": "AN2", "category": "回购", "verdict": "pass", "body_read": False}
    s3 = {"id": "AN3", "category": "减持", "verdict": "unsure", "body_read": True}
    rows = [(t, "600001", "reject", "减持·高风险", json.dumps({"notices": {"suspects": [s1, s2], "others": 3, "skipped": []}})),
            (t, "600002", "reject", "减持·拿不准", json.dumps({"notices": {"suspects": [s3, s1], "others": 0, "skipped": []}})),
            (t, "600003", "pass", "旧记录", json.dumps({"notices": [{"title": "旧"}]}))]
    conn.executemany("INSERT INTO decisions(ts, code, verdict, detail, trace) VALUES (?, ?, ?, ?, ?)", rows)
    st = notice_risk_stats(conn, datetime(2026, 9, 28).date())
    assert st == {"judged": 3, "reject": 1, "unsure": 1, "unread": 1, "by_category": {"减持": 2, "回购": 1}}
    text = write_report(conn, ledger, datetime(2026, 9, 28).date(), {}, "中性", "正常", tmp_path).read_text(encoding="utf-8")
    assert "## 公告风险统计" in text and "| 减持 | 2 |" in text


def test_report_reference_articles_line(tmp_path):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    day = date(2026, 9, 23)
    text = write_report(conn, ledger, day, {}, "中性", "正常", tmp_path).read_text(encoding="utf-8")
    assert "今日参考文章：未取到" in text
    arts = [{"title": "晚报", "link": "https://hi189.com/1.html"}]
    text = write_report(conn, ledger, day, {}, "中性", "正常", tmp_path, articles=arts).read_text(encoding="utf-8")
    assert "今日参考文章：晚报（https://hi189.com/1.html）" in text


def test_report_marks_midday_article_as_record_only_while_switch_off(tmp_path, monkeypatch):
    from jevq.data import hi189
    monkeypatch.setattr(hi189, "HI189_MIDDAY_JUDGE", False)
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    arts = [{"title": "2026/09/23(晚) x", "link": "https://e"}, {"title": "2026/09/23-(中) x", "link": "https://m"}]
    text = write_report(conn, ledger, date(2026, 9, 23), {}, "中性", "正常", tmp_path, articles=arts).read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("今日参考文章"))
    assert "(晚) x（https://e）" in line and "(中) x（https://m）【只记录，未参与判断】" in line
    assert "(晚) x（https://e）【" not in line
