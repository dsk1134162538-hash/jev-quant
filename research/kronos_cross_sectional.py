"""Kronos-small 零样本横截面对照；复用全 A PIT 样本、滚动测试折和评价器。"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from research.cross_sectional_model import (
    EvaluationConfig,
    WalkForwardConfig,
    _file_hash,
    _json_safe,
    _summarize_evaluation,
    evaluate_scores,
    make_walk_forward_splits,
)
from research.run_pit_folds import _atomic_json

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path("research/artifacts/pit-kronos-small")
DEFAULT_SMOKE_OUTPUT = Path("research/artifacts/pit-kronos-small-smoke")
DEFAULT_KRONOS_ROOT = ROOT / "third_party" / "Kronos"
MODEL_REVISION = "901c26c1332695a2a8f243eb2f37243a37bea320"
TOKENIZER_REVISION = "0e0117387f39004a9016484a186a908917e22426"
PRICE_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class KronosConfig:
    lookback: int = 512
    prediction_days: int = 6
    batch_size: int = 16
    sample_count: int = 1
    temperature: float = 1.0
    top_k: int = 1
    top_p: float = 1.0
    seed: int = 42

    def validate(self) -> None:
        if not 2 <= self.lookback <= 512:
            raise ValueError("Kronos-small lookback 必须在 2—512 之间")
        if self.prediction_days != 6:
            raise ValueError("同口径对照必须预测 T+1 至 T+6 六根日线")
        if self.batch_size <= 0 or self.sample_count != 1 or self.top_k != 1:
            raise ValueError("对照冒烟固定单路径、top_k=1，batch_size 必须为正")


def future_timestamps(
    signal_date: pd.Timestamp, calendar: pd.DatetimeIndex, prediction_days: int = 6
) -> pd.Series:
    """未来时间只作为模型的时间戳输入，不提供任何未来价格。"""
    calendar = pd.DatetimeIndex(sorted(pd.to_datetime(calendar)))
    position = int(calendar.searchsorted(pd.Timestamp(signal_date), side="right"))
    future = calendar[position:position + prediction_days]
    if len(future) != prediction_days:
        raise ValueError(f"{pd.Timestamp(signal_date).date()} 之后不足 {prediction_days} 个市场交易日")
    return pd.Series(future).reset_index(drop=True)


def prepare_histories(stocks: pd.DataFrame) -> dict[str, tuple[pd.DataFrame, pd.DatetimeIndex]]:
    """每只股票只排序并构造一次日期索引，供所有信号日复用。"""
    histories = {}
    for code, bars in stocks.groupby("code", sort=False):
        bars = bars.sort_values("date").reset_index(drop=True)
        dates = pd.DatetimeIndex(pd.to_datetime(bars["date"]))
        histories[str(code)] = (bars, dates)
    return histories


def history_context(
    bars: pd.DataFrame,
    signal_date: pd.Timestamp,
    lookback: int = 512,
    dates: pd.DatetimeIndex | None = None,
) -> pd.DataFrame:
    """只截取信号日及以前的实际行情，不使用未来复权因子或未来价格。"""
    if dates is None:
        bars = bars.sort_values("date").reset_index(drop=True)
        dates = pd.DatetimeIndex(pd.to_datetime(bars["date"]))
    end = int(dates.searchsorted(pd.Timestamp(signal_date), side="right"))
    context = bars.iloc[max(0, end - lookback):end].copy().reset_index(drop=True)
    if len(context) < 2 or context.empty or context["date"].iloc[-1] != pd.Timestamp(signal_date):
        raise ValueError(f"{pd.Timestamp(signal_date).date()} 缺少信号日历史行情")
    if context[PRICE_COLUMNS].isna().any().any():
        raise ValueError("Kronos 历史上下文存在缺失 OHLCV")
    if (dates[max(0, end - lookback):end] > pd.Timestamp(signal_date)).any():
        raise AssertionError("Kronos 上下文发生前视泄漏")
    return context


def forecast_score(forecast: pd.DataFrame) -> tuple[float, float, float]:
    """以预测 T+1 开盘到 T+6 开盘的收益作为横截面排序分数。"""
    if len(forecast) != 6 or "open" not in forecast:
        raise ValueError("Kronos 预测必须包含六根日线的 open")
    entry_open = float(forecast["open"].iloc[0])
    exit_open = float(forecast["open"].iloc[-1])
    if not np.isfinite([entry_open, exit_open]).all() or min(entry_open, exit_open) <= 0:
        raise ValueError("Kronos 预测开盘价不是有限正数")
    return exit_open / entry_open - 1.0, entry_open, exit_open


def select_test_samples(
    samples: pd.DataFrame,
    fold: int,
    walk_config: WalkForwardConfig = WalkForwardConfig(),
    smoke_days: int | None = None,
    smoke_names: int | None = None,
) -> pd.DataFrame:
    splits = make_walk_forward_splits(samples["signal_date"].unique(), walk_config)
    if not 1 <= fold <= len(splits):
        raise ValueError(f"折号必须在 1—{len(splits)} 之间")
    test = samples[samples["signal_date"].isin(splits[fold - 1]["test"])].copy()
    if smoke_days is None and smoke_names is None:
        return test.sort_values(["signal_date", "code"]).reset_index(drop=True)
    if not smoke_days or not smoke_names or smoke_names < 5:
        raise ValueError("冒烟模式必须同时指定正整数天数和至少 5 只每日证券")
    dates = sorted(test["signal_date"].unique())[:smoke_days]
    selected = []
    for _, group in test[test["signal_date"].isin(dates)].groupby("signal_date", sort=True):
        group = group.sort_values("code")
        positions = np.unique(np.linspace(0, len(group) - 1, num=min(smoke_names, len(group)), dtype=int))
        selected.append(group.iloc[positions])
    return pd.concat(selected).sort_values(["signal_date", "code"]).reset_index(drop=True)


def load_predictor(kronos_root: Path = DEFAULT_KRONOS_ROOT, device: str = "cpu"):
    """从项目内缓存加载固定 revision；不会在请求研究面板时调用。"""
    cache = ROOT / ".cache" / "huggingface"
    os.environ.setdefault("HF_HOME", str(cache))
    os.environ.setdefault("HF_HUB_CACHE", str(cache / "hub"))
    sys.path.insert(0, str(kronos_root))
    from model import Kronos, KronosPredictor, KronosTokenizer

    tokenizer = KronosTokenizer.from_pretrained(
        "NeoQuasar/Kronos-Tokenizer-base", revision=TOKENIZER_REVISION
    )
    model = Kronos.from_pretrained("NeoQuasar/Kronos-small", revision=MODEL_REVISION)
    tokenizer.eval()
    model.eval()
    return KronosPredictor(model, tokenizer, device=device, max_context=512)


def _prediction_batches(test, histories, calendar, config: KronosConfig, skipped=None):
    """按精确上下文长度跨信号日凑批；只保存轻量元数据，不预存全部行情窗口。

    全量五折若需限制元数据内存，应进一步改为外部分桶；这里不缩短上下文或预测期。
    """
    skipped = skipped or set()
    metadata = []
    futures = {}
    for row in test.itertuples():
        code = str(row.code)
        signal_date = pd.Timestamp(row.signal_date)
        key = (code, str(signal_date.date()))
        if key in skipped:
            continue
        _, dates = histories[code]
        end = int(dates.searchsorted(signal_date, side="right"))
        context_length = min(config.lookback, end)
        metadata.append((row.Index, code, signal_date, context_length))

    metadata.sort(key=lambda item: (item[3], item[2], item[1], item[0]))
    pending = []
    pending_length = None
    for index, code, signal_date, context_length in metadata:
        if pending_length != context_length and pending:
            yield pending
            pending = []
        pending_length = context_length
        bars, dates = histories[code]
        context = history_context(bars, signal_date, config.lookback, dates)
        if signal_date not in futures:
            futures[signal_date] = future_timestamps(signal_date, calendar, config.prediction_days)
        pending.append((
            index,
            context[PRICE_COLUMNS],
            context["date"].reset_index(drop=True),
            futures[signal_date],
        ))
        if len(pending) == config.batch_size:
            yield pending
            pending = []
    if pending:
        yield pending


def _existing_scores(path: Path) -> dict[tuple[str, str], dict]:
    if not path.exists():
        return {}
    records = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                records[(record["code"], record["signal_date"])] = record
    return records


def run_fold(
    samples_path: Path,
    stock_path: Path,
    benchmark_path: Path,
    output_dir: Path,
    fold: int,
    config: KronosConfig = KronosConfig(),
    walk_config: WalkForwardConfig = WalkForwardConfig(),
    evaluation_config: EvaluationConfig = EvaluationConfig(),
    smoke_days: int | None = None,
    smoke_names: int | None = None,
    predictor=None,
) -> dict:
    config.validate()
    smoke = smoke_days is not None or smoke_names is not None
    samples = pd.read_pickle(samples_path)
    test = select_test_samples(samples, fold, walk_config, smoke_days, smoke_names)
    del samples
    stocks = pd.read_csv(
        stock_path,
        dtype={"code": str, **{column: np.float32 for column in PRICE_COLUMNS}},
        parse_dates=["date"],
    )
    histories = prepare_histories(stocks)
    del stocks
    calendar = pd.DatetimeIndex(pd.read_csv(benchmark_path, usecols=["date"])["date"])
    predictor = predictor or load_predictor()
    random.seed(config.seed)
    np.random.seed(config.seed)
    import torch
    torch.manual_seed(config.seed)

    fold_dir = output_dir / "folds" / f"fold_{fold:02d}"
    fold_dir.mkdir(parents=True, exist_ok=True)
    score_path = fold_dir / "scores.jsonl"
    existing = _existing_scores(score_path)
    skipped = set(existing)
    with score_path.open("a", encoding="utf-8") as handle:
        for batch_number, batch in enumerate(_prediction_batches(test, histories, calendar, config, skipped), start=1):
            indices, frames, x_stamps, y_stamps = zip(*batch)
            with torch.no_grad():
                forecasts = predictor.predict_batch(
                    df_list=list(frames), x_timestamp_list=list(x_stamps), y_timestamp_list=list(y_stamps),
                    pred_len=config.prediction_days, T=config.temperature, top_k=config.top_k,
                    top_p=config.top_p, sample_count=config.sample_count, verbose=False,
                )
            for index, forecast in zip(indices, forecasts):
                row = test.loc[index]
                key = (str(row["code"]), str(row["signal_date"].date()))
                if key in skipped:
                    continue
                score, entry_open, exit_open = forecast_score(forecast)
                record = {
                    "code": key[0], "signal_date": key[1], "score": score,
                    "predicted_entry_open": entry_open, "predicted_exit_open": exit_open,
                }
                handle.write(json.dumps(record, allow_nan=False) + "\n")
                existing[key] = record
            handle.flush()
            if batch_number % 10 == 0:
                print(f"Kronos fold={fold} batches={batch_number} scores={len(existing)}/{len(test)}", flush=True)

    scores = pd.DataFrame([
        {"code": code, "signal_date": pd.Timestamp(date), "score": record["score"]}
        for (code, date), record in existing.items()
    ])
    scored = test.merge(scores, on=["code", "signal_date"], how="left", validate="one_to_one")
    if scored["score"].isna().any():
        raise RuntimeError("Kronos 评分覆盖不完整，拒绝评价或汇总")
    scored["fold"] = fold
    metrics, artifacts = evaluate_scores(scored, evaluation_config)
    scored.to_pickle(fold_dir / "predictions.pkl")
    artifacts["daily_ic"].to_csv(fold_dir / "daily_ic.csv", index=False)
    artifacts["quantile_returns"].to_csv(fold_dir / "quantile_returns.csv", index=False)
    artifacts["portfolio"].to_csv(fold_dir / "portfolio_returns.csv", index=False)
    artifacts["selected"].to_pickle(fold_dir / "selected.pkl")
    result = {
        "status": "smoke_completed" if smoke else "fold_completed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "deployment_eligible": False,
        "comparison_eligible": not smoke,
        "fold": fold,
        "smoke": {"days": smoke_days, "names_per_day": smoke_names, "rows": len(test)} if smoke else None,
        "model": {
            "algorithm": "Kronos-small zero-shot autoregressive OHLCV forecast",
            "model_revision": MODEL_REVISION, "tokenizer_revision": TOKENIZER_REVISION,
            "context_columns": PRICE_COLUMNS, "stock_code_used_as_feature": False,
            "fine_tuned": False, **asdict(config),
        },
        "test": metrics,
        "code_sha256": _file_hash((Path(__file__),)),
    }
    _atomic_json(fold_dir / "metrics.json", result)
    _atomic_json(fold_dir / "completed.json", {"status": result["status"], "fold": fold})
    return result


def finalize(samples_path: Path, data_summary_path: Path, output_dir: Path,
             evaluation_config: EvaluationConfig = EvaluationConfig()) -> dict:
    samples = pd.read_pickle(samples_path)
    fold_count = len(make_walk_forward_splits(samples["signal_date"].unique()))
    del samples
    predictions, daily, quantiles, portfolios, selected, fold_results = [], [], [], [], [], []
    for fold in range(1, fold_count + 1):
        fold_dir = output_dir / "folds" / f"fold_{fold:02d}"
        metrics = json.loads((fold_dir / "metrics.json").read_text(encoding="utf-8"))
        if metrics["status"] != "fold_completed" or not metrics["comparison_eligible"]:
            raise RuntimeError("冒烟折不能冒充全量对照结果")
        fold_results.append(metrics)
        predictions.append(pd.read_pickle(fold_dir / "predictions.pkl"))
        daily.append(pd.read_csv(fold_dir / "daily_ic.csv", parse_dates=["signal_date"]))
        quantiles.append(pd.read_csv(fold_dir / "quantile_returns.csv", parse_dates=["signal_date"]))
        portfolios.append(pd.read_csv(fold_dir / "portfolio_returns.csv", parse_dates=["signal_date", "entry_date", "exit_date"]))
        selected.append(pd.read_pickle(fold_dir / "selected.pkl"))
    prediction_frame = pd.concat(predictions, ignore_index=True)
    if prediction_frame.duplicated(["signal_date", "code"]).any():
        raise AssertionError("Kronos 滚动测试键发生重叠")
    aggregate = _summarize_evaluation(
        prediction_frame, pd.concat(daily, ignore_index=True), pd.concat(quantiles, ignore_index=True),
        pd.concat(portfolios, ignore_index=True), evaluation_config,
    )
    data_summary = json.loads(data_summary_path.read_text(encoding="utf-8"))
    summary = {
        "status": "completed", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "deployment_eligible": False, "comparison_eligible": True,
        "model": fold_results[0]["model"], "folds": fold_results, "out_of_sample": aggregate,
        "data": data_summary, "acceptance": {"live_or_paper_trading_approved": False},
        "limitations": [
            "Zero-shot comparison only; no Kronos fine-tuning or champion replacement.",
            "Amount is derived by the official predictor from OHLC and volume, not observed turnover.",
            "Daily forecasts do not model limit-lock, partial fills, market impact or minimum commission.",
        ],
    }
    _atomic_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=Path("research/artifacts/pit-full-market-baseline/samples.pkl"))
    parser.add_argument("--stock-data", type=Path, default=Path("research/artifacts/akshare-daily-backfill/stock_daily.csv"))
    parser.add_argument("--benchmark-data", type=Path, default=Path("research/artifacts/akshare-daily-backfill/benchmark_daily.csv"))
    parser.add_argument("--data-summary", type=Path, default=Path("research/artifacts/pit-full-market-baseline/data_summary.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fold", type=int, default=1)
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--smoke-days", type=int)
    parser.add_argument("--smoke-names", type=int)
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()
    smoke = args.smoke_days is not None or args.smoke_names is not None
    output = args.output or (DEFAULT_SMOKE_OUTPUT if smoke else DEFAULT_OUTPUT)
    if args.finalize:
        result = finalize(args.samples, args.data_summary, output)
    else:
        result = run_fold(
            args.samples, args.stock_data, args.benchmark_data, output, args.fold,
            config=KronosConfig(batch_size=args.batch_size),
            smoke_days=args.smoke_days, smoke_names=args.smoke_names,
        )
    print(json.dumps(_json_safe(result), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
