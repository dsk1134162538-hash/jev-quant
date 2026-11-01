"""确定性策略发布门（paper-only）。

用法::

    python -m research.strategy_lifecycle evaluate --candidate <目录> [--registry <目录>]
    python -m research.strategy_lifecycle status [--registry <目录>]

evaluate 会重新读取候选目录里的产物，按本模块内写死的门槛判定能否发布。
门槛不读配置文件、不接受命令行参数、也不看候选产物自己的结论字段，因此
无法通过 CLI 或模型输出放宽。合格时只把候选登记为 paper_candidate（仅
paper trading，等待模拟复盘）；不合格时只留下本次评价记录。champion 只能
由 research.paper_validation 的复盘门写入或撤销，evaluate 永远不写也不删。

退出码：evaluate 合格返回 0，不合格返回 1；status 始终返回 0。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REGISTRY = Path("research/artifacts/strategy-lifecycle")

# 候选目录必须齐全的产物文件。
REQUIRED_FILES = ("summary.json", "model.txt", "predictions.csv", "daily_returns.csv")

# 固定门槛，仅作记录与说明，判定逻辑同样写死在下方的校验里。
THRESHOLDS = {
    "status": "completed",
    "deployment_eligible": True,
    "quality.test.samples": ">=200",
    "results.test.model.trades": ">=30",
    "quality.test.spearman_ic": ">0",
    "results.test.model.information_ratio": ">0",
    "results.test.model.max_drawdown": ">=-0.25",
    "data.input_sha256": "64 位十六进制",
    "periods.test_entry_dates": "非空",
}

RELEASE_SCOPE = "paper_only"
SCHEMA_VERSION = 1
PAPER_CANDIDATE_STATUS = "paper_validation_pending"
HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_MISSING = object()


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def _now() -> str:
    """返回 UTC 时间戳，秒级精度。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _dig(data, *keys):
    """按路径取嵌套字段，缺失时返回哨兵 _MISSING。"""
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _jsonable(value):
    """把值转成可安全写入 JSON 的形态（缺失与非有限数字记为 null）。"""
    if value is _MISSING:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _short(value, limit: int = 60) -> str:
    """生成简短可读的实际值描述。"""
    if value is _MISSING:
        return "缺失"
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _non_empty(item) -> bool:
    """判断列表元素是否非空。"""
    if item is None:
        return False
    if isinstance(item, str):
        return bool(item.strip())
    return True


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# 候选产物与 candidate_id
# --------------------------------------------------------------------------


def _collect_artifacts(candidate_dir: Path) -> dict:
    """记录每个必需文件的 sha256 与字节数。"""
    artifacts = {}
    for name in REQUIRED_FILES:
        path = candidate_dir / name
        if not path.is_file():
            artifacts[name] = {"sha256": None, "bytes": None, "present": False, "path": str(path)}
            continue
        data = path.read_bytes()
        artifacts[name] = {
            "sha256": _sha256_bytes(data),
            "bytes": len(data),
            "present": True,
            "path": str(path),
        }
    return artifacts


def _compute_candidate_id(input_sha256: str, artifacts: dict) -> str:
    """由 input_sha256 与四个产物内容哈希确定性生成 candidate_id。"""
    digest = hashlib.sha256()
    digest.update(b"jev-quant/strategy-candidate/v1\n")
    digest.update(f"input_sha256={input_sha256}\n".encode("utf-8"))
    for name in REQUIRED_FILES:
        artifact_sha = artifacts[name]["sha256"] or "missing"
        digest.update(f"{name}:{artifact_sha}\n".encode("utf-8"))
    return digest.hexdigest()


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def _load_summary(candidate_dir: Path, reasons: list):
    """读取 summary.json，失败时记录原因并返回 None。"""
    path = candidate_dir / "summary.json"
    if not path.is_file():
        return None, path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        reasons.append(f"无法读取 summary.json: {exc}")
        return None, path
    try:
        data = json.loads(text)
    except ValueError as exc:
        reasons.append(f"summary.json 不是合法 JSON: {exc}")
        return None, path
    if not isinstance(data, dict):
        reasons.append("summary.json 顶层必须是 JSON 对象")
        return None, path
    return data, path


