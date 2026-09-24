import csv
from datetime import date

from jevq import db
from jevq.sync.export import export_day

TODAY = date(2026, 9, 23)


def conn():
    return db.connect(":memory:")


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def test_export_day_writes_headers_when_everything_empty(tmp_path):
    c = conn()
    paths = export_day(c, TODAY, out_dir=str(tmp_path / "exports"))
    assert len(paths) == 5
    for p in paths:
        assert p.exists()
    rows = read_csv(tmp_path / "exports" / "fills.csv")
    assert rows == [["id", "account", "code", "side", "price", "shares", "fee", "ts", "reason"]]


def test_export_day_fills_content_and_order(tmp_path):
    c = conn()
    c.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
             "VALUES ('main', '600001', 'buy', 10.0, 100, 5.1, '2026-09-23T09:31:00', 'r2')")
    c.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
             "VALUES ('main', '600002', 'sell', 11.0, 200, 5.2, '2026-09-23T09:30:00', 'r1')")
    c.commit()
    out = tmp_path / "exports"
    export_day(c, TODAY, out_dir=str(out))
    rows = read_csv(out / "fills.csv")
    assert rows[0] == ["id", "account", "code", "side", "price", "shares", "fee", "ts", "reason"]
    # 按 id 排序，不是按 ts：第一条插入的 id 更小，即使它的 ts 更晚
    assert [r[2] for r in rows[1:]] == ["600001", "600002"]


def test_export_day_positions_and_equity(tmp_path):
    c = conn()
    c.execute("INSERT INTO positions VALUES ('main', '600001', 100, 1005.1, 10.0, '2026-09-23')")
    c.execute("INSERT INTO equity(date, account, value) VALUES (?, 'main', 30000.0)", (TODAY.isoformat(),))
    c.commit()
    out = tmp_path / "exports"
    export_day(c, TODAY, out_dir=str(out))
    pos_rows = read_csv(out / "positions.csv")
    assert pos_rows[1] == ["main", "600001", "100", "1005.1", "10.0", "2026-09-23"]
    eq_rows = read_csv(out / "equity.csv")
    assert eq_rows[1] == ["2026-09-23", "main", "30000.0"]


def test_export_day_decisions_excludes_trace_and_filters_by_day(tmp_path):
    c = conn()
    c.execute("INSERT INTO decisions(ts, code, verdict, detail, trace, strategy, version) "
             "VALUES ('2026-09-23T10:00:00', '600001', 'buy', 'd1', '{\"big\": true}', 'lowbuy', 1)")
    c.execute("INSERT INTO decisions(ts, code, verdict, detail, trace, strategy, version) "
             "VALUES ('2026-09-22T10:00:00', '600002', 'skip', 'd2', '{}', 'lowbuy', 1)")
    c.commit()
    out = tmp_path / "exports"
    export_day(c, TODAY, out_dir=str(out))
    rows = read_csv(out / "decisions" / "2026-09-23.csv")
    assert rows[0] == ["ts", "strategy", "version", "code", "verdict", "detail"]
    assert len(rows) == 2                       # 只有今天那一条，且没有 trace 列
    assert rows[1] == ["2026-09-23T10:00:00", "lowbuy", "1", "600001", "buy", "d1"]


def test_export_day_board_events(tmp_path):
    c = conn()
    c.execute("INSERT INTO board_events(date, code, name, first_seal, bucket, concept, "
             "next_open_pct, next_close_pct, next_board) VALUES "
             "('2026-09-23', '600001', '甲', '093000', '早盘', '概念', 1.0, 2.0, 0)")
    c.commit()
    out = tmp_path / "exports"
    export_day(c, TODAY, out_dir=str(out))
    rows = read_csv(out / "board_events.csv")
    assert rows[1][:3] == ["2026-09-23", "600001", "甲"]


def test_export_day_is_idempotent_and_rewrites_whole_file(tmp_path):
    c = conn()
    c.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
             "VALUES ('main', '600001', 'buy', 10.0, 100, 5.1, '2026-09-23T09:31:00', 'r')")
    c.commit()
    out = tmp_path / "exports"
    export_day(c, TODAY, out_dir=str(out))
    first = read_csv(out / "fills.csv")
    export_day(c, TODAY, out_dir=str(out))       # 重复运行
    second = read_csv(out / "fills.csv")
    assert first == second
