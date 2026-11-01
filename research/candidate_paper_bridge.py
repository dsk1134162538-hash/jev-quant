"""训练候选（paper_candidate）到订阅模拟闭环的桥接。

只读取 registry 与其登记的候选产物，不联网、不下单、不触发训练：把当日
候选股从 predictions.csv 中确定性挑选出来，供 dashboard 的订阅模拟使用。
"""

from __future__ import annotations

import csv
import math
import re
from datetime import date, datetime
from pathlib import Path

from research.strategy_lifecycle import (
    DEFAULT_REGISTRY,
    PAPER_CANDIDATE_STATUS,
    RELEASE_SCOPE,
    REQUIRED_FILES,
    _collect_artifacts,
    _read_json,
)

PREDICTION_FIELDS = (
    "signal_date", "entry_date", "exit_date", "code", "split", "score",
    "target_gross_return", "target_net_return", "selected",
)
SELECTION_ARTIFACT = "predictions.csv"
_PREFIX = re.compile(r"^(?:sh|sz|bj)(?=\d)")
_TRUE_TEXT = {"true", "1", "yes", "y", "t"}


def _trade_date(value) -> date:
    """把 trade_date 归一为 date；只接受 ISO 日期（可带时间）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError as exc:
        raise ValueError(f"trade_date 不是 ISO 日期: {value!r}") from exc


def _six_digit(value) -> str:
    """把交易所前缀/不足六位的代码归一为六位数字字符串；无法归一返回空串。"""
    text = _PREFIX.sub("", str(value or "").strip().lower())
    if not text.isdigit() or len(text) > 6:
        return ""
    return text.zfill(6)


def _score(value):
    """解析有限分数；非法或非有限返回 None。"""
    try:
        score = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def _is_selected(value) -> bool:
    return str(value or "").strip().lower() in _TRUE_TEXT


def _entry_date(value):
    """解析 predictions 中的 entry_date；非法或非 ISO 日期返回 None。"""
    text = str(value or "").strip()
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _assert_paper_candidate(candidate: dict) -> None:
    """只接受 paper-only、未上实盘、仍在模拟验证中的候选。"""
    candidate_id = candidate.get("candidate_id")
    if candidate.get("releaseScope") != RELEASE_SCOPE:
        raise ValueError(f"候选 {candidate_id} 的 releaseScope 必须是 {RELEASE_SCOPE}")
    if candidate.get("liveTrading") is not False:
        raise ValueError(f"候选 {candidate_id} 的 liveTrading 必须为 false")
    if candidate.get("status") != PAPER_CANDIDATE_STATUS:
        raise ValueError(f"候选 {candidate_id} 的 status 必须是 {PAPER_CANDIDATE_STATUS}")


def _verify_artifacts(candidate: dict, artifacts: dict, candidate_dir: Path) -> None:
    """用 strategy_lifecycle 重新哈希四个必需文件，与登记值不一致即 fail closed。"""
    recorded = candidate.get("artifacts")
    if not isinstance(recorded, dict):
        raise ValueError("paper_candidate 缺少 artifacts 登记，拒绝使用")
    for name in REQUIRED_FILES:
        entry = recorded.get(name)
        if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
            raise ValueError(f"paper_candidate 未登记 {name} 的 sha256，拒绝使用")
        current = artifacts[name]["sha256"]
        if not artifacts[name]["present"]:
            raise ValueError(f"候选目录缺少 {name}: {candidate_dir}")
        if current != entry["sha256"]:
            raise ValueError(
                f"候选产物 {name} 与登记哈希不一致（疑似篡改），拒绝使用: "
                f"{entry['sha256'][:12]} -> {current[:12]}")


def _selection_limits(summary: dict) -> tuple:
    """读取 summary.selection 的分数门槛与每日取数，缺失即失败。"""
    selection = summary.get("selection")
    if not isinstance(selection, dict):
        raise ValueError("候选 summary.json 缺少 selection 配置")
    cutoff = _score(selection.get("score_cutoff_from_validation"))
    if cutoff is None:
        raise ValueError("summary.selection.score_cutoff_from_validation 必须是有限数字")
    top_k = selection.get("top_k_per_signal_day")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError("summary.selection.top_k_per_signal_day 必须是正整数")
    return cutoff, top_k


def _read_predictions(path: Path) -> list:
    """读取 predictions.csv，字段不全时抛出。"""
    try:
        with open(path, "r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            missing = [name for name in PREDICTION_FIELDS if name not in (reader.fieldnames or [])]
            if missing:
                raise ValueError("predictions.csv 缺少字段: " + ", ".join(missing))
            return list(reader)
    except OSError as exc:
        raise ValueError(f"无法读取 predictions.csv: {exc}") from exc


def load_candidate_selection(trade_date, registry=DEFAULT_REGISTRY):
    """返回当日候选池；没有 paper_candidate 时返回 None。

    paper_candidate 存在但登记不合法、产物哈希不符或产物不可解析时抛
    ValueError（fail closed），绝不退回到普通候选池。
    """
    day = _trade_date(trade_date)
    root = Path(registry).expanduser().resolve()
    candidate = _read_json(root / "paper_candidate.json")
    if not isinstance(candidate, dict):
        return None

    _assert_paper_candidate(candidate)
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    if not candidate_id:
        raise ValueError("paper_candidate 缺少 candidate_id")
    source = candidate.get("source_summary")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("paper_candidate 缺少 source_summary")
    candidate_dir = Path(source).expanduser().parent

    artifacts = _collect_artifacts(candidate_dir)
    _verify_artifacts(candidate, artifacts, candidate_dir)
    summary = _read_json(candidate_dir / "summary.json")
    if not isinstance(summary, dict):
        raise ValueError("候选 summary.json 缺失或不是 JSON 对象")
    cutoff, top_k = _selection_limits(summary)
    rows = _read_predictions(candidate_dir / SELECTION_ARTIFACT)

    picked = []
    for row in rows:
        if str(row.get("split") or "").strip() != "test":
            continue
        if _entry_date(row.get("entry_date")) != day:
            continue
        if not _is_selected(row.get("selected")):
            continue
        score = _score(row.get("score"))
        if score is None or score < cutoff:
            continue
        code = _six_digit(row.get("code"))
        if code:
            picked.append((score, code))

    picked.sort(key=lambda item: (-item[0], item[1]))
    codes = []
    for _, code in picked:
        if code not in codes:
            codes.append(code)
        if len(codes) >= top_k:
            break

    return {
        "candidateId": candidate_id,
        "codes": codes,
        "entryDate": day.isoformat(),
        "scoreCutoff": cutoff,
        "topK": top_k,
        "artifactSha256": artifacts[SELECTION_ARTIFACT]["sha256"],
    }
