"""K 线与成交量形态研究模型：LightGBM、时间顺序验证、独立隔夜回测。

只读取历史行情并写入研究产物，不连接实时盯盘、账户或交易执行链路。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from research.qlib_validation import BUY_COST, SELL_COST, download_history, qlib_risk_metrics

TRAIN_END = "2025-01-01"
VALIDATION_END = "2026-01-01"
ROUND_TRIP_COST = BUY_COST + SELL_COST
TOP_K = 3
THRESHOLD_QUANTILES = (0.80, 0.90, 0.95)

FEATURE_COLUMNS = [
    "ret_1", "ret_3", "ret_5", "ret_10", "ret_20", "gap", "body", "body_range",
    "upper_shadow", "lower_shadow", "close_location", "amplitude", "doji", "hammer",
    "bullish_engulfing", "ma_gap_5", "ma_gap_10", "ma_gap_20", "ma_gap_60",
    "ma_slope_5", "ma_slope_10", "ma_slope_20", "breakout_20", "breakout_60",
    "range_position_60", "atr_14", "volatility_20", "volume_change", "volume_ratio_5",
    "volume_ratio_20", "volume_trend_5", "shrink_3", "shrink_pullback", "volume_breakout",
    "small_body_pullback", "benchmark_ret_1", "benchmark_ret_5", "benchmark_ret_20",
    "benchmark_ma_gap_20", "benchmark_volatility_20", "benchmark_volume_ratio_5",
    "relative_ret_5", "relative_ret_20",
]


def select_universe(size: int = 30) -> pd.DataFrame:
    """从当前沪深 300 主板成分中按代码等距抽样；记录并承认幸存者偏差。"""
    import akshare as ak

    cons = ak.index_stock_cons(symbol="000300").copy()
    cons["code"] = cons["品种代码"].astype(str).str.zfill(6)
    cons["name"] = cons["品种名称"].astype(str)
    cons["included"] = cons["纳入日期"].astype(str)
    cons = cons[cons["code"].str.startswith(("00", "60"))].sort_values("code").reset_index(drop=True)
    if len(cons) < size:
        raise ValueError("可用主板成分股不足，拒绝缩小到不可复现的样本")
    indices = np.linspace(0, len(cons) - 1, num=size, dtype=int)
    return cons.iloc[indices][["code", "name", "included"]].reset_index(drop=True)


def _benchmark_features(benchmark: pd.DataFrame) -> pd.DataFrame:
    bars = benchmark.sort_values("date").copy()
    bars["date"] = pd.to_datetime(bars["date"])
    close, volume = bars["close"], bars["volume"]
    out = bars[["date"]].copy()
    out["benchmark_ret_1"] = close.pct_change()
    out["benchmark_ret_5"] = close.pct_change(5)
    out["benchmark_ret_20"] = close.pct_change(20)
    out["benchmark_ma_gap_20"] = close / close.rolling(20).mean() - 1.0
    out["benchmark_volatility_20"] = out["benchmark_ret_1"].rolling(20).std()
    out["benchmark_volume_ratio_5"] = volume / volume.shift(1).rolling(5).mean()
    return out


def _stock_features(bars: pd.DataFrame) -> pd.DataFrame:
    bars = bars.sort_values("date").reset_index(drop=True).copy()
    bars["date"] = pd.to_datetime(bars["date"])
    open_, high, low, close, volume = (bars[key] for key in ("open", "high", "low", "close", "volume"))
    previous_close = close.shift(1)
    price_range = (high - low).replace(0, np.nan)
    out = bars[["date", "code"]].copy()
    for window in (1, 3, 5, 10, 20):
        out[f"ret_{window}"] = close.pct_change(window)
    out["gap"] = open_ / previous_close - 1.0
    out["body"] = (close - open_) / previous_close
    out["body_range"] = (close - open_).abs() / price_range
    out["upper_shadow"] = (high - np.maximum(open_, close)) / previous_close
    out["lower_shadow"] = (np.minimum(open_, close) - low) / previous_close
    out["close_location"] = (close - low) / price_range
    out["amplitude"] = price_range / previous_close
    out["doji"] = (out["body_range"] < 0.10).astype(float)
    out["hammer"] = (
        (out["lower_shadow"] > out["upper_shadow"] * 2)
        & (out["close_location"] > 0.60)
        & (out["body_range"] < 0.40)
    ).astype(float)
    out["bullish_engulfing"] = (
        (close > open_) & (close.shift(1) < open_.shift(1))
        & (open_ <= close.shift(1)) & (close >= open_.shift(1))
    ).astype(float)
    for window in (5, 10, 20, 60):
        average = close.rolling(window).mean()
        out[f"ma_gap_{window}"] = close / average - 1.0
    for window in (5, 10, 20):
        average = close.rolling(window).mean()
        out[f"ma_slope_{window}"] = average / average.shift(5) - 1.0
    out["breakout_20"] = close / high.shift(1).rolling(20).max() - 1.0
    out["breakout_60"] = close / high.shift(1).rolling(60).max() - 1.0
    low_60, high_60 = low.rolling(60).min(), high.rolling(60).max()
    out["range_position_60"] = (close - low_60) / (high_60 - low_60).replace(0, np.nan)
    true_range = pd.concat([
        high - low,
        (high - previous_close).abs(),
        (low - previous_close).abs(),
    ], axis=1).max(axis=1)
    out["atr_14"] = true_range.rolling(14).mean() / close
    out["volatility_20"] = out["ret_1"].rolling(20).std()
    out["volume_change"] = volume / volume.shift(1) - 1.0
    out["volume_ratio_5"] = volume / volume.shift(1).rolling(5).mean()
    out["volume_ratio_20"] = volume / volume.shift(1).rolling(20).mean()
    out["volume_trend_5"] = volume.rolling(5).mean() / volume.shift(5).rolling(5).mean() - 1.0
    out["shrink_3"] = (
        (volume < volume.shift(1)) & (volume.shift(1) < volume.shift(2))
        & (volume.shift(2) < volume.shift(3))
    ).astype(float)
    out["shrink_pullback"] = (
        (out["shrink_3"] > 0) & (out["ret_3"] < 0) & (out["ma_gap_20"] > 0)
    ).astype(float)
    out["volume_breakout"] = ((out["breakout_20"] > 0) & (out["volume_ratio_5"] > 1.5)).astype(float)
    out["small_body_pullback"] = (
        (out["body"].abs() < 0.01) & (out["ret_3"] < 0)
        & (out["ma_gap_20"] > 0) & (out["volume_ratio_5"] < 0.8)
    ).astype(float)
    # T 日收盘特征只预测 T+1 开盘到 T+2 开盘的收益，标签跨越一日执行边界。
    out["entry_date"] = bars["date"].shift(-1)
    out["exit_date"] = bars["date"].shift(-2)
    out["entry_open"] = open_.shift(-1)
    out["exit_open"] = open_.shift(-2)
    out["target_gross_return"] = out["exit_open"] / out["entry_open"] - 1.0
    out["target_net_return"] = out["target_gross_return"] - ROUND_TRIP_COST
    return out


def build_samples(stocks: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    if stocks.empty or benchmark.empty:
        raise ValueError("训练行情为空")
    samples = pd.concat((_stock_features(bars) for _, bars in stocks.groupby("code", sort=True)), ignore_index=True)
    samples = samples.merge(_benchmark_features(benchmark), on="date", how="left", validate="many_to_one")
    samples["relative_ret_5"] = samples["ret_5"] - samples["benchmark_ret_5"]
    samples["relative_ret_20"] = samples["ret_20"] - samples["benchmark_ret_20"]
    samples = samples.replace([np.inf, -np.inf], np.nan)
    required = ["entry_date", "exit_date", "target_net_return", *FEATURE_COLUMNS]
    samples = samples.dropna(subset=required).reset_index(drop=True)
    if samples.empty:
        raise ValueError("特征与标签完整样本为空")
    return samples


def _period(samples: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    # 按实际建仓日切分，防止训练标签跨入验证区间。
    dates = pd.to_datetime(samples["entry_date"])
    mask = pd.Series(True, index=samples.index)
    if start is not None:
        mask &= dates >= pd.Timestamp(start)
    if end is not None:
        mask &= dates < pd.Timestamp(end)
    return samples.loc[mask].copy()


def fit_model(train: pd.DataFrame):
    from lightgbm import LGBMRegressor

    if train["entry_date"].nunique() < 60 or len(train) < 500:
        raise ValueError("训练样本不足，拒绝拟合看似完整的模型")
    model = LGBMRegressor(
        n_estimators=250, learning_rate=0.03, num_leaves=15, max_depth=4,
        min_child_samples=50, subsample=0.85, colsample_bytree=0.85,
        reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=4, verbosity=-1,
    )
    model.fit(train[FEATURE_COLUMNS], train["target_net_return"])
    return model


def _quality(frame: pd.DataFrame, score: np.ndarray) -> dict:
    from sklearn.metrics import mean_absolute_error, roc_auc_score

    actual = frame["target_net_return"].to_numpy()
    scores = pd.Series(score, index=frame.index)
    result = {
        "samples": int(len(frame)),
        "mae": float(mean_absolute_error(actual, score)),
        "spearman_ic": float(pd.Series(actual).corr(pd.Series(score), method="spearman")),
    }
    target = actual > 0
    result["roc_auc"] = float(roc_auc_score(target, score)) if len(np.unique(target)) == 2 else None
    deciles = pd.qcut(scores, 10, labels=False, duplicates="drop")
    decile_returns = pd.Series(actual, index=frame.index).groupby(deciles).mean()
    result["decile_average_net_returns"] = [float(value) for value in decile_returns]
    result["top_decile_average_net_return"] = float(decile_returns.iloc[-1])
    result["decile_trend_spearman"] = float(pd.Series(decile_returns.index).corr(
        pd.Series(decile_returns.to_numpy()), method="spearman"))
    return result


def _selected_rows(
    frame: pd.DataFrame,
    quantile: float,
    score_cutoff: float | None = None,
) -> pd.DataFrame:
    scored = frame.copy()
    # 最终验证／测试只能沿用验证集分数阈值，不能用本区间未来分数分布重算门槛。
    cutoff = float(scored["score"].quantile(quantile)) if score_cutoff is None else float(score_cutoff)
    scored = scored[scored["score"] >= cutoff]
    scored = scored.sort_values(["signal_date", "score", "code"], ascending=[True, False, True])
    return scored.groupby("signal_date", group_keys=False).head(TOP_K).copy()


def _risk_with_extras(returns: pd.Series, selected: pd.DataFrame) -> dict:
    metrics = qlib_risk_metrics(returns)
    standard_error = returns.std(ddof=1) / math.sqrt(len(returns)) if len(returns) > 1 else math.nan
    metrics.update({
        "trades": int(len(selected)),
        "active_days": int(selected["entry_date"].nunique()) if not selected.empty else 0,
        "win_rate": float((selected["target_net_return"] > 0).mean()) if not selected.empty else None,
        "average_trade_net_return": float(selected["target_net_return"].mean()) if not selected.empty else None,
        "average_positions": float(selected.groupby("entry_date").size().mean()) if not selected.empty else 0.0,
        "daily_mean_t_stat": float(returns.mean() / standard_error) if standard_error > 0 else None,
    })
    return metrics


def evaluate_period(
    frame: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    benchmark_returns: pd.Series,
    quantile: float,
    score_cutoff: float | None = None,
) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    selected = _selected_rows(frame, quantile, score_cutoff)
    selected["selected"] = True
    daily = pd.DataFrame(index=calendar)
    daily.index.name = "entry_date"
    daily["model_net"] = selected.groupby("entry_date")["target_net_return"].mean().reindex(calendar).fillna(0.0)
    daily["universe_net"] = frame.groupby("entry_date")["target_net_return"].mean().reindex(calendar).fillna(0.0)
    daily["benchmark"] = benchmark_returns.reindex(calendar).fillna(0.0)
    daily["positions"] = selected.groupby("entry_date").size().reindex(calendar).fillna(0).astype(int)
    daily["model_equity"] = (1.0 + daily["model_net"]).cumprod()
    daily["model_drawdown"] = daily["model_equity"] / daily["model_equity"].cummax().clip(lower=1.0) - 1.0
    metrics = {
        "quantile": quantile,
        "model": _risk_with_extras(daily["model_net"], selected),
        "universe_equal_weight": qlib_risk_metrics(daily["universe_net"]),
        "csi300": qlib_risk_metrics(daily["benchmark"]),
    }
    return metrics, daily.reset_index(), selected


def _benchmark_returns(benchmark: pd.DataFrame) -> pd.Series:
    bars = benchmark.sort_values("date").copy()
    bars["date"] = pd.to_datetime(bars["date"])
    opens = bars.set_index("date")["open"]
    return (opens.shift(-1) / opens - 1.0).dropna()


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return value


def _file_hashes(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def train_and_evaluate(
    stocks: pd.DataFrame,
    benchmark: pd.DataFrame,
    output_dir: Path,
) -> dict:
    samples = build_samples(stocks, benchmark)
    samples["signal_date"] = samples["date"]
    train = _period(samples, None, TRAIN_END)
    validation = _period(samples, TRAIN_END, VALIDATION_END)
    test = _period(samples, VALIDATION_END, None)
    if min(len(train), len(validation), len(test)) < 100:
        raise ValueError("训练、验证或测试样本不足")
    model = fit_model(train)
    for name, frame in (("train", train), ("validation", validation), ("test", test)):
        frame["score"] = model.predict(frame[FEATURE_COLUMNS])
        frame["split"] = name
    benchmark_returns = _benchmark_returns(benchmark)
    calendars = {
        name: pd.DatetimeIndex(sorted(frame["entry_date"].unique()))
        for name, frame in (("train", train), ("validation", validation), ("test", test))
    }
    candidates = []
    evaluations = {}
    for quantile in THRESHOLD_QUANTILES:
        metrics, _, _ = evaluate_period(validation, calendars["validation"], benchmark_returns, quantile)
        candidates.append(metrics)
        evaluations[quantile] = metrics
    eligible = [item for item in candidates if item["model"]["trades"] >= 30]
    if not eligible:
        raise ValueError("验证集交易样本不足，拒绝选择交易阈值")
    chosen = max(
        eligible,
        key=lambda item: (
            item["model"]["information_ratio"] if item["model"]["information_ratio"] is not None else -math.inf,
            item["quantile"],
        ),
    )
    quantile = chosen["quantile"]
    score_cutoff = float(validation["score"].quantile(quantile))
    period_results = {"validation_candidates": candidates}
    daily_frames, selected_frames = [], []
    for name, frame in (("train", train), ("validation", validation), ("test", test)):
        metrics, daily, selected = evaluate_period(
            frame, calendars[name], benchmark_returns, quantile, score_cutoff)
        daily["split"] = name
        selected["split"] = name
        daily_frames.append(daily)
        selected_frames.append(selected)
        period_results[name] = metrics
    predictions = pd.concat([train, validation, test], ignore_index=True)
    predictions = predictions[["signal_date", "entry_date", "exit_date", "code", "split", "score",
                               "target_gross_return", "target_net_return"]]
    selected_keys = pd.concat(selected_frames, ignore_index=True)[["signal_date", "code"]].assign(selected=True)
    predictions = predictions.merge(selected_keys, on=["signal_date", "code"], how="left")
    predictions["selected"] = predictions["selected"].fillna(False).astype(bool)
    predictions.to_csv(output_dir / "predictions.csv", index=False)
    pd.concat(daily_frames, ignore_index=True).to_csv(output_dir / "daily_returns.csv", index=False)
    model.booster_.save_model(str(output_dir / "model.txt"))
    importance = sorted(
        ({"feature": feature, "gain": float(gain)} for feature, gain in zip(
            FEATURE_COLUMNS, model.booster_.feature_importance(importance_type="gain"))),
        key=lambda item: item["gain"], reverse=True,
    )
    summary = {
        "status": "completed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "deployment_eligible": False,
        "model": {
            "algorithm": "LightGBM regression", "features": FEATURE_COLUMNS,
            "label": "T close features -> T+1 open entry -> T+2 open exit; net of 0.20% round-trip cost",
            "stock_code_used_as_feature": False, "random_state": 42,
        },
        "periods": {
            "train_entry_dates": [str(train["entry_date"].min().date()), str(train["entry_date"].max().date())],
            "validation_entry_dates": [str(validation["entry_date"].min().date()), str(validation["entry_date"].max().date())],
            "test_entry_dates": [str(test["entry_date"].min().date()), str(test["entry_date"].max().date())],
        },
        "quality": {
            "train": _quality(train, train["score"].to_numpy()),
            "validation": _quality(validation, validation["score"].to_numpy()),
            "test": _quality(test, test["score"].to_numpy()),
        },
        "selection": {
            "criterion": "highest validation information ratio; test data excluded; minimum 30 trades",
            "selected_quantile": quantile,
            "score_cutoff_from_validation": score_cutoff,
            "top_k_per_signal_day": TOP_K,
        },
        "results": period_results,
        "feature_importance": importance,
        "knowledge_mapping": {
            "source": "jevq/app/knowledge.json and docs/HANDOFF.md",
            "status": "unverified hypotheses converted into features, not adopted as proven rules",
            "examples": [
                "market and cycle context -> benchmark returns, volatility and relative strength",
                "small-body shrink pullback in an uptrend -> shrink_pullback and small_body_pullback",
                "volume-supported platform breakout -> breakout_20/60, volume ratios and volume_breakout",
                "upper/lower shadows and clean candles -> shadow, close-location, body and engulfing features",
            ],
        },
        "limitations": [
            "Current CSI 300 constituents create survivorship bias; this is not a point-in-time universe.",
            "Daily bars only; no sector, news, intraday auction, limit execution or liquidity simulation.",
            "Independent one-day trades at observed opens; no lot size, minimum commission, slippage or partial fills.",
            "Research artifact only; no evidence of future profitability and no connection to live trading.",
        ],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_json_safe(summary), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output_dir / "feature_columns.json").write_text(
        json.dumps(FEATURE_COLUMNS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _render_report(summary: dict, metadata: dict) -> str:
    test = summary["results"]["test"]
    rows = []
    for key, label in (("model", "K 线模型（净）"), ("universe_equal_weight", "样本等权隔夜（净）"), ("csi300", "沪深 300")):
        item = test[key]
        rows.append(f"| {label} | {item['total_return'] * 100:.2f}% | {item['annualized_return'] * 100:.2f}% "
                    f"| {item['max_drawdown'] * 100:.2f}% | {item['information_ratio']:.3f} |")
    quality = summary["quality"]["test"]
    model = test["model"]
    top_features = "\n".join(
        f"| {item['feature']} | {item['gain']:.3f} |" for item in summary["feature_importance"][:12])
    return f"""# K 线与成交量模型研究报告

