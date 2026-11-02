import sqlite3

from dashboard import minute_archive as ma
from research import board_touch_study as st


def test_next_day_exits_cover_open_ten_close_and_limit_orders():
    bars = [("09:30", 9.8, 1), ("09:45", 10.1, 2), ("10:00", 10.0, 3), ("14:59", 9.7, 4)]
    ex = st.next_day_exits(10.0, bars)
    assert ex == {"开盘": -2.15, "10点": -0.15, "收盘": -3.15, "保本挂单": -0.15, "+3%挂单": -3.15}


def test_samples_mark_touch_time_speed_seal_and_next_day_exits(tmp_path):
    db = tmp_path / "m.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(ma.SCHEMA)
    conn.executemany("INSERT INTO minute_bars VALUES (?,?,?,?,?)", [
        ("2026-11-02", "600001", "09:30", 10.2, 100), ("2026-11-02", "600001", "09:31", 10.75, 300),
        ("2026-11-02", "600001", "09:33", 10.95, 600), ("2026-11-02", "600001", "14:59", 11.0, 900),
        ("2026-11-03", "600001", "09:30", 11.3, 50), ("2026-11-03", "600001", "14:59", 11.1, 90)])
    conn.executemany("INSERT INTO minute_days VALUES (?,?,?,?,?,?,?,?)", [
        ("2026-11-02", "600001", "甲", 10.0, 11.0, 11.0, 11.0, 4),
        ("2026-11-03", "600001", "甲", 11.0, 12.1, 11.3, 11.1, 2)])
    conn.commit()
    conn.close()
    rows = st.samples(db)
    assert len(rows) == 1
    s = rows[0]
    assert (s["touch"], s["speed7to9"], s["sealed"], s["instantSeal"]) == ("09:33", 1, True, False)
    assert s["exits"]["开盘"] == round((11.3 / 10.95 - 1) * 100 - st.FEE, 2)
    assert "封住率100.0%" in st.report(rows)
