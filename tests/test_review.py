"""每周核对（用户 2026-10-19）。"""
from datetime import date

from jevq import config, db, review


def _fill(conn, account, code, side, price, ts, shares=1000, fee=5.0):
    conn.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) VALUES (?, ?, ?, ?, ?, ?, ?, 'x')",
                 (account, code, side, price, shares, fee, ts))


def test_round_trips_pair_buys_with_sells_and_skip_old_or_open():
    conn = db.connect(":memory:")
    _fill(conn, "rebound_main", "600001", "buy", 10.0, "2026-10-14T10:00:00")      # 改规则前买的：不算
    _fill(conn, "rebound_main", "600001", "sell", 9.7, "2026-10-16T09:31:00")
    _fill(conn, "rebound_main", "600002", "buy", 10.0, "2026-10-16T09:31:00")
    _fill(conn, "rebound_main", "600002", "sell", 10.3, "2026-10-19T10:00:00")
    _fill(conn, "rebound_main", "600003", "buy", 10.0, "2026-10-19T10:00:00")      # 还没卖
    trips = review.round_trips(conn, "rebound_main", date(2026, 10, 16))
    assert [t["code"] for t in trips] == ["600002"] and abs(trips[0]["ret"] - 2.9) < 0.1


def test_is_week_end_uses_calendar():
    cal = [date(2026, 10, d) for d in (19, 20, 21, 22, 23, 26)]
    assert review.is_week_end(date(2026, 10, 23), cal) and not review.is_week_end(date(2026, 10, 22), cal)
    assert review.is_week_end(date(2026, 10, 23), None)


def test_weekly_review_flags_a_clearly_worse_account(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_MIN_TRADES", 3)
    conn = db.connect(":memory:")
    for k in range(4):
        _fill(conn, "rebound_main", f"60000{k}", "buy", 10.0, f"2026-10-1{6 + k // 3}T10:0{k}:00")
        _fill(conn, "rebound_main", f"60000{k}", "sell", 9.7, f"2026-10-2{k // 2}T10:0{k}:00")
    lines, flagged = review.weekly_review(conn, date(2026, 10, 23))
    row = next(ln for ln in lines if ln.startswith("| 超跌·主"))
    assert flagged and "建议复盘" in row and "4 笔 / 0%" in row
    assert "笔数还少" in next(ln for ln in lines if ln.startswith("| 低吸"))
    assert "建议复盘" in review.push_text(lines)


def test_run_weekly_review_appends_to_report_and_pushes(tmp_path):
    import run
    conn = db.connect(":memory:")
    report = tmp_path / "r.md"
    report.write_text("# 报告", encoding="utf-8")
    sent, logs = [], []

    class N:
        def send(self, t, c):
            sent.append(t)
    cal = [date(2026, 10, 23), date(2026, 10, 26)]
    run._weekly_review(conn, date(2026, 10, 23), cal, report, N(), logs.append)
    assert "## 每周核对" in report.read_text(encoding="utf-8") and sent == ["每周核对"]
    run._weekly_review(conn, date(2026, 10, 22), cal + [date(2026, 10, 22)], report, N(), logs.append)
    assert sent == ["每周核对"]                                  # 不是周末最后一个交易日：不发