> **当前不具备部署条件。** 这是研究模型，不是实盘策略；样本外整体排序能力接近随机，不能由少数高分交易的区间收益推断未来盈利。

## 数据与模型

- 真实前复权日线：{metadata['price_start']} 至 {metadata['price_end']}，{metadata['symbols']} 只当前沪深 300 主板成分股。
- 训练建仓日：{summary['periods']['train_entry_dates'][0]} 至 {summary['periods']['train_entry_dates'][1]}。
- 验证建仓日：{summary['periods']['validation_entry_dates'][0]} 至 {summary['periods']['validation_entry_dates'][1]}，只用于选择阈值。
- 最终测试建仓日：{summary['periods']['test_entry_dates'][0]} 至 {summary['periods']['test_entry_dates'][1]}，不参与训练或阈值选择。
- T 日收盘特征，T+1 开盘买入，T+2 开盘卖出；每日最多 {summary['selection']['top_k_per_signal_day']} 只，等权，双边成本合计 0.20%。
- 验证集选中预测分位数阈值：{summary['selection']['selected_quantile']:.2f}，固定分数门槛：{summary['selection']['score_cutoff_from_validation']:.8f}；所有后续区间沿用该门槛，不重新拟合各自分布。股票代码不作为特征。

## 最终样本外结果

