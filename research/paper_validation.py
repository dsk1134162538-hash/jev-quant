"""模拟（paper）验证门：候选先在模拟盘跑够样本，才允许成为 paper champion。

用法::

    python -m research.paper_validation record --candidate-id <ID> --run <JSON 文件> [--registry <目录>]
    python -m research.paper_validation review --candidate-id <ID> [--registry <目录>]

record 把每一轮模拟结果追加到 registry/paper_runs/<candidate_id>.jsonl；只接受
已登记在 registry/paper_candidate.json 的候选，且 liveTrading 必须显式为 false。
review 重读该候选的全部 run，按本模块内写死的门槛判定；它接受当前 paper 候选、
已登记的 candidates/<candidate_id>/release.json、以及当前 champion 对应的候选，
因此 paper_candidate 被替换后仍能复盘/撤销旧候选：

* 合格才原子写 champion.json（仅 paper trading）；
* 不合格且当前 champion 正好是同一候选时，把 champion 原子移动到 retired/；
* champion 属于其他候选时一律不动。

门槛不读配置文件、也不接受命令行参数，无法通过 CLI 或模型输出放宽。

退出码：review 合格 0，不合格 1；record 成功 0，run 非法或候选不存在 1。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from research.strategy_lifecycle import (
    DEFAULT_REGISTRY,
    RELEASE_SCOPE,
    SCHEMA_VERSION,
    _append_event,
    _assert_paper_only,
    _atomic_write_json,
    _now,
    _read_json,
    status as lifecycle_status,
)

# 固定门槛，仅作记录与说明，判定逻辑同样写死在下方的校验里。
THRESHOLDS = {
    "distinctDays": ">=20",
    "completedRuns": ">=60",
    "fills": ">=10",
    "errorRuns": "=0",
    "reconciliationFailures": "=0",
    "totalReturn": ">0",
    "maxDrawdown": ">=-0.10",
    "liveTrading": "全部为 false",
}

RUN_STATUSES = ("completed", "error", "skipped")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def _stamp() -> str:
    """返回可用作文件名、且可按字典序排序的 UTC 时间戳。"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _parse_ts(value):
    """解析 ISO 时间字符串，失败时返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _is_number(value) -> bool:
    """判断是否为真数字（布尔不算）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _registry(registry) -> Path:
    return Path(registry).expanduser().resolve()


def _paper_candidate(registry: Path):
    """读取当前 paper 候选；不存在或损坏时返回 None。"""
    payload = _read_json(registry / "paper_candidate.json")
    if not isinstance(payload, dict):
        return None
    return payload


def _safe_id(candidate_id) -> str:
    """把 candidate_id 归一化为字符串并做 SAFE_ID 校验。"""
    candidate_id = str(candidate_id or "").strip()
    if not SAFE_ID.fullmatch(candidate_id):
        raise ValueError(f"candidate_id 不合法: {candidate_id!r}")
    return candidate_id


def _champion(registry: Path):
    """读取当前 champion；不存在或损坏时返回 None。"""
    payload = _read_json(registry / "champion.json")
    return payload if isinstance(payload, dict) else None


def _registered_release(candidate_id: str, registry: Path):
    """读取 candidates/<id>/release.json 里已登记的候选；不存在或不符时返回 None。"""
    payload = _read_json(registry / "candidates" / candidate_id / "release.json")
    if not isinstance(payload, dict):
        return None
    if payload.get("candidate_id") != candidate_id:
        return None
    if payload.get("accepted") is not True:
        return None
    return payload


def _require_record_candidate(candidate_id, registry: Path) -> dict:
    """record 用：只接受当前 paper 候选，否则抛出 ValueError。"""
    candidate_id = _safe_id(candidate_id)
    candidate = _paper_candidate(registry)
    if candidate is None:
        raise ValueError("registry 中没有 paper_candidate，请先通过 evaluate 产出候选")
    if candidate.get("candidate_id") != candidate_id:
        raise ValueError(f"candidate_id 不是当前 paper 候选: {candidate_id}")
    return candidate


