from research import first_board_ai as ai


def _data(n_days=4):
    days = [f"2026-06-{d:02d}" for d in range(1, n_days + 1)]
    cands = {d: [{"code": f"60000{i}", "industry": "电子", "openGap%": i, "pos5%": 0, "pos20%": 0, "floatMcap亿": 50,
                  "prevTurnover%": 2, "prevAmount亿": 3 - i, "holders万": 3, "同行业今日摸9%家数(盘中逐步可见)": 2,
                  "sealed": i == 0, "ret_open%": 3.0 if i == 0 else -2.0} for i in range(3)] for d in days}
    return {"days": days, "market": {d: {"昨日涨停": 50} for d in days}, "cands": cands}


def test_anonymized_view_hides_codes_and_picks_map_back(tmp_path, monkeypatch):
    data = _data()
    monkeypatch.setattr(ai, "TRAIN_DAYS", 2)
    view, key = ai.anon(data, "2026-06-01")
    assert "600000" not in ai.prompt(view, "v1") and set(key.values()) == {"600000", "600001", "600002"}

    def asker(view, variant, model, lessons=None):
        best = min(view["今天会摸到9%的候选"].items(), key=lambda kv: kv[1]["openGap%"])[0]
        return {"gate": "开闸", "confidence": 0.6, "reason": "x", "picks": [{"id": best, "why": "y"}]}
    out = tmp_path / "runs.jsonl"
    for m in ai.MODELS:
        ai.run_variant(data, data["days"], "v1", (m,), workers=2, asker=asker, out=out)
    runs = ai.read_runs(out)
    assert all(r["ok"] and r["picks"] == ["600000"] and r["ret"] == 3.0 for r in runs)
    rep = ai.report(data, runs)
    assert rep["base"]["test"]["avgPerTrade%"] == round((3 - 2 - 2) / 3, 3)
    assert rep["v1:deepseek-v4.1-flash"]["test"]["total%"] == 6.0
    assert rep["vote(v1,>=2)"]["test"]["total%"] == 6.0
