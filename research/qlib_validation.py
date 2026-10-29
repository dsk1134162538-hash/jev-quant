"""Qlib 风险分析接入验证：真实 A 股日线、时间顺序回测与样本外报告。

本模块只研究和生成文件，不导入实时盯盘、账户、券商或交易执行代码。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

DEFAULT_SYMBOLS = ("002553", "603001", "605566", "001216", "600499")
DEFAULT_LOOKBACKS = (5, 10, 20, 40, 60)
TRADING_DAYS = 252
BUY_COST = 0.0005
SELL_COST = 0.0015


def _symbol_with_exchange(symbol: str) -> str:
    symbol = str(symbol).strip()
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError(f"股票代码必须是 6 位数字：{symbol!r}")
    return ("sh" if symbol.startswith(("5", "6", "9")) else "sz") + symbol


def _validate_bars(frame: pd.DataFrame, required: Iterable[str]) -> pd.DataFrame:
    if frame.empty:
        raise ValueError("行情数据为空，不能生成回测指标")
    result = frame.copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise").dt.normalize()
    if result["date"].duplicated().any():
        raise ValueError("同一证券存在重复交易日")
    for column in required:
        if column not in result:
            raise ValueError(f"行情缺少字段：{column}")
        result[column] = pd.to_numeric(result[column], errors="raise")
    if result[list(required)].isna().any().any():
        raise ValueError("行情关键字段存在空值")
    if (result[["open", "close"]] <= 0).any().any():
        raise ValueError("开盘价和收盘价必须为正数")
    return result.sort_values("date").reset_index(drop=True)


def download_history(
    symbols: Iterable[str],
    start: str,
    end: str,
    retries: int = 3,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """通过 AKShare 的新浪接口下载前复权股票和未复权指数日线。"""
    import akshare as ak

    stock_frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for symbol in symbols:
        normalized = _symbol_with_exchange(symbol)
        for attempt in range(retries):
            try:
                frame = ak.stock_zh_a_daily(
                    symbol=normalized,
                    start_date=start.replace("-", ""),
                    end_date=end.replace("-", ""),
                    adjust="qfq",
                )
                frame = _validate_bars(frame, ("open", "high", "low", "close", "volume"))
                frame.insert(1, "code", symbol)
                stock_frames.append(frame)
                break
            except Exception as exc:  # 网络错误需要带上下文上报，不能伪造数据。
                errors.append(f"{symbol} 第 {attempt + 1} 次：{type(exc).__name__}: {exc}")
                if attempt + 1 < retries:
                    time.sleep(attempt + 1)
        else:
            raise RuntimeError("真实行情下载失败；未生成回测结果。\n" + "\n".join(errors[-retries:]))

    benchmark: pd.DataFrame | None = None
    for attempt in range(retries):
        try:
            benchmark = ak.stock_zh_index_daily(symbol="sh000300")
            benchmark = _validate_bars(benchmark, ("open", "high", "low", "close", "volume"))
            benchmark = benchmark[
                benchmark["date"].between(pd.Timestamp(start), pd.Timestamp(end))
            ].reset_index(drop=True)
            if benchmark.empty:
                raise ValueError("指定区间内沪深 300 数据为空")
            break
        except Exception as exc:
            errors.append(f"000300 第 {attempt + 1} 次：{type(exc).__name__}: {exc}")
            if attempt + 1 < retries:
                time.sleep(attempt + 1)
    if benchmark is None:
        raise RuntimeError("沪深 300 下载失败；未生成回测结果。\n" + "\n".join(errors[-retries:]))
    return pd.concat(stock_frames, ignore_index=True), benchmark


def momentum_positions(bars: pd.DataFrame, lookback: int) -> pd.Series:
    """T 日收盘动量信号只在 T+1 日开盘转为持仓。"""
    if lookback < 1:
        raise ValueError("回望期必须为正整数")
    bars = _validate_bars(bars, ("open", "close"))
    close = bars.set_index("date")["close"]
    close_signal = close.gt(close.shift(lookback)).astype(float)
    close_signal[close.shift(lookback).isna()] = 0.0
    return close_signal.shift(1).fillna(0.0).rename("position")


def simulate_symbol(
    bars: pd.DataFrame,
    lookback: int,
    buy_cost: float = BUY_COST,
    sell_cost: float = SELL_COST,
) -> pd.DataFrame:
    """计算开盘到下一开盘收益；信号延迟一日，换手当日扣费。"""
    bars = _validate_bars(bars, ("open", "close"))
    position = momentum_positions(bars, lookback)
    opens = bars.set_index("date")["open"]
    result = pd.DataFrame(index=opens.index)
    result["position"] = position
    result["asset_return"] = opens.shift(-1) / opens - 1.0
    previous = position.shift(1).fillna(0.0)
    buys = (position - previous).clip(lower=0.0)
    sells = (previous - position).clip(lower=0.0)
    result["cost"] = buys * buy_cost + sells * sell_cost
    result["gross_return"] = position * result["asset_return"]
    result["net_return"] = result["gross_return"] - result["cost"]
    return result.dropna(subset=["asset_return"])


def simulate_portfolio(
    stocks: pd.DataFrame,
    lookback: int,
    buy_cost: float = BUY_COST,
    sell_cost: float = SELL_COST,
) -> pd.DataFrame:
    """在有有效收益观察的样本股之间等权，不填充停牌或缺失行情。"""
    if stocks.empty or stocks["code"].nunique() < 1:
        raise ValueError("至少需要一个股票样本")
    pieces = []
    for code, bars in stocks.groupby("code", sort=True):
        result = simulate_symbol(bars, lookback, buy_cost, sell_cost)
        result["code"] = code
        pieces.append(result.reset_index())
    long = pd.concat(pieces, ignore_index=True)
    daily = long.groupby("date", sort=True).agg(
        strategy_gross=("gross_return", "mean"),
        strategy_net=("net_return", "mean"),
        turnover_cost=("cost", "mean"),
        active_positions=("position", "sum"),
        observed_symbols=("asset_return", "count"),
    )
    if daily.empty:
        raise ValueError("没有足够的开盘到下一开盘收益观察")
    return daily


def buy_and_hold(stocks: pd.DataFrame, buy_cost: float = BUY_COST) -> pd.Series:
    """等权买入持有，仅在每个样本首次建仓时扣买入成本。"""
    pieces = []
    for code, bars in stocks.groupby("code", sort=True):
        bars = _validate_bars(bars, ("open", "close"))
        opens = bars.set_index("date")["open"]
        net = opens.shift(-1) / opens - 1.0
        if not net.empty:
            net.iloc[0] -= buy_cost
        pieces.append(net.rename(code))
    wide = pd.concat(pieces, axis=1).dropna(how="all")
    result = wide.mean(axis=1).rename("buy_hold_net")
    if result.empty:
        raise ValueError("买入持有收益为空")
    return result


def benchmark_opens(benchmark: pd.DataFrame) -> pd.Series:
    benchmark = _validate_bars(benchmark, ("open", "close"))
    opens = benchmark.set_index("date")["open"]
    result = (opens.shift(-1) / opens - 1.0).dropna().rename("benchmark_return")
    if result.empty:
        raise ValueError("基准收益为空")
    return result


def qlib_risk_metrics(returns: pd.Series) -> dict[str, float | int | None]:
    """只把 Qlib 用于风险分析；非有限值在 JSON 中输出为 null。"""
    clean = pd.Series(returns, dtype=float).replace([math.inf, -math.inf], math.nan).dropna()
    if clean.empty:
        raise ValueError("收益序列为空，不生成风险指标")
    from qlib.contrib.evaluate import risk_analysis

    risk = risk_analysis(clean, N=TRADING_DAYS, mode="product")["risk"]
    metrics: dict[str, float | int | None] = {
        "observations": int(clean.size),
        "total_return": float((1.0 + clean).prod() - 1.0),
    }
    for name in ("mean", "std", "annualized_return", "information_ratio", "max_drawdown"):
        value = float(risk.get(name, math.nan))
        metrics[name] = value if math.isfinite(value) else None
    return metrics


def _drawdown(returns: pd.Series) -> pd.Series:
    equity = (1.0 + returns).cumprod()
    return equity / equity.cummax().clip(lower=1.0) - 1.0


def run_backtest(
    stocks: pd.DataFrame,
    benchmark: pd.DataFrame,
    split: str,
    lookbacks: Iterable[int] = DEFAULT_LOOKBACKS,
) -> tuple[dict, pd.DataFrame]:
    split_date = pd.Timestamp(split)
    candidates = []
    simulations = {}
    for lookback in lookbacks:
        daily = simulate_portfolio(stocks, lookback)
        train = daily.loc[daily.index < split_date, "strategy_net"]
        if train.size < 2:
            raise ValueError("训练样本不足两个交易日")
        metrics = qlib_risk_metrics(train)
        candidates.append({"lookback": int(lookback), "train_metrics": metrics})
        simulations[int(lookback)] = daily
    selected = max(
        candidates,
        key=lambda item: (
            item["train_metrics"]["annualized_return"]
            if item["train_metrics"]["annualized_return"] is not None
            else -math.inf,
            -item["lookback"],
        ),
    )
    lookback = selected["lookback"]
    daily = simulations[lookback]
    daily = daily.join(buy_and_hold(stocks), how="inner")
    daily = daily.join(benchmark_opens(benchmark), how="inner")
    train = daily.loc[daily.index < split_date]
    validation = daily.loc[daily.index >= split_date].copy()
    if validation.shape[0] < 2:
        raise ValueError("样本外区间不足两个完整收益观察")

    for column in ("strategy_net", "buy_hold_net", "benchmark_return"):
        validation[column.replace("_net", "").replace("_return", "") + "_equity"] = (
            1.0 + validation[column]
        ).cumprod()
        validation[column.replace("_net", "").replace("_return", "") + "_drawdown"] = _drawdown(
            validation[column]
        )
    validation.insert(0, "date", validation.index)
    validation = validation.reset_index(drop=True)

    summary = {
        "status": "completed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": {
            "signal": "close-to-close time-series momentum; long when close > close shifted by lookback",
            "execution": "T close signal becomes T+1 open position; open-to-next-open returns",
            "portfolio": "equal weight among symbols with valid return observations; long or cash",
            "buy_cost": BUY_COST,
            "sell_cost": SELL_COST,
            "lookback_selection": "highest training annualized return; validation never used for selection",
            "final_position_liquidated": False,
        },
        "split": {
            "train_return_dates": [str(train.index.min().date()), str(train.index.max().date())],
            "validation_return_dates": [
                str(validation["date"].min().date()),
                str(validation["date"].max().date()),
            ],
            "split_date": str(split_date.date()),
            "train_observations": int(len(train)),
            "validation_observations": int(len(validation)),
        },
        "selected_lookback": lookback,
        "candidate_training": candidates,
        "validation_metrics": {
            "strategy": qlib_risk_metrics(validation["strategy_net"]),
            "equal_weight_buy_hold": qlib_risk_metrics(validation["buy_hold_net"]),
            "csi300": qlib_risk_metrics(validation["benchmark_return"]),
        },
        "train_metrics_selected": qlib_risk_metrics(train["strategy_net"]),
        "limitations": [
            "Engineering validation sample, not evidence of strategy efficacy or future returns.",
            "Small convenience sample selected from prior simulated fills; selection and survivorship bias remain.",
            "No limit-up/down execution, T+1 rule, board lots, minimum commission, slippage or partial fills.",
            "Qlib is used only for risk_analysis; this is not a production-grade Qlib exchange simulation.",
            "No import or call into live monitoring, accounts, brokers or executors.",
        ],
    }
    return summary, validation


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def render_report(summary: dict, data_metadata: dict) -> str:
    metrics = summary["validation_metrics"]
    rows = []
    for key, label in (
        ("strategy", "动量研究基线（净）"),
        ("equal_weight_buy_hold", "样本等权买入持有（净）"),
        ("csi300", "沪深 300（开盘到下一开盘）"),
    ):
        item = metrics[key]
        rows.append(
            f"| {label} | {_pct(item['total_return'])} | {_pct(item['annualized_return'])} "
            f"| {_pct(item['max_drawdown'])} | {_num(item['information_ratio'])} | {item['observations']} |"
        )
    candidate_rows = [
        f"| {item['lookback']} | {_pct(item['train_metrics']['total_return'])} "
        f"| {_pct(item['train_metrics']['annualized_return'])} | {_pct(item['train_metrics']['max_drawdown'])} |"
        for item in summary["candidate_training"]
    ]
    split = summary["split"]
    return f"""# Qlib 独立研究接入验证