def _require_review_candidate(candidate_id, registry: Path) -> dict:
    """review 用：接受当前 paper 候选、已登记 release、或当前 champion 对应候选。

    paper 候选被新候选替换后，旧候选仍留有 release.json 或 champion 记录，
    因此仍可复盘并撤销。三条路径都不命中时抛出 ValueError（fail closed）。
    """
    candidate_id = _safe_id(candidate_id)

    current = _paper_candidate(registry)
    if current is not None and current.get("candidate_id") == candidate_id:
        return current

    release = _registered_release(candidate_id, registry)
    if release is not None:
        return release

    champion = _champion(registry)
    if champion is not None and champion.get("candidate_id") == candidate_id:
        return champion

    raise ValueError(f"candidate_id 既不是当前 paper 候选，也没有已登记记录: {candidate_id}")


# --------------------------------------------------------------------------
# 单轮模拟记录
# --------------------------------------------------------------------------


def _validate_run(run) -> list:
    """校验一轮模拟结果，返回不合格原因列表（空列表表示合法）。"""
    if not isinstance(run, dict):
        return ["run 必须是 JSON 对象"]

    reasons = []
    if _parse_ts(run.get("ts")) is None:
        reasons.append("ts 必须是 ISO 时间字符串")
    if run.get("status") not in RUN_STATUSES:
        reasons.append(f"status 必须是 {', '.join(RUN_STATUSES)} 之一")
    if not isinstance(run.get("reconciled"), bool):
        reasons.append("reconciled 必须是布尔")
    if run.get("liveTrading") is not False:
        reasons.append("liveTrading 必须显式为 false")
    equity = run.get("equity")
    if not _is_number(equity) or not math.isfinite(equity) or equity <= 0:
        reasons.append("equity 必须是正的有限数字")
    execution = run.get("execution")
    if not isinstance(execution, dict):
        reasons.append("execution 必须是 JSON 对象")
    elif not isinstance(execution.get("fills"), list):
        reasons.append("execution.fills 必须是列表")
    return reasons


def record_run(candidate_id, run, registry=DEFAULT_REGISTRY) -> dict:
    """校验并追加一轮模拟结果，返回写入的记录。"""
    registry = _registry(registry)
    candidate_id = _safe_id(candidate_id)
    _require_record_candidate(candidate_id, registry)

    reasons = _validate_run(run)
    if reasons:
        raise ValueError("run 不合法: " + "; ".join(reasons))

    path = registry / "paper_runs" / f"{candidate_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(run, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with open(path, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return run


def _load_runs(registry: Path, candidate_id: str) -> tuple:
    """重读该候选的全部 run，返回 (合法记录列表, 无法解析的条数)。"""
    path = registry / "paper_runs" / f"{candidate_id}.jsonl"
    runs = []
    invalid = 0
    if not path.is_file():
        return runs, invalid
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                invalid += 1
                continue
            if isinstance(record, dict):
                runs.append(record)
            else:
                invalid += 1
    return runs, invalid


# --------------------------------------------------------------------------
# 复盘计算
# --------------------------------------------------------------------------


def _curve_metrics(series: list) -> tuple:
    """由权益序列计算总收益与最大回撤；点数不足时返回 (None, None)。"""
    if len(series) < 2:
        return None, None
    total_return = series[-1] / series[0] - 1.0
    peak = series[0]
    worst = 0.0
    for value in series:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return total_return, worst


