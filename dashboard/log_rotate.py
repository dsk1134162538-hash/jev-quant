"""运行目录里只追加的 jsonl 日志按天轮转：主文件只留最近几天，更早的按月归到 runtime/archive/ 下。

起因：ai_decisions.jsonl 一行 17 KB 左右、每轮追加，11-03 涨到过 1 GB 把机器拖死（当时手工改名成 .bloated）；
11-12 体检时又到了 5 MB，没有任何截断。读它的人只要最近的：面板的决策记录、上一轮的分析、当天的盘中笔记。
收盘后任务（ai_loop._daily）每天调一次。归档只是搬家，一行都不丢；搬不成（文件被占用、磁盘错误）就原样不动，明天再试。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

KEEP_DAYS = 7           # 主文件留最近 7 个自然日：长假回来「上一轮的分析」还读得到最后几条
KEEP_ROWS = 30          # 再怎么旧也至少留最后这么多行


def _day_of(line: str) -> str | None:
    try:
        ts = str(json.loads(line).get("ts") or "")
    except ValueError:
        return None
    return ts[:10] if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-" else None


def rotate_jsonl(path: Path, now: datetime, keep_days: int = KEEP_DAYS, keep_rows: int = KEEP_ROWS,
                 archive_dir: Path | None = None) -> dict:
    """把 path 里 ts 早于 now - keep_days 的行搬到 archive_dir/<文件名>-YYYY-MM.jsonl。返回 {"moved", "kept"}（或 {"error"}）。
    没有 ts、解析不了的行留在主文件里；最后 keep_rows 行无论多旧都留着。"""
    path = Path(path)
    if not path.exists():
        return {"moved": 0, "kept": 0}
    cutoff = (now - timedelta(days=keep_days)).date().isoformat()
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        days = [_day_of(ln) for ln in lines]
        protect = len(lines) - keep_rows
        old = [i for i, d in enumerate(days) if d is not None and d < cutoff and i < protect]
        if not old:
            return {"moved": 0, "kept": len(lines)}
        archive_dir = Path(archive_dir or path.parent / "archive")
        archive_dir.mkdir(parents=True, exist_ok=True)
        by_month: dict[str, list[str]] = {}
        for i in old:
            by_month.setdefault(days[i][:7], []).append(lines[i])
        for month, rows in by_month.items():                      # 先写归档，再改主文件：中途出错最多是重复，不会丢
            with open(archive_dir / f"{path.stem}-{month}.jsonl", "a", encoding="utf-8") as f:
                f.write("\n".join(rows) + "\n")
        gone = set(old)
        keep = [ln for i, ln in enumerate(lines) if i not in gone]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return {"moved": len(old), "kept": len(keep)}
    except OSError as e:
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}
