from datetime import date

import daemon


def test_after_close_runs_vibe_sync_and_survives_failure(monkeypatch):
    logs, calls = [], []
    monkeypatch.setattr(daemon, "SYNC_ENABLED", False)
    monkeypatch.setattr(daemon, "VIBE_ENABLED", True)
    monkeypatch.setattr(daemon, "file_log", logs.append)

    def boom(conn, today, kb=None):
        calls.append(today)
        raise RuntimeError("vibe down")
    monkeypatch.setattr(daemon.vibe, "sync_day", boom)
    monkeypatch.setattr(daemon.vibe, "load_kb", lambda: None)
    daemon._default_after_close(object(), date(2026, 10, 9))
    assert calls == [date(2026, 10, 9)]
    assert any("Vibe" in m and "vibe down" in m for m in logs)


def test_after_close_skips_vibe_when_disabled(monkeypatch):
    monkeypatch.setattr(daemon, "SYNC_ENABLED", False)
    monkeypatch.setattr(daemon, "VIBE_ENABLED", False)
    monkeypatch.setattr(daemon.vibe, "sync_day", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    daemon._default_after_close(object(), date(2026, 10, 9))
