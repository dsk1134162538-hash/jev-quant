import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from research import pit_backfill as pb


def page_frame(partition, start, count):
    return pd.DataFrame({
        "STK_UNI_CODE": [f"{partition.name}-{index}" for index in range(start, start + count)],
        "STK_CODE": [f"{index:06d}" for index in range(start, start + count)],
        "STK_TYPE_PAR": ["A股"] * count,
        "LIST_SECT_PAR": [partition.board] * count,
        "LIST_DATE": ["2020-01-01"] * count,
        "LIST_STA_PAR": ["上市"] * count,
    })


def write_probe(root, partition, total=25, returned=20):
    stem = partition.probe_stem
    page_frame(partition, 0, returned).to_csv(root / f"{stem}.csv", index=False)
    (root / f"{stem}.log").write_text(
        f"Score cost: 20 pts\nTotal: {total} records\nReturned this page: {returned} records\n",
        encoding="utf-8",
    )


def one_partition_plan(root, total=25):
    partition = pb.Partition(
        "test_main", "主板",
        {"stkTypePar": "101", "secMarPar": "2", "listSectPar": "1"},
        "probe-main",
    )
    write_probe(root, partition, total)
    return partition, {
        "api_name": "股票基本信息-通用",
        "page_size": 20,
        "score_per_page": 20,
        "max_partition_total": 1999,
        "partitions": [{
            **pb.asdict(partition),
            "probe_log": str((root / "probe-main.log").resolve()),
            "probe_csv": str((root / "probe-main.csv").resolve()),
            "total": total,
            "score_cost": 20,
            "returned": 20,
            "pages": (total + 19) // 20,
        }],
        "total_records": total,
        "total_pages": (total + 19) // 20,
        "estimated_total_score": ((total + 19) // 20) * 20,
    }


def test_parse_probe_rejects_truncated_partition(tmp_path):
    partition = pb.Partition("bad", "主板", {}, "bad")
    write_probe(tmp_path, partition, total=2000)
    with pytest.raises(ValueError, match="必须细分"):
        pb.parse_probe_log(tmp_path / "bad.log")


def test_prepare_seeds_paid_probe_and_reports_remaining_cost(tmp_path):
    _, plan = one_partition_plan(tmp_path)
    runner = pb.BackfillRunner(plan, tmp_path / "out")
    manifest = runner.prepare()
    assert manifest["seeded_probe_pages"] == 1
    assert manifest["saved_pages"] == 1
    assert manifest["missing_pages"] == 1
    assert manifest["estimated_remaining_score"] == 20
    resumed = runner.prepare()
    assert resumed["seeded_probe_pages"] == 0
    assert resumed["saved_pages"] == 1


def test_execute_refuses_over_budget_before_any_call(tmp_path):
    _, plan = one_partition_plan(tmp_path)
    calls = []

    def forbidden(command):
        calls.append(command)
        raise AssertionError("不应发起请求")

    runner = pb.BackfillRunner(plan, tmp_path / "out", caller=forbidden)
    with pytest.raises(RuntimeError, match="未发起新请求"):
        runner.execute(max_paid_calls=0)
    assert calls == []


def test_execute_resume_and_consolidate_without_repeating_pages(tmp_path):
    partition, plan = one_partition_plan(tmp_path)
    helper = tmp_path / "helper.py"
    helper.touch()
    calls = []

    def fake_call(command):
        calls.append(command)
        params = json.loads(command[command.index("--params-json") + 1])
        assert params["pageNum"] == "2"
        output = Path(command[command.index("--file-path") + 1])
        page_frame(partition, 20, 5).to_csv(output, index=False)
        return subprocess.CompletedProcess(
            command, 0,
            stdout="Score cost: 20 pts\nTotal: 25 records\nReturned this page: 5 records\n",
            stderr="",
        )

    runner = pb.BackfillRunner(plan, tmp_path / "out", helper=helper, caller=fake_call)
    manifest = runner.execute(max_paid_calls=1)
    assert manifest["status"] == "complete"
    assert len(calls) == 1
    output = runner.consolidate()
    result = pd.read_csv(output, dtype=str)
    assert len(result) == 25
    assert result["STK_UNI_CODE"].is_unique
    runner.execute(max_paid_calls=0)
    assert len(calls) == 1


def test_failed_page_never_replaces_valid_saved_page(tmp_path):
    partition, plan = one_partition_plan(tmp_path)
    helper = tmp_path / "helper.py"
    helper.touch()

    def bad_call(command):
        output = Path(command[command.index("--file-path") + 1])
        page_frame(partition, 20, 5).assign(STK_TYPE_PAR="B股").to_csv(output, index=False)
        return subprocess.CompletedProcess(
            command, 0,
            stdout="Score cost: 20 pts\nTotal: 25 records\nReturned this page: 5 records\n",
            stderr="",
        )

    runner = pb.BackfillRunner(plan, tmp_path / "out", helper=helper, caller=bad_call)
    with pytest.raises(ValueError, match="非 A 股"):
        runner.execute(max_paid_calls=1)
    assert not (tmp_path / "out/pages/test_main/page-00002.csv").exists()
    assert runner.prepare()["missing_pages"] == 1


def test_consolidate_rejects_cross_partition_duplicate_ids(tmp_path):
    first, plan = one_partition_plan(tmp_path, total=20)
    second = pb.Partition("test_chinext", "创业板", {"listSectPar": "6"}, "probe-chinext")
    write_probe(tmp_path, second, total=20)
    duplicate = page_frame(second, 0, 20)
    duplicate["STK_UNI_CODE"] = first.name + "-" + duplicate.index.astype(str)
    duplicate.to_csv(tmp_path / "probe-chinext.csv", index=False)
    plan["partitions"].append({
        **pb.asdict(second),
        "probe_log": str((tmp_path / "probe-chinext.log").resolve()),
        "probe_csv": str((tmp_path / "probe-chinext.csv").resolve()),
        "total": 20, "score_cost": 20, "returned": 20, "pages": 1,
    })
    plan["total_records"] = 40
    plan["total_pages"] = 2
    runner = pb.BackfillRunner(plan, tmp_path / "out")
    assert runner.prepare()["status"] == "complete"
    with pytest.raises(ValueError, match="跨分区"):
        runner.consolidate()
