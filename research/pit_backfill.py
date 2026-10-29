"""可续传的财新全 A 历史主数据回溯；默认只估算，不发起计费请求。"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import pandas as pd

PAGE_SIZE = 20
SCORE_PER_PAGE = 20
MAX_PARTITION_TOTAL = 1999
DEFAULT_RESEARCH_DIR = Path("research")
DEFAULT_OUTPUT_DIR = Path("research/artifacts/pit-backfill")
DEFAULT_HELPER = (
    Path.home()
    / "AppData/Roaming/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home"
    / "plugins/managed/xtt-public-markets-investing/skills/datasource-router"
    / "subskills/datasource-caixin/scripts/caixin_tool.py"
)


@dataclass(frozen=True)
class Partition:
    name: str
    board: str
    params: dict[str, str]
    probe_stem: str


PARTITIONS = (
    Partition("sh_main", "主板", {"stkTypePar": "101", "secMarPar": "2", "listSectPar": "1"}, "caixin-sh-main-page-1"),
    Partition("sz_main", "主板", {"stkTypePar": "101", "secMarPar": "1", "listSectPar": "1"}, "caixin-sz-main-page-1"),
    Partition("chinext", "创业板", {"stkTypePar": "101", "listSectPar": "6"}, "caixin-board-6-page-1"),
    Partition("star", "科创板", {"stkTypePar": "101", "listSectPar": "7"}, "caixin-board-7-page-1"),
    Partition("bse", "北交所", {"stkTypePar": "101", "listSectPar": "8"}, "caixin-board-8-page-1"),
)


@dataclass(frozen=True)
class Probe:
    total: int
    score_cost: int
    returned: int
    pages: int


def parse_probe_log(path: Path, expected_rows: int | None = None) -> Probe:
    text = path.read_text(encoding="utf-8")
    total_match = re.search(r"Total:\s*(\d+) records", text)
    score_match = re.search(r"Score cost:\s*(\d+) pts", text)
    returned_match = re.search(r"Returned this page:\s*(\d+) records", text)
    if not (total_match and score_match and returned_match):
        raise ValueError(f"无法从探针日志解析总数、积分或返回行数：{path}")
    total = int(total_match.group(1))
    if not 0 < total <= MAX_PARTITION_TOTAL:
        raise ValueError(
            f"分区 {path.stem} 返回 {total} 条；必须细分到 1—{MAX_PARTITION_TOTAL} 条后才能分页"
        )
    expected = min(PAGE_SIZE, total) if expected_rows is None else expected_rows
    if int(returned_match.group(1)) != expected:
        raise ValueError(f"分页返回行数应为 {expected}：{path}")
    return Probe(
        total=total,
        score_cost=int(score_match.group(1)),
        returned=int(returned_match.group(1)),
        pages=math.ceil(total / PAGE_SIZE),
    )


def read_page(path: Path, partition: Partition, expected_rows: int | None = None) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    required = {"STK_UNI_CODE", "STK_CODE", "STK_TYPE_PAR", "LIST_SECT_PAR", "LIST_DATE", "LIST_STA_PAR"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{path} 缺少字段：{sorted(missing)}")
    if frame.empty or (frame["STK_UNI_CODE"].str.strip() == "").any():
        raise ValueError(f"{path} 存在空股票统一编码")
    if frame["STK_UNI_CODE"].duplicated().any():
        raise ValueError(f"{path} 内股票统一编码重复")
    if set(frame["STK_TYPE_PAR"]) != {"A股"}:
        raise ValueError(f"{path} 混入非 A 股记录")
    if set(frame["LIST_SECT_PAR"]) != {partition.board}:
        raise ValueError(f"{path} 的板块与分区 {partition.name} 不一致")
    if expected_rows is not None and len(frame) != expected_rows:
        raise ValueError(f"{path} 应有 {expected_rows} 行，实际 {len(frame)} 行")
    return frame


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _page_expected_rows(total: int, page: int) -> int:
    return min(PAGE_SIZE, total - (page - 1) * PAGE_SIZE)


def _validate_response_total(reported: int, expected_total: int, page: int) -> None:
    expected_rows = _page_expected_rows(expected_total, page)
    # 首页 Total 是分区总数；后续页实测 Total 可能只是本页行数。
    valid = reported == expected_total or (page > 1 and reported == expected_rows)
    if not valid:
        raise ValueError(
            f"分页总数响应异常：第 {page} 页报告 {reported}，"
            f"分区总数 {expected_total}，本页应有 {expected_rows} 行"
        )


def recover_page_from_log(
    log_path: Path, target: Path, partition: Partition, total: int, page: int
) -> None:
    """从已保存原始响应恢复 CSV，避免校验修复后重复计费。"""
    payload = ast.literal_eval(log_path.read_text(encoding="utf-8"))
    preview = payload.get("data_preview", "") if isinstance(payload, dict) else ""
    if not preview:
        raise ValueError(f"原始响应没有可恢复的数据：{log_path}")
    probe = parse_probe_log(log_path, _page_expected_rows(total, page))
    _validate_response_total(probe.total, total, page)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".csv.tmp")
    temporary.write_text(preview, encoding="utf-8")
    read_page(temporary, partition, _page_expected_rows(total, page))
    os.replace(temporary, target)
    saved_log = target.with_suffix(".log")
    if not saved_log.exists():
        shutil.copyfile(log_path, saved_log)


def build_plan(research_dir: Path = DEFAULT_RESEARCH_DIR) -> dict:
    partitions = []
    for partition in PARTITIONS:
        probe_path = research_dir / f"{partition.probe_stem}.log"
        csv_path = research_dir / f"{partition.probe_stem}.csv"
        probe = parse_probe_log(probe_path)
        read_page(csv_path, partition, probe.returned)
        partitions.append({
            **asdict(partition),
            "probe_log": str(probe_path.resolve()),
            "probe_csv": str(csv_path.resolve()),
            **asdict(probe),
        })
    return {
        "api_name": "股票基本信息-通用",
        "page_size": PAGE_SIZE,
        "score_per_page": SCORE_PER_PAGE,
        "max_partition_total": MAX_PARTITION_TOTAL,
        "partitions": partitions,
        "total_records": sum(item["total"] for item in partitions),
        "total_pages": sum(item["pages"] for item in partitions),
        "estimated_total_score": sum(item["pages"] * SCORE_PER_PAGE for item in partitions),
    }


def _valid_saved_page(path: Path, partition: Partition, total: int, page: int) -> bool:
    if not path.is_file():
        return False
    try:
        read_page(path, partition, _page_expected_rows(total, page))
    except (OSError, ValueError, pd.errors.ParserError):
        return False
    return True


PageCaller = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def call_subprocess(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, timeout=90,
    )


class BackfillRunner:
    def __init__(
        self,
        plan: dict,
        output_dir: Path,
        helper: Path = DEFAULT_HELPER,
        caller: PageCaller = call_subprocess,
        pause_seconds: float = 0.0,
    ) -> None:
        self.plan = plan
        self.output_dir = output_dir
        self.helper = helper
        self.caller = caller
        self.pause_seconds = pause_seconds

    def _page_path(self, name: str, page: int) -> Path:
        return self.output_dir / "pages" / name / f"page-{page:05d}.csv"

    def _seed_probes(self) -> int:
        seeded = 0
        for item in self.plan["partitions"]:
            partition = Partition(item["name"], item["board"], item["params"], item["probe_stem"])
            target = self._page_path(item["name"], 1)
            if _valid_saved_page(target, partition, item["total"], 1):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".csv.tmp")
            shutil.copyfile(item["probe_csv"], temporary)
            read_page(temporary, partition, item["returned"])
            os.replace(temporary, target)
            seeded += 1
        return seeded

    def prepare(self) -> dict:
        seeded = self._seed_probes()
        missing = []
        saved_pages = 0
        for item in self.plan["partitions"]:
            partition = Partition(item["name"], item["board"], item["params"], item["probe_stem"])
            for page in range(1, item["pages"] + 1):
                if _valid_saved_page(self._page_path(item["name"], page), partition, item["total"], page):
                    saved_pages += 1
                else:
                    missing.append({"partition": item["name"], "page": page})
        manifest = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "complete" if not missing else "awaiting_download",
            "seeded_probe_pages": seeded,
            "saved_pages": saved_pages,
            "missing_pages": len(missing),
            "estimated_remaining_score": len(missing) * SCORE_PER_PAGE,
            "plan": self.plan,
        }
        _atomic_json(self.output_dir / "manifest.json", manifest)
        return manifest

    def execute(self, max_paid_calls: int) -> dict:
        manifest = self.prepare()
        missing = []
        for item in self.plan["partitions"]:
            for page in range(1, item["pages"] + 1):
                partition = Partition(item["name"], item["board"], item["params"], item["probe_stem"])
                if not _valid_saved_page(self._page_path(item["name"], page), partition, item["total"], page):
                    missing.append((item, partition, page))
        if missing and max_paid_calls == 0:
            raise RuntimeError(
                f"仍有 {len(missing)} 次计费请求，但本次上限为 0；未发起新请求"
            )
        if missing and not self.helper.is_file():
            raise FileNotFoundError(f"财新调用脚本不存在：{self.helper}")

        # 上限只约束本轮新增请求数，允许大批量任务分段续传。
        missing = missing[:max_paid_calls]
        completed = 0
        for item, partition, page in missing:
            target = self._page_path(item["name"], page)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_csv = target.with_suffix(".csv.tmp")
            temporary_log = target.with_suffix(".log.tmp")
            if temporary_log.is_file():
                try:
                    recover_page_from_log(
                        temporary_log, target, partition, item["total"], page
                    )
                except (OSError, ValueError, SyntaxError, TypeError):
                    pass
                else:
                    continue
            params = {**item["params"], "pageNum": str(page)}
            command = [
                sys.executable, str(self.helper), "call",
                "--api-name", self.plan["api_name"],
                "--params-json", json.dumps(params, ensure_ascii=False),
                "--file-path", str(temporary_csv.resolve()),
            ]
            attempt = {
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "partition": item["name"], "page": page, "params": params,
                "status": "failed", "score_cost": None,
            }
            try:
                result = self.caller(command)
                stdout = (result.stdout or "") + (result.stderr or "")
                temporary_log.write_text(stdout, encoding="utf-8")
                attempt["return_code"] = result.returncode
                score_match = re.search(r"Score cost:\s*(\d+) pts", stdout)
                if score_match:
                    attempt["score_cost"] = int(score_match.group(1))
                if result.returncode != 0:
                    raise RuntimeError(f"财新请求失败：{item['name']} 第 {page} 页；{stdout[-500:]}")
                probe = parse_probe_log(
                    temporary_log, _page_expected_rows(item["total"], page)
                )
                _validate_response_total(probe.total, item["total"], page)
                read_page(temporary_csv, partition, _page_expected_rows(item["total"], page))
                os.replace(temporary_csv, target)
                os.replace(temporary_log, target.with_suffix(".log"))
                attempt["status"] = "completed"
            except Exception as exc:
                attempt["error"] = str(exc)[-1000:]
                raise
            finally:
                attempt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
                ledger = self.output_dir / "request_ledger.jsonl"
                ledger.parent.mkdir(parents=True, exist_ok=True)
                with ledger.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(attempt, ensure_ascii=False, allow_nan=False) + "\n")
                temporary_csv.unlink(missing_ok=True)
            completed += 1
            if self.pause_seconds and completed < len(missing):
                import time
                time.sleep(self.pause_seconds)
        final = self.prepare()
        final["new_paid_calls"] = completed
        final["new_score_cost"] = completed * SCORE_PER_PAGE
        _atomic_json(self.output_dir / "manifest.json", final)
        return final

    def consolidate(self) -> Path:
        manifest = self.prepare()
        if manifest["missing_pages"]:
            raise RuntimeError(f"仍有 {manifest['missing_pages']} 页未下载，不生成全市场主数据")
        frames = []
        for item in self.plan["partitions"]:
            partition = Partition(item["name"], item["board"], item["params"], item["probe_stem"])
            for page in range(1, item["pages"] + 1):
                frames.append(read_page(
                    self._page_path(item["name"], page), partition,
                    _page_expected_rows(item["total"], page),
                ))
        universe = pd.concat(frames, ignore_index=True)
        if len(universe) != self.plan["total_records"]:
            raise ValueError("合并行数与分区估算总数不一致")
        if universe["STK_UNI_CODE"].duplicated().any():
            duplicates = universe.loc[universe["STK_UNI_CODE"].duplicated(), "STK_UNI_CODE"].tolist()
            raise ValueError(f"跨分区股票统一编码重复：{duplicates[:10]}")
        target = self.output_dir / "universe_all_a_history.csv"
        temporary = target.with_suffix(".csv.tmp")
        universe.to_csv(temporary, index=False)
        os.replace(temporary, target)
        return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--helper", type=Path, default=DEFAULT_HELPER)
    parser.add_argument("--execute", action="store_true", help="实际发起分页请求；省略时仅估算和复用探针")
    parser.add_argument("--max-paid-calls", type=int, default=0, help="本次允许的新计费请求硬上限")
    parser.add_argument("--pause-seconds", type=float, default=0.2)
    args = parser.parse_args()
    if args.max_paid_calls < 0 or args.pause_seconds < 0:
        parser.error("计费上限与暂停秒数不能为负")
    plan = build_plan(args.research_dir)
    runner = BackfillRunner(plan, args.output, args.helper, pause_seconds=args.pause_seconds)
    manifest = runner.execute(args.max_paid_calls) if args.execute else runner.prepare()
    result = {
        "status": manifest["status"],
        "total_records": plan["total_records"],
        "total_pages": plan["total_pages"],
        "saved_pages": manifest["saved_pages"],
        "missing_pages": manifest["missing_pages"],
        "estimated_total_score": plan["estimated_total_score"],
        "estimated_remaining_score": manifest["estimated_remaining_score"],
        "new_paid_calls": manifest.get("new_paid_calls", 0),
        "new_score_cost": manifest.get("new_score_cost", 0),
    }
    if args.execute and not manifest["missing_pages"]:
        result["universe_path"] = str(runner.consolidate().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
