"""每日刷新真实日线并重训研究模型；不连接账户或交易执行器。"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(
    r"C:\Users\Think\Documents\kimi\tasks\2026-10-26\20-47-12-0ee8631d\jev-quant"
)


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
        }
    }


if __name__ == "__main__":
    print(json.dumps(run(None), ensure_ascii=False, indent=2))