def _metrics(runs: list) -> dict:
    """统计复盘指标，权益序列只取 completed 且 reconciled 的 run。"""
    days = set()
    completed = 0
    errors = 0
    reconciliation_failures = 0
    fills = 0
    live_all_false = True
    points = []

    for index, run in enumerate(runs):
        ts = _parse_ts(run.get("ts"))
        if ts is not None:
            days.add(ts.date().isoformat())
        status_value = run.get("status")
        reconciled = run.get("reconciled")
        if status_value == "completed":
            completed += 1
            if reconciled is not True:
                reconciliation_failures += 1
        elif status_value == "error":
            errors += 1
        if run.get("liveTrading") is not False:
            live_all_false = False
        execution = run.get("execution")
        if isinstance(execution, dict) and isinstance(execution.get("fills"), list):
            fills += len(execution["fills"])
        equity = run.get("equity")
        if (
            status_value == "completed"
            and reconciled is True
            and _is_number(equity)
            and math.isfinite(equity)
            and equity > 0
        ):
            fallback = datetime.min.replace(tzinfo=timezone.utc)
            points.append((ts or fallback, index, float(equity)))

    points.sort(key=lambda item: (item[0], item[1]))
    total_return, max_drawdown = _curve_metrics([item[2] for item in points])

    return {
        "distinctDays": len(days),
        "completedRuns": completed,
        "errorRuns": errors,
        "reconciliationFailures": reconciliation_failures,
        "fills": fills,
        "equityPoints": len(points),
        "totalReturn": total_return,
        "maxDrawdown": max_drawdown,
        "liveTradingAllFalse": live_all_false,
    }


def _gate(metrics: dict, reasons: list) -> dict:
    """按写死的门槛逐项判定，返回验收明细。"""
    checks = {}

    def check(name: str, observed, passed: bool, required: str) -> None:
        checks[name] = {"ok": passed, "observed": observed, "required": required}
        if not passed:
            reasons.append(f"{name}={observed!r} 未满足门槛 {required}")

    check("distinctDays", metrics["distinctDays"], metrics["distinctDays"] >= 20, ">=20")
    check("completedRuns", metrics["completedRuns"], metrics["completedRuns"] >= 60, ">=60")
    check("fills", metrics["fills"], metrics["fills"] >= 10, ">=10")
    check("errorRuns", metrics["errorRuns"], metrics["errorRuns"] == 0, "=0")
    check(
        "reconciliationFailures",
        metrics["reconciliationFailures"],
        metrics["reconciliationFailures"] == 0,
        "=0",
    )

    total_return = metrics["totalReturn"]
    if total_return is None:
        checks["totalReturn"] = {"ok": False, "observed": None, "required": ">0"}
        reasons.append("缺少可用的 completed 且已对账权益序列，无法计算 totalReturn")
    else:
        check("totalReturn", total_return, total_return > 0, ">0")

    max_drawdown = metrics["maxDrawdown"]
    if max_drawdown is None:
        checks["maxDrawdown"] = {"ok": False, "observed": None, "required": ">=-0.10"}
        reasons.append("缺少可用的 completed 且已对账权益序列，无法计算 maxDrawdown")
    else:
        check("maxDrawdown", max_drawdown, max_drawdown >= -0.10, ">=-0.10")

    check("liveTrading", metrics["liveTradingAllFalse"], metrics["liveTradingAllFalse"], "全部为 false")
    return checks


# --------------------------------------------------------------------------
# champion 写入 / 撤销
# --------------------------------------------------------------------------


def _promote(candidate_id: str, candidate: dict, review_path: Path, registry: Path) -> None:
    """原子写入 champion，沿用 paper 候选的产物哈希。"""
    champion = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "status": "active",
        "releaseScope": RELEASE_SCOPE,
        "liveTrading": False,
        "latestReviewPath": str(review_path),
        "artifacts": candidate.get("artifacts"),
        "input_sha256": candidate.get("input_sha256"),
        "source_summary": candidate.get("source_summary"),
        "promoted_at": _now(),
    }
    _assert_paper_only(champion)
    _atomic_write_json(registry / "champion.json", champion)


def _retire(candidate_id: str, registry: Path) -> bool:
    """把同一候选的 champion 原子移入 retired/，返回是否发生了移动。"""
    champion_path = registry / "champion.json"
    existing = _read_json(champion_path)
    if not isinstance(existing, dict) or existing.get("candidate_id") != candidate_id:
        return False

    retired_dir = registry / "retired"
    retired_dir.mkdir(parents=True, exist_ok=True)
    retired_path = retired_dir / f"{candidate_id}-{_stamp()}.json"
    os.replace(champion_path, retired_path)
    if champion_path.exists():
        champion_path.unlink()
    return True


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _champion_action(qualified: bool, candidate_id: str, registry: Path) -> str:
    """在任何写盘之前确定本次复盘对 champion 的实际动作。

    合格即 promoted；不合格时按当前 champion 归属决定：没有 champion 为
    none，同一候选为 retired，其他候选为 unchanged。
    """
    if qualified:
        return "promoted"
    champion = _champion(registry)
    if champion is None:
        return "none"
    if champion.get("candidate_id") == candidate_id:
        return "retired"
    return "unchanged"


