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
    assert not home_install.in_session(datetime(2026, 9, 30, 15, 31))
    assert not home_install.in_session(datetime(2026, 9, 30, 9, 14))
    assert not home_install.in_session(datetime(2026, 10, 3, 10, 0))


def test_in_session_covers_daemon_sync_and_deploy_window_until_15_30():
    """daemon 15:05–15:20 做同步/上线（git rebase、push）：这段时间装/重启会并发 git、还可能杀掉正在 rebase 的 daemon。"""
    assert home_install.in_session(datetime(2026, 9, 30, 15, 5))
    assert home_install.in_session(datetime(2026, 9, 30, 15, 6))
    assert home_install.in_session(datetime(2026, 9, 30, 15, 20))
    assert home_install.in_session(datetime(2026, 9, 30, 15, 30))
    assert not home_install.in_session(datetime(2026, 9, 30, 15, 30, 1))


# ---- check_env ----

def _env(monkeypatch, **values):
    for name in ("TYPESAFE_API_KEY", "PUSHPLUS_TOKEN", "XIADAN_GATEWAY_TOKEN", "JEVQ_LIVE_HOST"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(home_install, "_windows_env_fallback", lambda name: None)


def test_check_env_reports_live_variables_by_presence_only(monkeypatch, capsys):
    _env(monkeypatch, TYPESAFE_API_KEY="k-secret-1", PUSHPLUS_TOKEN="p-secret-2",
         XIADAN_GATEWAY_TOKEN="g-secret-3", JEVQ_LIVE_HOST="100.64.9.9")
    assert home_install.check_env() is True
    out = capsys.readouterr().out
    assert "XIADAN_GATEWAY_TOKEN：已设置" in out and "JEVQ_LIVE_HOST：已设置" in out
    for secret in ("k-secret-1", "p-secret-2", "g-secret-3", "100.64.9.9"):
        assert secret not in out      # 只说有没有，绝不打印值


def test_check_env_missing_live_variables_hint_live_needs_them_but_do_not_fail(monkeypatch, capsys):
    _env(monkeypatch, TYPESAFE_API_KEY="k", PUSHPLUS_TOKEN="p")
    assert home_install.check_env() is True         # 实盘变量没设不算失败：模拟盘照样能装
    lines = {line.split("：")[0]: line for line in capsys.readouterr().out.splitlines()}
    assert "实盘需要" in lines["XIADAN_GATEWAY_TOKEN"] and "没" in lines["XIADAN_GATEWAY_TOKEN"]
    assert "实盘需要" in lines["JEVQ_LIVE_HOST"] and "没" in lines["JEVQ_LIVE_HOST"]


def test_check_env_still_fails_when_a_required_variable_is_missing(monkeypatch, capsys):
    _env(monkeypatch, TYPESAFE_API_KEY="k", XIADAN_GATEWAY_TOKEN="g", JEVQ_LIVE_HOST="h")
    assert home_install.check_env() is False
    assert "PUSHPLUS_TOKEN：没有" in capsys.readouterr().out


def test_check_env_reads_live_variables_from_registry_fallback(monkeypatch, capsys):
    _env(monkeypatch, TYPESAFE_API_KEY="k", PUSHPLUS_TOKEN="p")
    monkeypatch.setattr(home_install, "_windows_env_fallback",
                        lambda name: "reg-value" if name == "XIADAN_GATEWAY_TOKEN" else None)
    home_install.check_env()
    out = capsys.readouterr().out
    assert "XIADAN_GATEWAY_TOKEN：已设置" in out and "reg-value" not in out


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
