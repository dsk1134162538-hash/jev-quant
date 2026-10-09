"""面板「候选」页：快照存取、两个策略的候选视图（按优先顺序）、Api.candidates、页面渲染。"""
import json
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest
from factories import quote

from jevq import db
from jevq.data import candidates
from jevq.rules.board import Candidate
from jevq.strategies.board import BoardStrategy
from jevq.strategies.rebound import ReboundStrategy
from jevq.ledger import Ledger

NOW = datetime(2026, 9, 24, 14, 52)
UI = Path(__file__).resolve().parent.parent / "jevq" / "app" / "ui.html"


def test_snapshot_latest_picks_newest_day_and_survives_garbage():
    conn = db.connect(":memory:")
    assert candidates.latest(conn)["date"] is None
    candidates.save(conn, date(2026, 9, 23), NOW, [{"code": "1"}], None)
    candidates.save(conn, date(2026, 9, 24), NOW, [{"code": "2"}], {"items": []})
    got = candidates.latest(conn)
    assert got["date"] == "2026-09-24" and got["board"] == [{"code": "2"}] and got["updated"] == "14:52"
    conn.execute("UPDATE day_cache SET value='坏' WHERE date='2026-09-24'")
    assert candidates.latest(conn)["date"] is None


def test_board_candidates_view_orders_article_hot_then_heat_then_score():
    conn = db.connect(":memory:")
    b = BoardStrategy(conn, Ledger(conn), None, None, None, {"1": "甲", "2": "乙", "3": "丙"}, {}, {}, {},
                      date(2026, 9, 23))
    b.watch = {"1": Candidate("1", "A", 5, 3), "2": Candidate("2", "B", 3, 2, article_hot="板块：B"),
               "3": Candidate("3", "C", 5, 1)}
    b.verdicts = {"3": ("pass", "公告无雷", {})}
    rows = b.candidates_view({"3": quote("3", pct=8.0)})
    assert [r["code"] for r in rows] == ["2", "3", "1"] and [r["rank"] for r in rows] == [1, 2, 3]
    assert rows[1]["verdict"] == "pass" and rows[1]["pct"] == 8.0 and rows[0]["pct"] is None
    assert rows[0]["article_hot"] == "板块：B" and rows[0]["name"] == "乙"


def test_rebound_candidates_view_ranks_and_reports_window():
    conn = db.connect(":memory:")
    universe = {"600001": "甲", "600002": "乙"}
    r = ReboundStrategy(conn, Ledger(conn), None, None, universe, {"600001": -3.0, "600002": -8.0}, set(),
                        date(2026, 9, 23), lambda: "偏弱")
    r.features = None                                              # 不看成交量：只按 5 日跌幅排
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES (?,?,?,?,?)",
                 (NOW.isoformat(), "600002", "reject", "新增减持", "rebound"))
    quotes = {c: quote(c, pct=-1.0, ts=NOW) for c in universe}
    info = r.candidates_view(quotes, NOW)
    assert [i["code"] for i in info["items"]] == ["600002", "600001"]       # 跌得多的优先
    assert info["items"][0]["verdict"] == "reject" and info["items"][1]["verdict"] == ""
    assert info["mode"] == "none" and info["window_open"] is False and info["total"] == 2   # 偏弱不买，候选照列
    assert r.candidates_view({}, NOW)["items"] == []
    r.enabled = False
    assert r.candidates_view(quotes, NOW)["items"] == []


def test_api_candidates_reads_latest(tmp_path):
    from jevq.app.api import Api
    path = str(tmp_path / "t.sqlite")
    conn = db.connect(path)
    candidates.save(conn, date(2026, 9, 24), NOW, [{"code": "600001"}], None)
    conn.close()
    api = Api.__new__(Api)
    api._db_path = path
    assert api.candidates()["board"] == [{"code": "600001"}]


_HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const noop = () => {};
const ctx = {console, document: {addEventListener: noop, getElementById: () => null,
                                 querySelectorAll: () => [], querySelector: () => null},
             window: {addEventListener: noop}, CSS: {escape: (s) => s}, setInterval: noop, setTimeout: noop};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + "\n;globalThis.candidatesHtml = candidatesHtml;", ctx);
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
console.log(JSON.stringify(cases.map((c) => ctx.candidatesHtml(c))));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="没有安装 node")
def test_candidates_page_renders_escapes_and_handles_empty(tmp_path):
    import re
    js = "\n".join(re.findall(r"<script>(.*?)</script>", UI.read_text(encoding="utf-8"), flags=re.S))
    (tmp_path / "ui.js").write_text(js, encoding="utf-8")
    (tmp_path / "h.js").write_text(_HARNESS, encoding="utf-8")
    evil = {"date": "2026-09-24", "updated": "14:52",
            "board": [{"rank": 1, "code": "600001", "name": "<b>甲</b>", "concept": "<i>x</i>", "heat": 4, "pct": 8.2,
                       "price": 10.5, "article_hot": "板块：<u>", "verdict": "pass", "why": "<s>"}],
            "rebound": {"sentiment": "偏弱", "mode": "tail", "window_open": False, "enabled": True, "total": 40,
                        "items": [{"rank": 1, "code": "600002", "name": "乙", "pct": -1.0, "price": 9.9,
                                   "ret5": -6.0, "shrink_days": 4, "verdict": "", "why": ""}]}}
    cases = [None, {"date": None}, evil, {**evil, "rebound": None}]
    (tmp_path / "c.json").write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(["node", str(tmp_path / "h.js"), str(tmp_path / "ui.js"), str(tmp_path / "c.json")],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    empty1, empty2, full, no_rebound = json.loads(r.stdout)
    assert "还没有候选快照" in empty1 and "还没有候选快照" in empty2
    assert "<b>" not in full and "&lt;b&gt;甲" in full and "&lt;i&gt;" in full and "&lt;u&gt;" in full and "&lt;s&gt;" in full
    assert "只在尾盘买" in full and "现在不在买入时段" in full and "共 40 只，只列前 1 只" in full
    assert "未判" in full and "已通过" in full
    assert "超跌反弹未运行" in no_rebound