def review(candidate_id, registry=DEFAULT_REGISTRY) -> dict:
    """重读该候选的全部模拟记录并判定，返回本次复盘结果。"""
    registry = _registry(registry)
    candidate_id = _safe_id(candidate_id)
    candidate = _require_review_candidate(candidate_id, registry)

    runs, invalid = _load_runs(registry, candidate_id)
    metrics = _metrics(runs)
    reasons = []
    if invalid:
        reasons.append(f"{invalid} 条 run 记录无法解析")
    if not runs:
        reasons.append("该候选还没有任何模拟 run")
    checks = _gate(metrics, reasons)
    qualified = not reasons

    reviewed_at = _now()
    review_path = registry / "reviews" / candidate_id / f"{_stamp()}.json"

    result = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "reviewed_at": reviewed_at,
        "qualified": qualified,
        "releaseScope": RELEASE_SCOPE,
        "liveTrading": False,
        "runCount": len(runs),
        "invalidRuns": invalid,
        "thresholds": THRESHOLDS,
        "checks": checks,
        "reasons": reasons,
        "review_path": str(review_path),
    }
    result.update(
        {
            "distinctDays": metrics["distinctDays"],
            "completedRuns": metrics["completedRuns"],
            "errorRuns": metrics["errorRuns"],
            "reconciliationFailures": metrics["reconciliationFailures"],
            "fills": metrics["fills"],
            "totalReturn": metrics["totalReturn"],
            "maxDrawdown": metrics["maxDrawdown"],
        }
    )

    action = _champion_action(qualified, candidate_id, registry)

    # 撤回失败（期间 champion 已变）时动作回落为 unchanged，保证文件、返回值、
    # 事件三者一致；_retire 自身的原子移动保持不变。
    if action == "retired" and not _retire(candidate_id, registry):
        action = "unchanged"

    result["championAction"] = action
    _atomic_write_json(review_path, result)
    if qualified:
        _promote(candidate_id, candidate, review_path, registry)

    _append_event(
        registry,
        {
            "time": _now(),
            "event": "paper_review",
            "candidate_id": candidate_id,
            "qualified": qualified,
            "reasons": reasons,
            "metrics": {key: metrics[key] for key in ("distinctDays", "completedRuns", "errorRuns",
                                                      "reconciliationFailures", "fills",
                                                      "totalReturn", "maxDrawdown")},
            "review_path": str(review_path),
            "champion_action": result["championAction"],
        },
    )
    return result


def status(registry=DEFAULT_REGISTRY) -> dict:
    """复用生命周期快照；其中 latestReview 为时间戳最新的复盘记录。"""
    return lifecycle_status(registry)


def main(argv=None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="python -m research.paper_validation",
        description="模拟验证门（paper-only）",
    )
    subparsers = parser.add_subparsers(dest="command")

    record_parser = subparsers.add_parser("record", help="追加一轮模拟结果")
    record_parser.add_argument("--candidate-id", required=True, help="paper 候选 ID")
    record_parser.add_argument("--run", required=True, help="单轮模拟结果 JSON 文件")
    record_parser.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="registry 目录")

    review_parser = subparsers.add_parser("review", help="复盘该候选的全部模拟记录并判定")
    review_parser.add_argument("--candidate-id", required=True, help="paper 候选 ID")
    review_parser.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="registry 目录")

    args = parser.parse_args(argv)
    if args.command == "record":
        try:
            run = json.loads(Path(args.run).read_text(encoding="utf-8"))
            recorded = record_run(args.candidate_id, run, args.registry)
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(recorded, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "review":
        try:
            result = review(args.candidate_id, args.registry)
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["qualified"] else 1
    parser.print_help()
    return 2