| 序列 | 区间收益 | 年化收益 | 最大回撤 | 信息比率 |
|---|---:|---:|---:|---:|
{chr(10).join(rows)}

- 测试交易数：{model['trades']}；活跃交易日：{model['active_days']}；逐笔胜率：{model['win_rate'] * 100:.2f}%。
- 测试回归 MAE：{quality['mae']:.6f}；Spearman IC：{quality['spearman_ic']:.4f}；ROC AUC：{quality['roc_auc']:.4f}。
- 日收益均值 t 统计量：{model['daily_mean_t_stat']:.3f}；最高分数十分位逐笔平均净收益：{quality['top_decile_average_net_return'] * 100:.4f}%；十分位收益趋势 Spearman：{quality['decile_trend_spearman']:.4f}。

## 稳健性结论

- 训练 IC 为 {summary['quality']['train']['spearman_ic']:.4f}，测试 IC 降至 {quality['spearman_ic']:.4f}，存在明显过拟合迹象。
- IC 接近 0、AUC 接近 0.5，说明模型对全部样本几乎没有稳定排序能力；最高十分位收益为负且十分位收益不单调。
- 日收益均值 t 统计量远低于常用显著性门槛 2；少量极端 Top K 交易的正收益不足以抵消这些反证。
- `deployment_eligible=false`：结果只用于继续研究，不接入模拟或真实交易。