> 状态：{summary['status']}。这是工程验证样本，不构成策略有效性证明、收益承诺或交易建议。

## 数据与切分

- 行情：AKShare 新浪接口；个股前复权日线，沪深 300 指数日线。
- 样本代码：{', '.join(data_metadata['symbols'])}；基准：000300（沪深 300）。
- 下载价格区间：{data_metadata['price_start']} 至 {data_metadata['price_end']}。
- 训练收益区间：{split['train_return_dates'][0]} 至 {split['train_return_dates'][1]}，{split['train_observations']} 个观察。
- 样本外收益区间：{split['validation_return_dates'][0]} 至 {split['validation_return_dates'][1]}，{split['validation_observations']} 个观察。
- 参数只用训练集选择，选中回望期：**{summary['selected_lookback']} 个交易日**。
- Qlib：{data_metadata['qlib_version']}；调用 `risk_analysis(..., N=252, mode="product")`。

## 样本外结果

| 序列 | 区间收益 | 年化收益 | 最大回撤 | 信息比率 | 观察数 |
|---|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

## 训练集参数选择记录

| 回望期（交易日） | 训练区间收益 | 训练年化收益 | 训练最大回撤 |
|---:|---:|---:|---:|
{chr(10).join(candidate_rows)}

## 计算口径

