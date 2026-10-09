"""面板「情绪」页：每天一根情绪 K 线 + 涨停/跌停（用户 2026-10-09）。"""
import json

from jevq import db
from jevq.app.sentiment_board import board, score


def _sent(conn, ts, label, probs=None, **stats):
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, '', 'sentiment', ?, ?, 'market')",
                 (ts, json.dumps({"stats": stats, "label": label, "overseas": "偏弱"}, ensure_ascii=False),
                  json.dumps({"probabilities": probs}, ensure_ascii=False) if probs else None))


def test_score_is_probability_weighted_and_falls_back_to_label():
    assert score("偏弱", {"冰点": 0.1, "偏弱": 0.6, "正常": 0.3, "火热": 0.0}) == 1.2
    assert score("火热", None) == 3 and score(None, None) is None


def test_board_builds_one_candle_per_day_with_last_stats_and_turn():
    conn = db.connect(":memory:")
    _sent(conn, "2026-10-08T09:30:00", "火热", 涨停家数=5)
    _sent(conn, "2026-10-08T14:50:00", "偏弱", 涨停家数=40, 跌停家数=3)
    _sent(conn, "2026-10-09T09:30:00", "偏弱", {"冰点": 0.2, "偏弱": 0.8})
    _sent(conn, "2026-10-09T11:16:00", "冰点", {"冰点": 0.9, "偏弱": 0.1}, 涨停家数=28, 跌停家数=18)
    _sent(conn, "2026-10-09T14:54:00", "火热", {"正常": 0.4, "火热": 0.6}, 涨停家数=69, 跌停家数=7, 上涨家数=1947)
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES "
                 "('2026-10-09T13:00:00', '', 'turn', '{\"confirmed\": false}', 'market'), "
                 "('2026-10-09T13:10:00', '', 'turn', '{\"confirmed\": true}', 'market')")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES "
                 "('2026-10-09T10:00:00', '600001', 'pass', 'x', 'rebound')")
    d8, d9 = board(conn)["days"]
    assert d8["date"] == "2026-10-08" and (d8["open"], d8["close"], d8["high"], d8["low"]) == (3, 1, 3, 1)
    assert d8["limit_up"] == 40 and d8["label"] == "偏弱" and d8["turn"] is None
    assert d9["open"] == 0.8 and d9["low"] == 0.1 and d9["close"] == 2.6 and d9["high"] == 2.6
    assert (d9["limit_up"], d9["limit_down"], d9["up"]) == (69, 7, 1947)
    assert d9["turn"] == "13:10" and d9["n"] == 3 and d9["points"][1]["limit_down"] == 18
    assert d9["overseas"] == "偏弱"


def test_board_limits_days_and_handles_empty():
    conn = db.connect(":memory:")
    assert board(conn) == {"days": []}
    for day in ("2026-10-07", "2026-10-08", "2026-10-09"):
        _sent(conn, f"{day}T10:00:00", "正常")
    assert [d["date"] for d in board(conn, days=2)["days"]] == ["2026-10-08", "2026-10-09"]


import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

UI = Path(__file__).resolve().parent.parent / "jevq" / "app" / "ui.html"
_HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const noop = () => {};
const ctx = {console, document: {addEventListener: noop, getElementById: () => null,
                                 querySelectorAll: () => [], querySelector: () => null},
             window: {addEventListener: noop}, CSS: {escape: (s) => s}, setInterval: noop, setTimeout: noop};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + "\n;globalThis.f = moodHtml; globalThis.g = moodDayHtml;", ctx);
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
console.log(JSON.stringify(cases.map((c) => ctx.f(c)).concat([ctx.g(cases[2].days[1])])));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="没有安装 node")
def test_mood_page_renders_candles_bars_turn_and_escapes(tmp_path):
    js = "\n".join(re.findall(r"<script>(.*?)</script>", UI.read_text(encoding="utf-8"), flags=re.S))
    (tmp_path / "ui.js").write_text(js, encoding="utf-8")
    (tmp_path / "h.js").write_text(_HARNESS, encoding="utf-8")
    evil = "<b>x</b>"
    day = lambda d, o, c, turn=None: {"date": d, "n": 2, "turn": turn, "label": evil, "overseas": "偏弱",
                                       "open": o, "close": c, "high": max(o, c), "low": min(o, c), "limit_up": 69,
                                       "limit_down": 7, "break_rate": 16.9, "max_board": 9, "up": 1947, "down": 1014,
                                       "points": [{"time": "09:30", "label": evil, "score": o, "limit_up": 4,
                                                   "limit_down": 1, "break_rate": 0, "up": 916, "down": 1519,
                                                   "amount_ratio": -8.3}]}
    data = {"days": [day("2026-10-08", 3, 1), day("2026-10-09", 0.8, 2.6, "13:10")]}
    (tmp_path / "c.json").write_text(json.dumps([None, {"days": []}, data], ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(["node", str(tmp_path / "h.js"), str(tmp_path / "ui.js"), str(tmp_path / "c.json")],
                       capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    e1, e2, full, dayhtml = json.loads(r.stdout)
    assert "还没有情绪记录" in e1 and "还没有情绪记录" in e2
    assert "<b>x</b>" not in full and "&lt;b&gt;x&lt;/b&gt;" in full
    assert full.count("<rect") == 2 + 4 and "拐</div>" in full and "情绪拐点 <b>13:10</b>" in full
    assert "涨停 <b class=\"up\">69</b>" in full and "10-09" in full
    assert "2026-10-09 盘中情绪" in dayhtml and "-8.3%" in dayhtml and "&lt;b&gt;" in dayhtml
