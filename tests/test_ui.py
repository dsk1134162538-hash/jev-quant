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


def test_curve_draws_single_point_series_as_dot():
    """新账户第一天只有一个净值点，polyline 画不出来；要画成一个点，否则图例有、图上没有。"""
    js = _scripts()
    body = js[js.index("function renderCurve"):js.index("function mdToHtml")]
    # 每个点都画圆点（单点序列画得更大），polyline 只在 ≥2 个点时画
    assert "s.pts.length > 1 ? `<polyline" in body and "const dots = s.pts.map" in body
    assert 'stroke-linecap="round"' in body


def test_curve_has_hover_tooltip_with_account_names():
    """鼠标悬停显示当天各账户名称、收益率、净值。"""
    js = _scripts()
    body = js[js.index("function renderCurve"):js.index("function mdToHtml")]
    assert "plot.onmousemove" in body and "accountName(s.id)" in body and "plot.onmouseleave" in body


def test_report_markdown_hides_heading_and_inline_symbols():
    """复盘报告里的 ### 标题、**粗体**、`代码` 不能把符号原样显示出来。"""
    js = _scripts()
    body = js[js.index("const inlineMd"):js.index("const VERDICT")]
    assert "#{1,6}" in body and "<code>" in body and "<b>" in body


def test_render_tab_bails_out_when_user_switched_tabs():
    """renderTab 的每个 await 后都要检查标签没变，慢回来的旧结果不能盖掉规则编辑器。"""
    js = _scripts()
    body = js[js.index("async function renderTab"):js.index("// ======================= 规则页")]
    awaits = re.findall(r"await api\.(fills|decisions|decision|board_stats|reports|logs)\(", body)
    assert set(awaits) == {"fills", "decisions", "decision", "board_stats", "reports", "logs"}
    assert body.count("if (moved()) return;") == 5          # fills/decisions(两次 await 后一次)/board/reports/logs