1. T 日收盘价高于其 N 日前收盘价时产生多头信号，否则空仓；信号在 T+1 日开盘才生效。
2. 收益按 T 日开盘到下一交易日开盘计算，避免使用当日收盘信号赚取同日收益。
3. 买入换手成本 0.05%，卖出换手成本 0.15%；样本间按有效收益观察等权，期末不强制平仓。
4. 对照为相同样本的等权买入持有，以及沪深 300 的同口径开盘收益；基准不扣交易成本。
5. Qlib 负责风险指标，独立研究代码负责信号、时间切分、收益和成本计算。

## 明确限制

- 这是从既有模拟成交样本中选取的小样本，存在选择偏差与幸存者偏差，不能外推到全市场。
- 未模拟涨跌停无法成交、T+1、100 股整手、最低佣金、滑点、部分成交和停牌撮合。
- Qlib 的 A 股生产级交易所仿真并未因此得到验证；实时盯盘、模拟账户与真实执行链路未被调用。
- 结果仅说明接入、时间顺序、成本扣减和报告流程可运行，不说明策略可盈利。

## 产物

- `summary.json`：切分、参数选择、风险指标和限制。
- `daily_returns.csv`：样本外逐日收益、权益和回撤。
- `stock_daily.csv`、`benchmark_daily.csv`：本次真实研究输入快照。
- `equity_drawdown.png`：样本外权益与回撤图。
"""


def save_plot(daily: pd.DataFrame, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dates = pd.to_datetime(daily["date"])
    series = (
        ("strategy", "Momentum net", "#2563eb"),
        ("buy_hold", "Equal-weight buy & hold", "#059669"),
        ("benchmark", "CSI 300", "#64748b"),
    )
    figure, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    for prefix, label, color in series:
        axes[0].plot(dates, daily[f"{prefix}_equity"], label=label, color=color, linewidth=1.8)
        axes[1].plot(dates, daily[f"{prefix}_drawdown"] * 100, label=label, color=color, linewidth=1.2)
    axes[0].set_title("Out-of-sample equity (start = 1.0)")
    axes[0].set_ylabel("Equity")
    axes[1].set_title("Drawdown")
    axes[1].set_ylabel("Drawdown (%)")
    axes[1].set_xlabel("Return date")
    for axis in axes:
        axis.grid(alpha=0.2)
        axis.legend(loc="best")
    figure.tight_layout()
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)


def run_validation(
    output_dir: Path,
    symbols: Iterable[str] = DEFAULT_SYMBOLS,
    start: str = "2024-01-01",
    end: str = "2026-10-27",
    split: str = "2026-01-01",
    reuse_data: bool = False,
) -> dict:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    symbols = tuple(str(symbol) for symbol in symbols)
    stock_path = output_dir / "stock_daily.csv"
    benchmark_path = output_dir / "benchmark_daily.csv"
    if reuse_data:
        if not stock_path.exists() or not benchmark_path.exists():
            raise FileNotFoundError("--reuse-data 需要先存在 stock_daily.csv 和 benchmark_daily.csv")
        stocks = pd.read_csv(stock_path, dtype={"code": str})
        benchmark = pd.read_csv(benchmark_path)
    else:
        stocks, benchmark = download_history(symbols, start, end)
        stocks.to_csv(stock_path, index=False)
        benchmark.to_csv(benchmark_path, index=False)
    summary, daily = run_backtest(stocks, benchmark, split)
    import qlib

    data_metadata = {
        "source": "AKShare Sina daily endpoints",
        "stock_adjustment": "qfq",
        "benchmark_adjustment": "none",
        "symbols": list(symbols),
        "benchmark": "000300",
        "requested_start": start,
        "requested_end": end,
        "price_start": str(pd.to_datetime(stocks["date"]).min().date()),
        "price_end": str(pd.to_datetime(stocks["date"]).max().date()),
        "stock_rows": int(len(stocks)),
        "benchmark_rows": int(len(benchmark)),
        "qlib_version": qlib.__version__,
    }
    summary["data"] = data_metadata
    daily.to_csv(output_dir / "daily_returns.csv", index=False)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(render_report(summary, data_metadata), encoding="utf-8")
    save_plot(daily, output_dir / "equity_drawdown.png")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("research/artifacts/qlib-validation"))
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2026-10-27")
    parser.add_argument("--split", default="2026-01-01")
    parser.add_argument("--reuse-data", action="store_true", help="只复用已保存的真实行情快照")
    args = parser.parse_args()
    summary = run_validation(
        output_dir=args.output,
        symbols=args.symbols,
        start=args.start,
        end=args.end,
        split=args.split,
        reuse_data=args.reuse_data,
    )
    print(json.dumps({
        "status": summary["status"],
        "selected_lookback": summary["selected_lookback"],
        "validation_metrics": summary["validation_metrics"],
    }, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
