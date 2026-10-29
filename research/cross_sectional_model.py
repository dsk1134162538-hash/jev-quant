"""五日横截面相对强弱模型：滚动 LightGBM、RankIC 与扣费组合验证。

仅读取历史研究数据，不连接实时盯盘、账户或交易执行链路。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from research.kline_model import (
    BUY_COST,
    FEATURE_COLUMNS,
    SELL_COST,
    _benchmark_features,
    _json_safe,
    _stock_features,
)
from research.pit_validation import apply_pit_universe

HOLDING_DAYS = 5
DEFAULT_TRAIN_DAYS = 504
DEFAULT_VALIDATION_DAYS = 126
DEFAULT_TEST_DAYS = 126
DEFAULT_EMBARGO_DAYS = HOLDING_DAYS + 1
DEFAULT_QUANTILES = 5
MIN_CROSS_SECTION = 5


@dataclass(frozen=True)
class WalkForwardConfig:
    train_days: int = DEFAULT_TRAIN_DAYS
    validation_days: int = DEFAULT_VALIDATION_DAYS
    test_days: int = DEFAULT_TEST_DAYS
    step_days: int = DEFAULT_TEST_DAYS
    embargo_days: int = DEFAULT_EMBARGO_DAYS

    def validate(self) -> None:
        values = asdict(self)
        if any(not isinstance(value, int) or value <= 0 for value in values.values()):
            raise ValueError("滚动窗口参数必须全部为正整数")
        if self.embargo_days < HOLDING_DAYS + 1:
            raise ValueError("隔离区必须覆盖完整建仓与五日退出窗口")


@dataclass(frozen=True)
class EvaluationConfig:
    quantiles: int = DEFAULT_QUANTILES
    rebalance_every: int = HOLDING_DAYS
    buy_cost: float = BUY_COST
    sell_cost: float = SELL_COST

    def validate(self) -> None:
        if self.quantiles < 2 or self.rebalance_every <= 0:
            raise ValueError("分层数和调仓间隔不合法")
        if min(self.buy_cost, self.sell_cost) < 0:
            raise ValueError("交易成本不能为负")


def _forward_labels(
    bars: pd.DataFrame, calendar: Iterable[pd.Timestamp] | None = None
) -> pd.DataFrame:
    """T 日收盘可见，T+1 开盘建仓，T+6 开盘退出，共持有五日。

    全市场样本按市场交易日对齐；建仓或退出日停牌时不伪造可成交标签。
    """
    bars = bars.sort_values("date").reset_index(drop=True).copy()
    bars["date"] = pd.to_datetime(bars["date"])
    if calendar is not None:
        market_dates = pd.DatetimeIndex(sorted(pd.to_datetime(list(calendar))))
        # 仅为该段上市历史的实际行情区间补市场日历，避免不同上市身份产生重复键。
        market_dates = market_dates[
            (market_dates >= bars["date"].min()) & (market_dates <= bars["date"].max())
        ]
        bars = bars.set_index("date").reindex(market_dates).rename_axis("date").reset_index()
        bars["code"] = bars["code"].ffill().bfill()
    out = bars[["date", "code"]].copy()
    out["entry_date"] = bars["date"].shift(-1)
    out["exit_date"] = bars["date"].shift(-(HOLDING_DAYS + 1))
    entry_open = bars["open"].shift(-1)
    exit_open = bars["open"].shift(-(HOLDING_DAYS + 1))
    out["gross_return"] = exit_open / entry_open - 1.0
    return out


def _benchmark_forward_returns(benchmark: pd.DataFrame) -> pd.DataFrame:
    bars = benchmark.sort_values("date").reset_index(drop=True).copy()
    bars["date"] = pd.to_datetime(bars["date"])
    out = bars[["date"]].copy()
    entry_open = bars["open"].shift(-1)
    exit_open = bars["open"].shift(-(HOLDING_DAYS + 1))
    out["benchmark_return"] = exit_open / entry_open - 1.0
    return out


def build_samples(stocks: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    """先按当日完整可评价股票池构造横截面标签，再检查特征覆盖率。"""
    if stocks.empty or benchmark.empty:
        raise ValueError("训练行情为空")
    required = {"date", "code", "open", "high", "low", "close", "volume"}
    if not required.issubset(stocks.columns) or not {"date", "open", "close", "volume"}.issubset(benchmark.columns):
        raise ValueError("行情字段不完整")
    if stocks.duplicated(["code", "date"]).any():
        raise ValueError("股票行情存在重复代码与日期")

    # 同一代码重新上市时，老身份的行情不得进入新身份的滚动特征。
    group_column = "STK_UNI_CODE" if "STK_UNI_CODE" in stocks.columns else "code"
    stock_feature_columns = [
        column for column in FEATURE_COLUMNS
        if not column.startswith(("benchmark_", "relative_ret_"))
    ]
    feature_frames = []
    for _, bars in stocks.groupby(group_column, sort=True):
        frame = _stock_features(bars)[["date", "code", *stock_feature_columns]]
        frame[stock_feature_columns] = frame[stock_feature_columns].astype(np.float32)
        feature_frames.append(frame)
    features = pd.concat(feature_frames, ignore_index=True)
    del feature_frames
    labels = pd.concat(
        (
            _forward_labels(bars, benchmark["date"])
            for _, bars in stocks.groupby(group_column, sort=True)
        ),
        ignore_index=True,
    )
    samples = labels.merge(features, on=["date", "code"], how="left", validate="one_to_one")
    del features, labels
    samples = samples.merge(
        _benchmark_features(benchmark), on="date", how="left", validate="many_to_one"
    )
    samples = samples.merge(
        _benchmark_forward_returns(benchmark), on="date", how="left", validate="many_to_one"
    )
    samples["relative_ret_5"] = samples["ret_5"] - samples["benchmark_ret_5"]
    samples["relative_ret_20"] = samples["ret_20"] - samples["benchmark_ret_20"]
    samples["excess_return"] = (
        (1.0 + samples["gross_return"]) / (1.0 + samples["benchmark_return"]) - 1.0
    )
    samples = samples.replace([np.inf, -np.inf], np.nan)
    samples["raw_universe_size"] = samples.groupby("date")["excess_return"].transform("count")
    # 排名只使用同一信号日已经实现的横截面标签，不使用任何未来日期信息。
    samples["target_rank"] = samples.groupby("date")["excess_return"].rank(
        method="average", pct=True
    )
    samples["signal_date"] = samples["date"]
    required_sample = [
        "entry_date", "exit_date", "gross_return", "benchmark_return",
        "excess_return", "target_rank",
    ]
    samples["feature_complete"] = samples[FEATURE_COLUMNS].notna().all(axis=1)
    # LightGBM 原生处理缺失特征；新股历史不足不应被误删或计入覆盖率损失。
    samples = samples.dropna(subset=required_sample).reset_index(drop=True)
    if samples.empty:
        raise ValueError("五日标签完整样本为空")
    if (samples["raw_universe_size"] < MIN_CROSS_SECTION).any():
        raise ValueError("至少一个信号日的可评价股票池小于五只，无法构造稳定横截面")
    return samples.sort_values(["signal_date", "code"]).reset_index(drop=True)


def make_walk_forward_splits(
    calendar: Iterable[pd.Timestamp], config: WalkForwardConfig = WalkForwardConfig()
) -> list[dict[str, pd.DatetimeIndex]]:
    config.validate()
    dates = pd.DatetimeIndex(sorted(pd.to_datetime(list(calendar))))
    if dates.empty:
        raise ValueError("滚动日历为空")
    window = (
        config.train_days + config.embargo_days + config.validation_days
        + config.embargo_days + config.test_days
    )
    splits = []
    for start in range(0, len(dates) - window + 1, config.step_days):
        train_end = start + config.train_days
        validation_start = train_end + config.embargo_days
        validation_end = validation_start + config.validation_days
        test_start = validation_end + config.embargo_days
        test_end = test_start + config.test_days
        splits.append({
            "train": dates[start:train_end],
            "validation": dates[validation_start:validation_end],
            "test": dates[test_start:test_end],
        })
    if not splits:
        raise ValueError(f"滚动样本不足：至少需要 {window} 个信号日，实际 {len(dates)} 个")
    return splits


def _take_dates(samples: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    return samples[samples["signal_date"].isin(dates)].copy()


def purged_fold_samples(
    samples: pd.DataFrame, split: dict[str, pd.DatetimeIndex]
) -> dict[str, pd.DataFrame]:
    """除日历隔离外，再按实际退出日 purge，确保标签不跨区间。"""
    frames = {name: _take_dates(samples, split[name]) for name in ("train", "validation", "test")}
    for earlier, later in (("train", "validation"), ("validation", "test")):
        if frames[earlier].empty or frames[later].empty:
            raise ValueError(f"滚动折中的 {earlier} 或 {later} 样本为空")
        next_entry = frames[later]["entry_date"].min()
        frames[earlier] = frames[earlier][frames[earlier]["exit_date"] < next_entry].copy()
    if min(len(frames["train"]), len(frames["validation"]), len(frames["test"])) == 0:
        raise ValueError("purge 后训练、验证或测试样本为空")
    return frames


def fit_model(train: pd.DataFrame, validation: pd.DataFrame):
    from lightgbm import LGBMRegressor, early_stopping

    if train["signal_date"].nunique() < 60 or len(train) < 500:
        raise ValueError("滚动训练样本不足")
    if validation["signal_date"].nunique() < 20 or len(validation) < 100:
        raise ValueError("滚动验证样本不足")
    model = LGBMRegressor(
        n_estimators=500,
        learning_rate=0.025,
        num_leaves=15,
        max_depth=4,
        min_child_samples=60,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_alpha=0.5,
        reg_lambda=2.0,
        random_state=42,
        n_jobs=4,
        verbosity=-1,
    )
    model.fit(
        train[FEATURE_COLUMNS],
        train["target_rank"],
        eval_set=[(validation[FEATURE_COLUMNS], validation["target_rank"])],
        eval_metric="l2",
        callbacks=[early_stopping(40, verbose=False)],
    )
    return model


def _safe_correlation(left: pd.Series, right: pd.Series, method: str) -> float:
    if len(left) < 2 or left.nunique() < 2 or right.nunique() < 2:
        return math.nan
    return float(left.corr(right, method=method))


def _summarize_evaluation(
    frame: pd.DataFrame,
    daily_ic: pd.DataFrame,
    quantile_returns: pd.DataFrame,
    portfolio: pd.DataFrame,
    config: EvaluationConfig,
) -> dict:
    valid_ic = daily_ic["rank_ic"].dropna()
    ic_std = valid_ic.std(ddof=1)
    ic_mean = valid_ic.mean()
    ic_se = ic_std / math.sqrt(len(valid_ic)) if len(valid_ic) > 1 else math.nan
    quantile_means = quantile_returns.groupby("quantile")["gross_return"].mean()
    quantile_excess = quantile_returns.groupby("quantile")["excess_return"].mean()

    net = portfolio["net_return"].astype(float)
    gross = portfolio["gross_return"].astype(float)
    equity = (1.0 + net).cumprod()
    total_return = float(equity.iloc[-1] - 1.0) if len(equity) else math.nan
    annualized = (
        float((1.0 + total_return) ** ((252.0 / config.rebalance_every) / len(net)) - 1.0)
        if len(net) and 1.0 + total_return > 0 else (-1.0 if len(net) else math.nan)
    )
    drawdown = equity / equity.cummax().clip(lower=1.0) - 1.0
    net_std = net.std(ddof=1)
    net_se = net_std / math.sqrt(len(net)) if len(net) > 1 else math.nan
    raw_coverage = float(frame.groupby("signal_date")["raw_universe_size"].first().sum())
    coverage = float(len(frame) / raw_coverage) if raw_coverage > 0 else math.nan
    return {
        "rank_ic": {
            "days": int(len(valid_ic)),
            "mean": float(ic_mean),
            "std": float(ic_std),
            "t_stat": float(ic_mean / ic_se) if ic_se > 0 else None,
            "icir_daily": float(ic_mean / ic_std) if ic_std > 0 else None,
            "positive_day_ratio": float((valid_ic > 0).mean()) if len(valid_ic) else None,
        },
        "coverage": {
            "scored_rows": int(len(frame)),
            "raw_universe_rows": int(raw_coverage),
            "ratio": coverage,
            "mean_scored_names": float(frame.groupby("signal_date").size().mean()),
            "mean_raw_names": float(frame.groupby("signal_date")["raw_universe_size"].first().mean()),
        },
        "quantiles": {
            "average_gross_returns": {str(int(key)): float(value) for key, value in quantile_means.items()},
            "average_excess_returns": {str(int(key)): float(value) for key, value in quantile_excess.items()},
            "top_minus_bottom_diagnostic": (
                float(quantile_means.iloc[-1] - quantile_means.iloc[0]) if len(quantile_means) > 1 else None
            ),
            "monotonicity_spearman": _safe_correlation(
                pd.Series(quantile_means.index.astype(float)),
                pd.Series(quantile_means.to_numpy()),
                "spearman",
            ),
            "executable_long_short_assumed": False,
        },
        "top_quantile_nonoverlapping": {
            "periods": int(len(net)),
            "total_net_return": total_return,
            "annualized_net_return": annualized,
            "max_drawdown": float(drawdown.min()) if len(drawdown) else None,
            "average_period_gross_return": float(gross.mean()) if len(gross) else None,
            "average_period_net_return": float(net.mean()) if len(net) else None,
            "period_mean_t_stat": float(net.mean() / net_se) if net_se > 0 else None,
            "average_one_way_turnover": float(portfolio["turnover"].mean()) if len(portfolio) else None,
            "average_positions": float(portfolio["positions"].mean()) if len(portfolio) else None,
            "buy_cost": config.buy_cost,
            "sell_cost": config.sell_cost,
            "cost_method": "period gross return minus turnover times buy plus sell cost",
            "rebalance_every_signal_days": config.rebalance_every,
        },
    }


def evaluate_scores(
    frame: pd.DataFrame, config: EvaluationConfig = EvaluationConfig()
) -> tuple[dict, dict[str, pd.DataFrame]]:
    config.validate()
    if frame.empty:
        raise ValueError("评分样本为空")
    required = {"signal_date", "entry_date", "exit_date", "code", "score", "gross_return", "excess_return", "raw_universe_size"}
    if not required.issubset(frame.columns):
        raise ValueError("评分样本字段不完整")

    daily_rows = []
    quantile_frames = []
    for signal_date, group in frame.groupby("signal_date", sort=True):
        group = group.sort_values(["score", "code"]).copy()
        daily_rows.append({
            "signal_date": signal_date,
            "rank_ic": _safe_correlation(group["score"], group["excess_return"], "spearman"),
            "pearson_ic": _safe_correlation(group["score"], group["excess_return"], "pearson"),
            "scored_names": int(len(group)),
            "raw_names": int(group["raw_universe_size"].iloc[0]),
        })
        if len(group) >= config.quantiles:
            score_order = group["score"].rank(method="first")
            group["quantile"] = pd.qcut(score_order, config.quantiles, labels=False).astype(int) + 1
            quantile_frames.append(group)
    daily_ic = pd.DataFrame(daily_rows)
    if not quantile_frames:
        raise ValueError("没有足够的截面样本进行分层")
    quantile_samples = pd.concat(quantile_frames, ignore_index=True)
    quantile_returns = (
        quantile_samples.groupby(["signal_date", "quantile"], as_index=False)
        .agg(gross_return=("gross_return", "mean"), excess_return=("excess_return", "mean"))
    )

    signal_dates = pd.DatetimeIndex(sorted(frame["signal_date"].unique()))
    rebalance_dates = signal_dates[::config.rebalance_every]
    previous_weights: dict[str, float] | None = None
    portfolio_rows = []
    selected_frames = []
    top_quantile = quantile_samples["quantile"].max()
    for signal_date in rebalance_dates:
        group = quantile_samples[
            (quantile_samples["signal_date"] == signal_date)
            & (quantile_samples["quantile"] == top_quantile)
        ].sort_values("code")
        if group.empty:
            continue
        weights = dict(zip(group["code"], np.repeat(1.0 / len(group), len(group))))
        if previous_weights is None:
            turnover = 1.0
        else:
            codes = set(weights) | set(previous_weights)
            turnover = 0.5 * sum(abs(weights.get(code, 0.0) - previous_weights.get(code, 0.0)) for code in codes)
        gross_return = float(group["gross_return"].mean())
        cost = turnover * (config.buy_cost + config.sell_cost)
        portfolio_rows.append({
            "signal_date": signal_date,
            "entry_date": group["entry_date"].min(),
            "exit_date": group["exit_date"].max(),
            "gross_return": gross_return,
            "net_return": gross_return - cost,
            "turnover": float(turnover),
            "positions": int(len(group)),
            "benchmark_return": float(group["benchmark_return"].iloc[0]),
        })
        selected_frames.append(group.assign(turnover=turnover))
        previous_weights = weights
    portfolio = pd.DataFrame(portfolio_rows)
    if portfolio.empty:
        raise ValueError("非重叠组合为空")
    selected = pd.concat(selected_frames, ignore_index=True)
    metrics = _summarize_evaluation(frame, daily_ic, quantile_returns, portfolio, config)
    return metrics, {
        "daily_ic": daily_ic,
        "quantile_returns": quantile_returns,
        "portfolio": portfolio,
        "selected": selected,
    }


def _file_hash(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def train_and_evaluate(
    samples: pd.DataFrame,
    output_dir: Path,
    walk_config: WalkForwardConfig = WalkForwardConfig(),
    evaluation_config: EvaluationConfig = EvaluationConfig(),
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    model_dir = output_dir / "models"
    model_dir.mkdir(exist_ok=True)
    splits = make_walk_forward_splits(samples["signal_date"].unique(), walk_config)
    prediction_frames = []
    daily_frames = []
    quantile_frames = []
    portfolio_frames = []
    selected_frames = []
    fold_rows = []
    fold_metrics = []
    importance_frames = []

    for fold_number, split in enumerate(splits, start=1):
        frames = purged_fold_samples(samples, split)
        model = fit_model(frames["train"], frames["validation"])
        validation = frames["validation"].copy()
        test = frames["test"].copy()
        validation["score"] = model.predict(validation[FEATURE_COLUMNS])
        test["score"] = model.predict(test[FEATURE_COLUMNS])
        test["fold"] = fold_number
        validation_metrics, _ = evaluate_scores(validation, evaluation_config)
        test_metrics, artifacts = evaluate_scores(test, evaluation_config)
        prediction_frames.append(test)
        for name in ("daily_ic", "quantile_returns", "portfolio", "selected"):
            artifact = artifacts[name].copy()
            artifact["fold"] = fold_number
            {"daily_ic": daily_frames, "quantile_returns": quantile_frames,
             "portfolio": portfolio_frames, "selected": selected_frames}[name].append(artifact)
        model.booster_.save_model(str(model_dir / f"fold_{fold_number:02d}.txt"))
        importance_frames.append(pd.DataFrame({
            "feature": FEATURE_COLUMNS,
            "gain": model.booster_.feature_importance(importance_type="gain"),
            "fold": fold_number,
        }))
        fold_rows.append({
            "fold": fold_number,
            "train_signal_start": frames["train"]["signal_date"].min(),
            "train_signal_end": frames["train"]["signal_date"].max(),
            "validation_signal_start": validation["signal_date"].min(),
            "validation_signal_end": validation["signal_date"].max(),
            "test_signal_start": test["signal_date"].min(),
            "test_signal_end": test["signal_date"].max(),
            "train_rows_after_purge": len(frames["train"]),
            "validation_rows_after_purge": len(validation),
            "test_rows": len(test),
            "best_iteration": int(model.best_iteration_ or model.n_estimators),
            "validation_rank_ic": validation_metrics["rank_ic"]["mean"],
            "test_rank_ic": test_metrics["rank_ic"]["mean"],
            "test_net_return": test_metrics["top_quantile_nonoverlapping"]["total_net_return"],
        })
        fold_metrics.append({"fold": fold_number, "validation": validation_metrics, "test": test_metrics})

    predictions = pd.concat(prediction_frames, ignore_index=True)
    daily_ic = pd.concat(daily_frames, ignore_index=True)
    quantile_returns = pd.concat(quantile_frames, ignore_index=True)
    portfolio = pd.concat(portfolio_frames, ignore_index=True)
    selected = pd.concat(selected_frames, ignore_index=True)
    aggregate = _summarize_evaluation(predictions, daily_ic, quantile_returns, portfolio, evaluation_config)
    folds = pd.DataFrame(fold_rows)
    if folds["test_signal_start"].duplicated().any():
        raise AssertionError("滚动测试信号日发生重叠")
    importance = (
        pd.concat(importance_frames, ignore_index=True).groupby("feature", as_index=False)["gain"].mean()
        .sort_values("gain", ascending=False)
    )

    predictions.to_csv(output_dir / "predictions.csv", index=False)
    daily_ic.to_csv(output_dir / "daily_ic.csv", index=False)
    quantile_returns.to_csv(output_dir / "quantile_returns.csv", index=False)
    portfolio.to_csv(output_dir / "portfolio_returns.csv", index=False)
    selected.to_csv(output_dir / "selected.csv", index=False)
    folds.to_csv(output_dir / "folds.csv", index=False)
    importance.to_csv(output_dir / "feature_importance.csv", index=False)
    return {
        "status": "completed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "deployment_eligible": False,
        "model": {
            "algorithm": "LightGBM regression with validation-only early stopping",
            "features": FEATURE_COLUMNS,
            "label": "T close -> T+1 open entry -> T+6 open exit; benchmark-relative return ranked within signal date",
            "holding_days": HOLDING_DAYS,
            "stock_code_used_as_feature": False,
            "hyperparameters_fixed_before_test": True,
            "random_state": 42,
        },
        "walk_forward": asdict(walk_config),
        "evaluation": asdict(evaluation_config),
        "folds": fold_metrics,
        "out_of_sample": aggregate,
        "acceptance": {
            "research_pipeline_completed": True,
            "live_or_paper_trading_approved": False,
            "reason": "Baseline evidence is not a profitability guarantee; point-in-time full-market validation remains required.",
        },
    }


def run(
    stock_path: Path,
    benchmark_path: Path,
    universe_path: Path,
    output_dir: Path,
    universe_mode: str = "non_pit_current_constituents",
    allow_non_pit_sample: bool = False,
    walk_config: WalkForwardConfig = WalkForwardConfig(),
    evaluation_config: EvaluationConfig = EvaluationConfig(),
) -> dict:
    if universe_mode != "pit_full_market" and not allow_non_pit_sample:
        raise ValueError("非 PIT 样本只能用于冒烟研究，必须显式传入 --allow-non-pit-sample")
    stocks = pd.read_csv(stock_path, dtype={"code": str})
    benchmark = pd.read_csv(benchmark_path)
    universe = pd.read_csv(
        universe_path, dtype={"code": str, "STK_CODE": str, "STK_UNI_CODE": str}
    )
    pit_report = None
    if universe_mode == "pit_full_market":
        stocks, pit_report = apply_pit_universe(stocks, universe)
    samples = build_samples(stocks, benchmark)
    summary = train_and_evaluate(samples, output_dir, walk_config, evaluation_config)
    summary["data"] = {
        "stock_path": str(stock_path.resolve()),
        "benchmark_path": str(benchmark_path.resolve()),
        "universe_path": str(universe_path.resolve()),
        "input_sha256": _file_hash((stock_path, benchmark_path, universe_path)),
        "universe_mode": universe_mode,
        "point_in_time_full_market": universe_mode == "pit_full_market",
        "pit_validation": pit_report,
        "codes": int(stocks["code"].nunique()),
        "price_start": str(pd.to_datetime(stocks["date"]).min().date()),
        "price_end": str(pd.to_datetime(stocks["date"]).max().date()),
        "sample_rows": int(len(samples)),
        "signal_days": int(samples["signal_date"].nunique()),
    }
    summary["limitations"] = [
        "Daily bars only; no intraday liquidity, limit-up/down execution, partial fills or minimum commission simulation.",
        "Top-minus-bottom quantile spread is diagnostic, not an executable A-share long-short return.",
        "Non-overlapping top-quantile portfolio uses approximate turnover costs and no market-impact model.",
        "Research artifact only; no automatic replacement of the existing model and no trading permission.",
    ]
    if universe_mode != "pit_full_market":
        summary["limitations"].insert(0, "Current-constituent sample is not point-in-time and has survivorship bias; this is a pipeline smoke baseline only.")
    (output_dir / "summary.json").write_text(
        json.dumps(_json_safe(summary), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-data", type=Path, default=Path("research/artifacts/kline-model/stock_daily.csv"))
    parser.add_argument("--benchmark-data", type=Path, default=Path("research/artifacts/kline-model/benchmark_daily.csv"))
    parser.add_argument("--universe-data", type=Path, default=Path("research/artifacts/kline-model/universe.csv"))
    parser.add_argument("--output", type=Path, default=Path("research/artifacts/cross-sectional-model"))
    parser.add_argument("--universe-mode", choices=["non_pit_current_constituents", "pit_full_market"], default="non_pit_current_constituents")
    parser.add_argument("--allow-non-pit-sample", action="store_true", help="仅允许非 PIT 冒烟研究，不代表全市场验证")
    parser.add_argument("--train-days", type=int, default=DEFAULT_TRAIN_DAYS)
    parser.add_argument("--validation-days", type=int, default=DEFAULT_VALIDATION_DAYS)
    parser.add_argument("--test-days", type=int, default=DEFAULT_TEST_DAYS)
    parser.add_argument("--step-days", type=int, default=DEFAULT_TEST_DAYS)
    parser.add_argument("--embargo-days", type=int, default=DEFAULT_EMBARGO_DAYS)
    args = parser.parse_args()
    walk_config = WalkForwardConfig(
        train_days=args.train_days,
        validation_days=args.validation_days,
        test_days=args.test_days,
        step_days=args.step_days,
        embargo_days=args.embargo_days,
    )
    summary = run(
        args.stock_data,
        args.benchmark_data,
        args.universe_data,
        args.output,
        universe_mode=args.universe_mode,
        allow_non_pit_sample=args.allow_non_pit_sample,
        walk_config=walk_config,
    )
    print(json.dumps(_json_safe({
        "status": summary["status"],
        "data": summary["data"],
        "out_of_sample": summary["out_of_sample"],
        "deployment_eligible": summary["deployment_eligible"],
    }), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
