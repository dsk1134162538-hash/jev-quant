"""面板 ui.html 的静态检查：脚本能被 JS 引擎解析；JS 调用的每个 Api 方法都真实存在。"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jevq.app.api import Api

UI = Path(__file__).resolve().parent.parent / "jevq" / "app" / "ui.html"


def _scripts() -> str:
    html = UI.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert blocks, "ui.html 里没找到内联脚本"
    return "\n".join(blocks)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有安装 node")
def test_ui_script_parses(tmp_path):
    js = tmp_path / "ui.js"
    js.write_text(_scripts(), encoding="utf-8")
    r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr


def test_ui_only_calls_existing_api_methods():
    js = _scripts()
    assert "api = window.pywebview.api" in js
    called = set(re.findall(r"(?:pywebview\.api|\bapi)\.([A-Za-z_]\w*)\s*\(", js))
    public = {name for name in dir(Api) if not name.startswith("_") and callable(getattr(Api, name))}
    assert {"status", "rules_list", "rule_fields", "rule_validate", "rule_preview", "rule_create",
            "rule_update", "rule_disable", "rule_detail"} <= called
    assert called <= public, f"ui.html 调用了不存在的 Api 方法：{sorted(called - public)}"


def test_ui_sends_jev_switches_under_engine_key():
    """CustomStrategy/validate 读的是 spec.jev.notices（复数）；早期草稿发的 jev.notice 会被静默忽略。"""
    js = _scripts()
    assert "jev: {notices:" in js and "d.jev.notices" in js
    assert not re.search(r"jev\s*:\s*\{\s*notice\s*:", js)