def _number_check(summary, keys, name, requirement, predicate, checks, reasons) -> None:
    """校验一个数值门槛：缺失、非数字、非有限数字一律拒绝。"""
    dotted = ".".join(keys)
    value = _dig(summary, *keys)
    if value is _MISSING:
        checks[name] = {"ok": False, "observed": None, "required": requirement}
        reasons.append(f"缺少字段: {dotted}")
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        checks[name] = {"ok": False, "observed": _jsonable(value), "required": requirement}
        reasons.append(f"{dotted} 必须是数字，实际为 {type(value).__name__}")
        return
    if not math.isfinite(value):
        checks[name] = {"ok": False, "observed": None, "required": requirement}
        reasons.append(f"{dotted} 不是有限数字: {value!r}")
        return
    passed = bool(predicate(value))
    checks[name] = {"ok": passed, "observed": value, "required": requirement}
    if not passed:
        reasons.append(f"{dotted}={value} 未满足门槛 {requirement}")


def _validate_summary(summary, reasons: list) -> dict:
    """逐项校验 summary 字段，返回验收明细。"""
    checks = {}
    if summary is None:
        return checks

    status_value = _dig(summary, "status")
    passed = status_value == "completed"
    checks["status_completed"] = {"ok": passed, "observed": _jsonable(status_value), "required": "completed"}
    if not passed:
        reasons.append(f"status 必须为 completed，实际为 {_short(status_value)}")

    eligible = _dig(summary, "deployment_eligible")
    passed = eligible is True
    checks["deployment_eligible"] = {
        "ok": passed,
        "observed": _jsonable(eligible),
        "required": "true（显式布尔）",
    }
    if not passed:
        reasons.append(f"deployment_eligible 必须显式为 true，实际为 {_short(eligible)}")

    _number_check(summary, ("quality", "test", "samples"), "test_samples", ">=200",
                  lambda v: v >= 200, checks, reasons)
    _number_check(summary, ("results", "test", "model", "trades"), "test_trades", ">=30",
                  lambda v: v >= 30, checks, reasons)
    _number_check(summary, ("quality", "test", "spearman_ic"), "spearman_ic", ">0",
                  lambda v: v > 0, checks, reasons)
    _number_check(summary, ("results", "test", "model", "information_ratio"), "information_ratio", ">0",
                  lambda v: v > 0, checks, reasons)
    _number_check(summary, ("results", "test", "model", "max_drawdown"), "max_drawdown", ">=-0.25",
                  lambda v: v >= -0.25, checks, reasons)

    sha = _dig(summary, "data", "input_sha256")
    passed = isinstance(sha, str) and HEX64.fullmatch(sha) is not None
    checks["input_sha256_format"] = {
        "ok": passed,
        "observed": _short(sha),
        "required": "64 位十六进制",
    }
    if not passed:
        reasons.append(f"data.input_sha256 必须是 64 位十六进制字符串，实际为 {_short(sha)}")

    dates = _dig(summary, "periods", "test_entry_dates")
    passed = isinstance(dates, list) and len(dates) > 0 and all(_non_empty(item) for item in dates)
    checks["test_entry_dates"] = {
        "ok": passed,
        "observed": len(dates) if isinstance(dates, list) else _short(dates),
        "required": "非空日期列表",
    }
    if not passed:
        reasons.append(f"periods.test_entry_dates 必须是非空日期列表，实际为 {_short(dates)}")
    return checks


