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


def test_decision_detail_renders_new_notice_risk_and_legacy_list():
    js = _scripts()
    assert "Array.isArray(t.notices)" in js and "t.notices.suspects" in js
    assert "已读正文" in js and "仅标题" in js


def test_ui_has_no_lowbuy_left():
    """低吸下线：面板上没有低吸的账户颜色、策略筛选、留痕分支和标题。"""
    html = UI.read_text(encoding="utf-8")
    assert "低吸" not in html and "lowbuy" not in html
    assert 'BUILTIN_ORDER = ["board_main", "board_shadow", "rebound_main", "rebound_shadow"]' in html


_RENDER_HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const noop = () => {};
const ctx = {console, document: {addEventListener: noop, getElementById: () => null,
                                 querySelectorAll: () => [], querySelector: () => null},
             window: {addEventListener: noop}, CSS: {escape: (s) => s}, setInterval: noop, setTimeout: noop};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + "\n;globalThis.renderTrace = renderTrace;", ctx);
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
console.log(JSON.stringify(cases.map((c) => ctx.renderTrace(c))));
"""


def _render(tmp_path, cases: list) -> list[str]:
    import json
    (tmp_path / "ui.js").write_text(_scripts(), encoding="utf-8")
    (tmp_path / "h.js").write_text(_RENDER_HARNESS, encoding="utf-8")
    (tmp_path / "cases.json").write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(["node", str(tmp_path / "h.js"), str(tmp_path / "ui.js"), str(tmp_path / "cases.json")],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _decision(verdict, detail, notices):
    return {"code": "600001", "name": "甲", "time": "10:00", "verdict": verdict, "detail": detail,
            "trace": {"strategy": "rebound", "candidate": {"pct": -1}, "notices": notices}}


@pytest.mark.skipif(shutil.which("node") is None, reason="没有安装 node")
def test_notice_risk_detail_escapes_and_survives_partial_fields(tmp_path):
    evil = {"title": "<script>x</script>", "category": "<b>c</b>", "level": "<i>", "score": None,
            "probs": {"<k>": 1, "高": "<y>"}, "body_read": True, "excerpt": "<img src=x onerror=1>",
            "days_ago": "<z>", "date": "<d>", "verdict": "<v>"}
    html = _render(tmp_path, [
        _decision("pass", "ok", {"suspects": [evil], "others": "<o>",
                                 "skipped": [{"date": "<a>", "title": "<t>", "category": "<c>"}]}),
        _decision("pass", "ok", {}),
        _decision("pass", "ok", {"suspects": [{}]}),
        _decision("pass", "ok", [{"date": "2026-09-01", "title": "旧<公告>", "label": "无关"}]),
    ])
    assert "<script>" not in html[0] and "<img" not in html[0] and "<b>c</b>" not in html[0]
    assert "&lt;script&gt;" in html[0] and "&lt;o&gt;" in html[0] and "&lt;t&gt;" in html[0]
    assert "放行" not in html[0]                       # 不认识的结论不能默认写成放行
    assert "没有疑似雷的公告" in html[1]
    assert "仅标题" in html[2]
    assert "公告排雷" in html[3] and "旧&lt;公告&gt;" in html[3]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有安装 node")
def test_notice_risk_detail_error_verdict_is_not_shown_as_clean(tmp_path):
    empty = {"suspects": [], "others": 0, "skipped": []}
    html = _render(tmp_path, [
        _decision("error", "判断异常：RuntimeError()", {**empty, "incomplete": True}),
        _decision("error", "Jev 不可用", {**empty, "others": 3}),          # 加 incomplete 标记之前的留痕
        _decision("error", "消息面判断失败", empty),                       # 公告判完了，是消息面出错
    ])
    for h in html[:2]:
        assert "公告没判完" in h and "没有疑似雷的公告" not in h
    assert "公告没判完" not in html[2] and "没有疑似雷的公告" in html[2]
