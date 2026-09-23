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
    assert "probs" in {r[1] for r in conn.execute("PRAGMA table_info(notice_verdicts)")}
    assert conn.execute("SELECT code, trace FROM decisions").fetchone() == ("600001", None)
    db.connect(path)                                   # 再次连接不报错（迁移幂等）