def _detect_tampering(registry: Path, input_sha256, artifacts: dict) -> list:
    """比对同源已发布产物的哈希，返回冲突说明列表。

    同一 input_sha256 曾以某组内容发布过，若本次内容不同，说明候选文件在
    发布后被改动，必须拒绝，不能沿用旧 champion 掩盖改动。
    """
    if not isinstance(input_sha256, str):
        return []
    candidates_dir = registry / "candidates"
    if not candidates_dir.is_dir():
        return []

    conflicts = []
    for release_path in sorted(candidates_dir.glob("*/release.json")):
        try:
            record = json.loads(release_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("accepted") is not True:
            continue
        if record.get("input_sha256") != input_sha256:
            continue
        published = record.get("artifacts")
        if not isinstance(published, dict):
            continue
        for name in REQUIRED_FILES:
            old_entry = published.get(name)
            old_sha = old_entry.get("sha256") if isinstance(old_entry, dict) else None
            new_sha = artifacts[name]["sha256"]
            if old_sha and old_sha != new_sha:
                conflicts.append(
                    f"产物 {name} 与同源已发布版本不一致（疑似篡改）: "
                    f"{old_sha[:12]} -> {new_sha[:12] if new_sha else '缺失'}"
                )
    return conflicts


# --------------------------------------------------------------------------
# 写盘
# --------------------------------------------------------------------------


def _assert_paper_only(payload: dict) -> None:
    """写盘前保险：只允许 paper_only 且禁止 live 交易。"""
    if payload.get("releaseScope") != RELEASE_SCOPE:
        raise ValueError(f"releaseScope 必须是 {RELEASE_SCOPE}")
    if payload.get("liveTrading") is not False:
        raise ValueError("liveTrading 必须为 false")


def _atomic_write_json(path: Path, payload: dict) -> None:
    """先写同目录临时文件，fsync 后用 os.replace 原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append_event(registry: Path, event: dict) -> None:
    """追加一行事件，写后 flush + fsync。"""
    registry.mkdir(parents=True, exist_ok=True)
    path = registry / "events.jsonl"
    line = json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with open(path, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_json(path: Path):
    """读取 JSON 文件，不存在或损坏时返回 None。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _latest_review(registry: Path):
    """读取 registry/reviews 下时间戳最新的复盘记录；没有时返回 None。"""
    reviews_dir = registry / "reviews"
    if not reviews_dir.is_dir():
        return None
    newest = None
    newest_key = None
    for path in reviews_dir.glob("*/*.json"):
        key = (path.stem, str(path))
        if newest_key is None or key > newest_key:
            newest_key = key
            newest = path
    if newest is None:
        return None
    payload = _read_json(newest)
    return payload if isinstance(payload, dict) else None


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def evaluate(candidate_dir, registry=DEFAULT_REGISTRY) -> dict:
    """评价候选并写入 registry，返回本次 release 记录。"""
    candidate_dir = Path(candidate_dir).expanduser().resolve()
    registry = Path(registry).expanduser().resolve()

    reasons = []
    checks = {}

    if not candidate_dir.is_dir():
        reasons.append(f"候选目录不存在: {candidate_dir}")

    artifacts = _collect_artifacts(candidate_dir)
    missing = [name for name in REQUIRED_FILES if not artifacts[name]["present"]]
    checks["required_files"] = {
        "ok": not missing,
        "observed": missing,
        "required": list(REQUIRED_FILES),
    }
    if missing:
        reasons.append("缺少必需文件: " + ", ".join(missing))

    summary, summary_path = _load_summary(candidate_dir, reasons)
    checks.update(_validate_summary(summary, reasons))

    input_sha = _dig(summary, "data", "input_sha256") if summary is not None else _MISSING
    input_sha_label = input_sha if isinstance(input_sha, str) else "unavailable"

    if not missing:
        checkable_sha = input_sha if isinstance(input_sha, str) and HEX64.fullmatch(input_sha) else None
        conflicts = _detect_tampering(registry, checkable_sha, artifacts)
        checks["artifacts_unchanged"] = {
            "ok": not conflicts,
            "observed": conflicts,
            "required": "与同源已发布产物内容一致",
        }
        reasons.extend(conflicts)

    candidate_id = _compute_candidate_id(input_sha_label, artifacts)
    accepted = not reasons

    release = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "accepted": accepted,
        "releaseScope": RELEASE_SCOPE,
        "liveTrading": False,
        "source_summary": str(summary_path),
        "candidate_dir": str(candidate_dir),
        "input_sha256": input_sha_label,
        "artifacts": artifacts,
        "thresholds": THRESHOLDS,
        "checks": checks,
        "reasons": reasons,
    }
    _assert_paper_only(release)

    release_path = registry / "candidates" / candidate_id / "release.json"
    _atomic_write_json(release_path, release)

    # 合格只登记 paper_candidate，不碰 champion：champion 由模拟复盘门决定。
    paper_candidate = None
    if accepted:
        paper_candidate_path = registry / "paper_candidate.json"
        existing = _read_json(paper_candidate_path)
        if isinstance(existing, dict) and existing.get("candidate_id") == candidate_id:
            paper_candidate = existing  # 已登记，保持不变，保证重跑幂等
        else:
            paper_candidate = {
                "schema_version": SCHEMA_VERSION,
                "candidate_id": candidate_id,
                "status": PAPER_CANDIDATE_STATUS,
                "releaseScope": RELEASE_SCOPE,
                "liveTrading": False,
                "source_summary": str(summary_path),
                "release_path": f"candidates/{candidate_id}/release.json",
                "input_sha256": input_sha_label,
                "artifacts": artifacts,
                "checks": checks,
                "created_at": _now(),
            }
            _assert_paper_only(paper_candidate)
            _atomic_write_json(paper_candidate_path, paper_candidate)

    champion = _read_json(registry / "champion.json")
    if not isinstance(champion, dict):
        champion = None

    _append_event(
        registry,
        {
            "time": _now(),
            "candidate_id": candidate_id,
            "accepted": accepted,
            "reasons": reasons,
            "artifacts": {name: artifacts[name]["sha256"] for name in REQUIRED_FILES},
            "artifact_bytes": {name: artifacts[name]["bytes"] for name in REQUIRED_FILES},
            "input_sha256": input_sha_label,
            "source_summary": str(summary_path),
            "release_path": f"candidates/{candidate_id}/release.json",
            "paper_candidate_id": paper_candidate["candidate_id"] if paper_candidate else None,
            "champion_id": champion["candidate_id"] if champion else None,
        },
    )
    return release


