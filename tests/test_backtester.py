"""回测员（dj 2026-11-12）：每只候选报前期位置和历史同档表现；经理开会多一行「回测」。"""
import json
from datetime import datetime

from dashboard import backtester as bt
from dashboard import manager as mg

NOW = datetime(2026, 11, 12, 9, 26)
ROWS = [{"lo": None, "hi": 0, "label": "<0%", "n": 100, "avg1": 0.1, "avg5": 0.3, "med5": 0.1, "win5": 51.0, "deep5": 6.0},
        {"lo": 0, "hi": 30, "label": "0~30%", "n": 80, "avg1": 0.1, "avg5": 0.2, "med5": -0.2, "win5": 48.0, "deep5": 7.0},
        {"lo": 30, "hi": 60, "label": "30~60%", "n": 20, "avg1": 0.3, "avg5": -0.6, "med5": -2.4, "win5": 41.0, "deep5": 37.0},
        {"lo": 60, "hi": None, "label": "≥60%", "n": 9, "avg1": 0.6, "avg5": -0.7, "med5": -2.8, "win5": 42.0, "deep5": 46.0}]


def test_note_names_the_position_and_the_historical_bucket():
    text = bt.note({"rise5": 19.2, "rise20": 79.0, "fromLow60": 133.5}, ROWS)
    assert text.startswith("高位·程序拒买：20日+79%、比60日低点高134%")
    assert "≥60%" in text and "5日中位-2.8%" in text and "跌超10%的占46%" in text
    assert bt.note({"rise5": 1.0}, ROWS) == ""                                     # 没有 20 日数据：不写
    assert bt.note({"rise20": 35.0}, []).startswith("高位：20日+35%")              # 底账读不到：只报位置
    assert bt.verdict(-25) == "超跌" and bt.verdict(10) == "正常" and bt.verdict(None) == "历史不够"


def test_annotate_merges_position_and_note_without_touching_the_input():
    payload = {"000530": {"name": "冰山冷热", "pct": 0.4}, "600002": {"name": "乙", "pct": 1.0}}
    out = bt.annotate(payload, {"000530": {"rise5": 19.2, "rise20": 79.0, "fromLow60": 133.5}}, ROWS)
    assert out["000530"]["rise20"] == 79.0 and "回测" in out["000530"] and out["000530"]["name"] == "冰山冷热"
    assert out["600002"] == {"name": "乙", "pct": 1.0} and "rise20" not in payload["000530"]


def test_report_is_one_line_for_the_manager_with_details_for_the_panel():
    quotes = {"000530": {"name": "冰山冷热", "rise5": 19.2, "rise20": 79.0, "fromLow60": 133.5},
              "600003": {"name": "丙", "rise5": 5.0, "rise20": 35.0, "fromLow60": 40.0},
              "600002": {"name": "乙", "rise5": 1.0, "rise20": 3.0, "fromLow60": 8.0}, "600004": {"name": "丁"}}
    r = bt.report(quotes, NOW, ROWS)
    assert r["role"] == "backtest" and r["level"] == "bad"
    assert r["line"] == "看了 3 只 · 高位 2 只（拒买 1） · 最高 冰山冷热 20日+79%"
    assert [i["code"] for i in r["items"]] == ["000530", "600003", "600002"]          # 按 20 日涨幅从高到低
    assert bt.report({"600002": quotes["600002"]}, NOW, ROWS)["level"] == "ok"
    assert bt.report({}, NOW, ROWS)["level"] == "idle"


def test_manager_hears_the_backtester_in_the_meeting():
    ctx = {"quotes": {"000530": {"name": "冰山冷热", "rise20": 79.0, "src": "梯队"}}}
    reports = mg.desk_reports(ctx, {}, {}, NOW)
    bt_row = [r for r in reports if r["role"] == "backtest"]
    assert len(bt_row) == 1 and "冰山冷热" in bt_row[0]["line"] and bt_row[0]["level"] == "bad"


def test_the_shipped_base_rates_file_has_the_buckets_the_backtester_reads():
    data = json.loads(bt.RATES_PATH.read_text(encoding="utf-8"))
    rows = data["all"]
    assert rows and all({"lo", "hi", "label", "n", "med5", "win5", "deep5"} <= set(r) for r in rows)
    high = bt.bucket_of(79.0, rows)
    assert high is not None and high["deep5"] > 30                              # 高位档：5 天内跌超 10% 的比例明显偏高
