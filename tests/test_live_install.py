"""scripts/live_install.py：纯函数部分（token 选择、配置合并、找 xiadan.exe、Tailscale 地址、健康检查）。
Windows 专用的注册表写入、快捷方式、进程操作都注入替换，测试不碰真实系统、不联网。"""
import json
import subprocess
from datetime import datetime
from pathlib import Path

from scripts import live_install


def test_choose_token_prefers_existing_env_then_config_then_new():
    assert live_install.choose_token("env-tok", {"auth": {"token": "cfg-tok"}}) == "env-tok"
    assert live_install.choose_token(None, {"auth": {"token": "cfg-tok"}}) == "cfg-tok"
    new = live_install.choose_token(None, {"auth": {"token": live_install.PLACEHOLDER}})
    assert len(new) == 48 and new != live_install.PLACEHOLDER
    assert live_install.choose_token(None, {}) != live_install.choose_token(None, {})


def test_build_config_forces_safe_fields_and_keeps_the_rest():
    existing = {"host": "0.0.0.0", "port": 8000, "auth": {"enabled": False, "token": ""},
                "alerts": {"webhook_url": "https://example.com/hook", "level": "warn"},
                "query": {"copy_method": "message"}, "order": {"capture_entrust_no": True}}
    cfg = live_install.build_config(existing, "tok", Path(r"D:\ths\xiadan.exe"))
    assert cfg["host"] == "127.0.0.1" and cfg["port"] == 5000
    assert cfg["auth"] == {"enabled": True, "token": "tok"}
    assert cfg["alerts"] == {"webhook_url": "", "level": "warn"}
    assert cfg["window_monitor"]["enabled"] is True
    assert cfg["trading_app_paths"] == [str(Path(r"D:\ths\xiadan.exe"))]
    assert cfg["query"] == {"copy_method": "message"} and cfg["order"] == {"capture_entrust_no": True}


def test_find_xiadan_returns_first_existing_candidate(tmp_path):
    a, b = tmp_path / "a" / "xiadan.exe", tmp_path / "b" / "xiadan.exe"
    b.parent.mkdir()
    b.write_bytes(b"")
    assert live_install.find_xiadan([a, b]) == b
    assert live_install.find_xiadan([a]) is None


def test_write_config_sets_env_for_a_new_token_and_never_prints_it(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("XIADAN_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(live_install, "_windows_env_fallback", lambda name: None)
    written = {}
    monkeypatch.setattr(live_install, "set_user_env", lambda name, value: written.update({name: value}))
    live_install.write_config(tmp_path, Path(r"C:\ths\xiadan.exe"))

    cfg = json.loads((tmp_path / "config" / "app_config.json").read_text(encoding="utf-8"))
    token = cfg["auth"]["token"]
    assert written == {"XIADAN_GATEWAY_TOKEN": token}
    assert token not in capsys.readouterr().out


def test_write_config_reuses_env_token_without_rewriting_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XIADAN_GATEWAY_TOKEN", "already-set-token")
    written = {}
    monkeypatch.setattr(live_install, "set_user_env", lambda name, value: written.update({name: value}))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.json").write_text(json.dumps({"query": {"copy_method": "keyboard"}}),
                                                         encoding="utf-8")
    live_install.write_config(tmp_path, Path(r"C:\ths\xiadan.exe"))

    cfg = json.loads((tmp_path / "config" / "app_config.json").read_text(encoding="utf-8"))
    assert cfg["auth"]["token"] == "already-set-token" and cfg["query"] == {"copy_method": "keyboard"}
    assert written == {}
    assert "already-set-token" not in capsys.readouterr().out


def _fake_run(stdout="", returncode=0, exc=None):
    def run(*a, **kw):
        if exc:
            raise exc
        return subprocess.CompletedProcess(a, returncode, stdout=stdout, stderr="")
    return run


def test_tailscale_ip_parses_the_100_address(monkeypatch):
    monkeypatch.setattr(live_install.subprocess, "run", _fake_run("100.101.102.103\n"))
    assert live_install.tailscale_ip() == "100.101.102.103"


def test_tailscale_ip_missing_or_logged_out_gives_none(monkeypatch):
    monkeypatch.setattr(live_install.subprocess, "run", _fake_run(exc=FileNotFoundError()))
    assert live_install.tailscale_ip() is None
    monkeypatch.setattr(live_install.subprocess, "run", _fake_run("", returncode=1))
    assert live_install.tailscale_ip() is None


def test_ensure_live_host_sets_env_only_when_changed(monkeypatch, capsys):
    monkeypatch.setattr(live_install, "tailscale_ip", lambda: "100.64.1.2")
    monkeypatch.setattr(live_install, "_windows_env_fallback", lambda name: None)
    monkeypatch.setenv("JEVQ_LIVE_HOST", "100.64.1.2")
    written = {}
    monkeypatch.setattr(live_install, "set_user_env", lambda name, value: written.update({name: value}))
    live_install.ensure_live_host()
    assert written == {}
    monkeypatch.setenv("JEVQ_LIVE_HOST", "100.64.9.9")
    live_install.ensure_live_host()
    assert written == {"JEVQ_LIVE_HOST": "100.64.1.2"}
    assert "http://100.64.1.2:8766" in capsys.readouterr().out


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_wait_health_retries_until_the_gateway_answers():
    calls = []

    def get(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise ConnectionError("not up yet")
        return _Resp({"status": "success", "data": {"xiadan_running": True, "logged_in": False}})

    data = live_install.wait_health(get=get, sleep=lambda s: None)
    assert data == {"xiadan_running": True, "logged_in": False}
    assert calls[0] == "http://127.0.0.1:5000/health"


def test_wait_health_gives_up_with_none():
    def get(url, timeout):
        raise ConnectionError("down")
    assert live_install.wait_health(get=get, sleep=lambda s: None, tries=3) is None


def test_in_session_blocks_weekday_trading_hours():
    assert live_install.in_session(datetime(2026, 10, 8, 10, 0))
    assert not live_install.in_session(datetime(2026, 10, 8, 15, 31))
    assert not live_install.in_session(datetime(2026, 10, 3, 10, 0))


def test_start_bat_runs_the_gateway_from_its_own_venv(tmp_path):
    bat = live_install.write_start_bat(tmp_path)
    text = bat.read_bytes().decode("ascii")
    assert ".venv\\Scripts\\python.exe main.py" in text and "\r\n" in text
    assert "scripts" not in text.lower().replace(".venv\\scripts", "")   # 绝不碰网关仓库 scripts/ 下会下单的脚本