def status(registry=DEFAULT_REGISTRY) -> dict:
    """返回 paper 候选、champion、最近一次评价与最新复盘；无记录时字段为 null。"""
    registry = Path(registry).expanduser().resolve()

    champion = _read_json(registry / "champion.json")
    if not isinstance(champion, dict):
        champion = None

    paper_candidate = _read_json(registry / "paper_candidate.json")
    if not isinstance(paper_candidate, dict):
        paper_candidate = None

    latest = None
    events_path = registry / "events.jsonl"
    if events_path.is_file():
        try:
            with open(events_path, "r", encoding="utf-8") as stream:
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        latest = json.loads(line)
                    except ValueError:
                        continue
        except OSError:
            latest = None

    return {
        "registry": str(registry),
        "releaseScope": RELEASE_SCOPE,
        "paperCandidate": paper_candidate,
        "champion": champion,
        "latest_evaluation": latest,
        "latestReview": _latest_review(registry),
    }


def main(argv=None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="python -m research.strategy_lifecycle",
        description="确定性策略发布门（paper-only）",
    )
    subparsers = parser.add_subparsers(dest="command")

    evaluate_parser = subparsers.add_parser("evaluate", help="评价候选并按固定门槛决定能否发布")
    evaluate_parser.add_argument("--candidate", required=True, help="候选产物目录")
    evaluate_parser.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="registry 目录")

    status_parser = subparsers.add_parser("status", help="查看 paper 候选、champion、最近评价与最新复盘")
    status_parser.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="registry 目录")

    args = parser.parse_args(argv)
    if args.command == "evaluate":
        result = evaluate(args.candidate, args.registry)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["accepted"] else 1
    if args.command == "status":
        print(json.dumps(status(args.registry), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