## 主要特征重要性（LightGBM gain）

| 特征 | 增益 |
|---|---:|
{top_features}

## 仓库经验如何进入模型

知识库中的“周期环境、缩量回踩、放量突破、上下影线、均线位置和相对强弱”只作为待检验假设，被转换为数值特征。没有直接继承“胜率九成”等未经本地验证的说法。

## 限制

- 当前成分股回看历史有幸存者偏差；缺少历史时点股票池、行业、消息、竞价和分钟数据。
- 假设能按实际开盘价成交，未模拟涨跌停、滑点、整手、最低佣金和部分成交。
- 本报告不证明策略有效或未来盈利，模型没有连接模拟账户或真实执行器。
"""


def run(
    output_dir: Path,
    universe_size: int = 30,
    start: str = "2022-01-01",
    end: str | None = None,
    refresh: bool = False,
    force: bool = False,
) -> dict:
    end = end or datetime.now().date().isoformat()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stock_path, benchmark_path, universe_path = (output_dir / name for name in
                                                  ("stock_daily.csv", "benchmark_daily.csv", "universe.csv"))
    inputs_exist = all(path.exists() for path in (stock_path, benchmark_path, universe_path))
    if refresh or not inputs_exist:
        universe = select_universe(universe_size)
        stocks, benchmark = download_history(universe["code"], start, end)
        universe.to_csv(universe_path, index=False)
        stocks.to_csv(stock_path, index=False)
        benchmark.to_csv(benchmark_path, index=False)
    else:
        universe = pd.read_csv(universe_path, dtype={"code": str})
        stocks = pd.read_csv(stock_path, dtype={"code": str})
        benchmark = pd.read_csv(benchmark_path)
    input_hash = _file_hashes((stock_path, benchmark_path, universe_path))
    state_path = output_dir / "training_state.json"
    if state_path.exists() and not force:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("input_sha256") == input_hash:
            return {"status": "unchanged", "input_sha256": input_hash, "output_dir": str(output_dir)}
    summary = train_and_evaluate(stocks, benchmark, output_dir)
    metadata = {
        "source": "AKShare Sina daily; current CSI 300 constituents",
        "symbols": int(universe.shape[0]),
        "price_start": str(pd.to_datetime(stocks["date"]).min().date()),
        "price_end": str(pd.to_datetime(stocks["date"]).max().date()),
        "input_sha256": input_hash,
    }
    summary["data"] = metadata
    (output_dir / "summary.json").write_text(
        json.dumps(_json_safe(summary), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output_dir / "report.md").write_text(_render_report(summary, metadata), encoding="utf-8")
    state = {
        "status": "completed", "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_sha256": input_hash, "model": "model.txt", "summary": "summary.json",
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("research/artifacts/kline-model"))
    parser.add_argument("--universe-size", type=int, default=30)
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default=None, help="行情截止日期，默认运行当天")
    parser.add_argument("--refresh", action="store_true", help="下载最新真实行情后再训练")
    parser.add_argument("--force", action="store_true", help="输入未变化也强制重训")
    args = parser.parse_args()
    result = run(args.output, args.universe_size, args.start, args.end, args.refresh, args.force)
    if result["status"] == "unchanged":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(_json_safe({
            "status": result["status"], "selected_quantile": result["selection"]["selected_quantile"],
            "test_quality": result["quality"]["test"], "test_results": result["results"]["test"],
        }), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
