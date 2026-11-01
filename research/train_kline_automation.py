"""每日刷新真实日线并重训研究模型；不连接账户或交易执行器。"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

from research import strategy_lifecycle


# 原来写死成 Kimi 临时工作目录；跟着本文件所在的仓库走（2026-11-01 迁到正式目录时改）
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(ctx):
    del ctx
    root = PROJECT_ROOT
    python = root / ".venv" / "Scripts" / "python.exe"
    output_dir = root / "research" / "artifacts" / "kline-model"
    if not python.exists():
        raise FileNotFoundError(f"项目 Python 不存在：{python}")
    completed = subprocess.run(
        [str(python), "-m", "research.kline_model", "--refresh"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
        check=False,
    )
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout)[-6000:]
        raise RuntimeError(f"K 线模型训练失败（退出码 {completed.returncode}）：{diagnostic}")
    try:
        process_result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"训练命令未返回有效 JSON：{completed.stdout[-2000:]}") from exc
    summary_path = output_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    test = summary["results"]["test"]["model"]
    quality = summary["quality"]["test"]
    data_as_of = summary["data"]["price_end"]

    # 训练产物生成后立刻过验收门：普通不达标只是研究结论，不是训练失败。
    registry = root / "research" / "artifacts" / "strategy-lifecycle"
    release = strategy_lifecycle.evaluate(output_dir, registry)
    accepted = release.get("accepted") is True
    lifecycle = {
        "evaluated": True,
        "accepted": accepted,
        "candidateId": release.get("candidate_id"),
        "releaseScope": release.get("releaseScope"),
        "liveTrading": release.get("liveTrading"),
        "reasons": list(release.get("reasons") or []),
        "registryPath": str(registry),
    }
    snapshot = strategy_lifecycle.status(registry)
    champion = snapshot.get("champion") if isinstance(snapshot, dict) else None
    # 回测合格只代表等待模拟验证，不能把 paper_candidate 误报成 champion。
    paper_champion_id = champion.get("candidate_id") if isinstance(champion, dict) else None

    return {
        "artifact": {
            "status": process_result["status"],
            "outputDir": str(output_dir),
            "reportPath": str(output_dir / "report.md"),
            "modelPath": str(output_dir / "model.txt"),
            "dataAsOf": data_as_of,
            "fetchedAt": summary["generated_at_utc"],
            "stale": data_as_of < date.today().isoformat(),
            "selectedQuantile": summary["selection"]["selected_quantile"],
            "scoreCutoff": summary["selection"]["score_cutoff_from_validation"],
            "testTotalReturn": test["total_return"],
            "testMaxDrawdown": test["max_drawdown"],
            "testSpearmanIc": quality["spearman_ic"],
            "testDailyMeanTStat": test["daily_mean_t_stat"],
            "deploymentEligible": summary["deployment_eligible"],
            "tradingConnected": False,
        },
        "lifecycle": lifecycle,
        "paperChampionId": paper_champion_id,
    }


if __name__ == "__main__":
    print(json.dumps(run(None), ensure_ascii=False, indent=2))
