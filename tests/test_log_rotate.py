import json
from datetime import datetime

from dashboard.log_rotate import rotate_jsonl

NOW = datetime(2026, 11, 12, 15, 40)


def _write(path, days):
    path.write_text("".join(json.dumps({"ts": f"{d}T10:00:00", "n": i}, ensure_ascii=False) + "\n" for i, d in enumerate(days)), encoding="utf-8")


def test_old_rows_move_to_a_monthly_archive_and_recent_rows_stay(tmp_path):
    p = tmp_path / "ai_decisions.jsonl"
    _write(p, ["2026-10-30", "2026-11-03", "2026-11-04", "2026-11-06", "2026-11-11", "2026-11-12"])
    out = rotate_jsonl(p, NOW, keep_days=7, keep_rows=0)
    assert out == {"moved": 3, "kept": 3}
    assert [json.loads(x)["ts"][:10] for x in p.read_text(encoding="utf-8").splitlines()] == ["2026-11-06", "2026-11-11", "2026-11-12"]
    assert len((tmp_path / "archive" / "ai_decisions-2026-10.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert len((tmp_path / "archive" / "ai_decisions-2026-11.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert rotate_jsonl(p, NOW, keep_days=7, keep_rows=0) == {"moved": 0, "kept": 3}          # 再跑一次什么都不动


def test_the_last_rows_are_kept_however_old_and_unparseable_rows_never_move(tmp_path):
    p = tmp_path / "x.jsonl"
    _write(p, ["2026-09-01", "2026-09-02", "2026-09-03"])
    with open(p, "a", encoding="utf-8") as f:
        f.write("不是 json\n" + json.dumps({"no": "ts"}) + "\n")
    out = rotate_jsonl(p, NOW, keep_days=7, keep_rows=3)                 # 最后 3 行（含两条没时间的）受保护
    assert out == {"moved": 2, "kept": 3}
    kept = p.read_text(encoding="utf-8").splitlines()
    assert json.loads(kept[0])["ts"].startswith("2026-09-03") and kept[1] == "不是 json"


def test_archive_appends_across_runs_and_a_missing_file_is_fine(tmp_path):
    assert rotate_jsonl(tmp_path / "none.jsonl", NOW) == {"moved": 0, "kept": 0}
    p = tmp_path / "a.jsonl"
    _write(p, ["2026-11-01", "2026-11-12"])
    rotate_jsonl(p, NOW, keep_rows=0)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-11-02T09:00:00"}) + "\n")
    rotate_jsonl(p, NOW, keep_rows=0)
    assert len((tmp_path / "archive" / "a-2026-11.jsonl").read_text(encoding="utf-8").splitlines()) == 2
