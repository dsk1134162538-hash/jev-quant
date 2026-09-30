import gzip
import sqlite3
from datetime import datetime

import pytest

from scripts import home_install


def _packed_db(tmp_path):
    src = tmp_path / "src.sqlite"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE closes(date TEXT, code TEXT, close REAL)")
    conn.execute("INSERT INTO closes VALUES ('2026-09-29', '600001', 10.0)")
    conn.commit()
    conn.close()
    gz = tmp_path / "jevq.sqlite.gz"
    gz.write_bytes(gzip.compress(src.read_bytes()))
    return gz


def test_in_session_blocks_weekday_trading_hours_only():
    assert home_install.in_session(datetime(2026, 9, 30, 10, 0))
    assert not home_install.in_session(datetime(2026, 9, 30, 15, 6))
    assert not home_install.in_session(datetime(2026, 10, 3, 10, 0))


def test_ensure_db_from_local_file(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    home_install.ensure_db(run_dir, _packed_db(tmp_path))
    conn = sqlite3.connect(run_dir / "data" / "jevq.sqlite")
    assert conn.execute("SELECT code FROM closes").fetchall() == [("600001",)]
    conn.close()


def test_ensure_db_never_overwrites(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "data").mkdir(parents=True)
    (run_dir / "data" / "jevq.sqlite").write_bytes(b"keep")
    home_install.ensure_db(run_dir, _packed_db(tmp_path))
    assert (run_dir / "data" / "jevq.sqlite").read_bytes() == b"keep"


def test_ensure_db_rejects_corrupt_file(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    bad = tmp_path / "bad.gz"
    bad.write_bytes(gzip.compress(b"not a database" * 100))
    with pytest.raises(SystemExit):
        home_install.ensure_db(run_dir, bad)
    assert list((run_dir / "data").iterdir()) == []
