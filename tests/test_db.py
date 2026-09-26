import sqlite3

from jevq import db


def test_migrates_old_schema(tmp_path):
    path = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, code TEXT, verdict TEXT, detail TEXT)")
    old.execute("CREATE TABLE notice_verdicts(id TEXT PRIMARY KEY, label TEXT, confidence REAL, source TEXT)")
    old.execute("INSERT INTO decisions(ts, code, verdict, detail) VALUES ('t', '600001', 'pass', 'ok')")
    old.commit()
    old.close()
    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
    assert "trace" in cols
    assert "strategy" in cols
    assert "probs" in {r[1] for r in conn.execute("PRAGMA table_info(notice_verdicts)")}
    assert conn.execute("SELECT code, trace FROM decisions").fetchone() == ("600001", None)
    db.connect(path)                                   # 再次连接不报错（迁移幂等）


def test_custom_strategies_table_and_version_columns_created(tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(custom_strategies)")}
    assert cols == {"id", "version", "name", "spec", "created", "active_from", "enabled"}
    assert "version" in {r[1] for r in conn.execute("PRAGMA table_info(fills)")}
    assert "version" in {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}


def test_migrates_old_schema_adds_version_columns(tmp_path):
    path = str(tmp_path / "old2.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE fills(id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, code TEXT, "
               "side TEXT, price REAL, shares INTEGER, fee REAL, ts TEXT, reason TEXT)")
    old.execute("CREATE TABLE decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, code TEXT, "
               "verdict TEXT, detail TEXT)")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert "version" in {r[1] for r in conn.execute("PRAGMA table_info(fills)")}
    assert "version" in {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}


def test_file_db_uses_wal(tmp_path):
    conn = db.connect(str(tmp_path / "w.sqlite"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_connect_readonly_reads_but_cannot_write(tmp_path):
    import pytest
    path = str(tmp_path / "r.sqlite")
    conn = db.connect(path)
    conn.execute("INSERT INTO cash VALUES ('main', 1.0)")
    conn.commit()
    ro = db.connect_readonly(path)
    assert ro.execute("SELECT amount FROM cash").fetchone() == (1.0,)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO cash VALUES ('shadow', 2.0)")
    ro.close()
    conn.close()
    ro = db.connect_readonly(path)                      # 写连接已关闭（WAL 文件被清理）后也能只读打开
    assert ro.execute("SELECT amount FROM cash").fetchone() == (1.0,)


def test_migrates_old_closes_adds_volume_column_idempotently(tmp_path):
    path = str(tmp_path / "old3.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE closes(date TEXT, code TEXT, close REAL, PRIMARY KEY(date, code))")
    old.execute("INSERT INTO closes VALUES ('2026-09-23', '600001', 10.0)")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert "volume" in {r[1] for r in conn.execute("PRAGMA table_info(closes)")}
    assert conn.execute("SELECT close, volume FROM closes").fetchone() == (10.0, None)   # 旧行的量留空
    conn.close()
    conn = db.connect(path)                            # 再连一次不报错（迁移幂等）
    pk = [r[1] for r in conn.execute("PRAGMA table_info(closes)") if r[5]]
    assert pk == ["date", "code"]                      # 主键不变
