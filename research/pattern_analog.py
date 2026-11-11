"""K 线形态找历史相似走势（dj 2026-11-11：「看 K 线技术形态，回测这只票之前类似的走势，给出预测，供交易员和经理参考」）。

假设（先写下来，回测只用来验证）：一只票今天走强（当日涨幅 ≥2%）时，它最近 20 个交易日的价格路径和量能形状，
和历史上最像的那 50 次相比，那 50 次之后 1 天 / 3 天的平均涨跌，对它自己之后的涨跌有预测力。

否证条件（跑之前定死，不看结果调参）：历史库只用 2024-11-29 之前的数据，考题全部来自 2025 年以后。
把考题按「相似走势的次日平均涨跌」每天排序分五组——
  1. 每日秩相关（Rank IC）的均值 < 0.02，或
  2. 最高组减最低组的次日平均收益 ≤ 0.30%（来回手续费的量级），
任何一条成立就判不成立，不接给交易员。

数据：research/artifacts/first_board/kline_long.sqlite（2019-01～2024-11）、kline_2025.sqlite（2024-11～2026-05）、kline.sqlite（2026-04～2026-10）。
不复权：除权除息日会出现假的大跌，|单日涨跌| > 11% 的窗口整个丢掉。代码表是现在还在的主板票，有幸存者偏差。

    python -m research.pattern_analog              # 建库 + 样本外检验，结果打印并写 research/artifacts/pattern_analog/report.json
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

ART = Path(__file__).resolve().parent / "artifacts"
SOURCES = [ART / "first_board" / "kline_long.sqlite", ART / "first_board" / "kline_2025.sqlite", ART / "first_board" / "kline.sqlite"]
OUT = ART / "pattern_analog"
WINDOW = 20                 # 看最近 20 个交易日的形状
K = 50                      # 找最像的 50 次
EVENT_MIN_PCT = 2.0         # 只在「今天走强」的日子上建库和出题：交易员的候选就是这类票
LIMIT_PCT = 9.5             # 当天涨到这里基本就是涨停，收盘买不进
MAX_DAY_PCT = 11.0          # 主板涨跌停 10%：超过的是除权或脏数据
LIBRARY_END = "2024-11-29"
TEST_START = "2025-01-01"
IC_FLOOR, SPREAD_FLOOR = 0.02, 0.30


def features(close: np.ndarray, volume: np.ndarray) -> np.ndarray | None:
    """最近 WINDOW+1 个收盘、WINDOW 个成交量 → 46 维形状向量；窗口里有脏数据返回 None。
    价格路径（20，累计涨幅去均值、按自身波动缩放——只留形状）+ 量能形状（20，对数量比）+ 最近 5 天涨幅（5）+ 20 日总涨幅（1）。"""
    if len(close) != WINDOW + 1 or len(volume) != WINDOW or np.any(close <= 0) or np.any(volume <= 0):
        return None
    r = (close[1:] / close[:-1] - 1) * 100
    if np.any(np.abs(r) > MAX_DAY_PCT):
        return None
    path = np.cumsum(r)
    path = (path - path.mean()) / (path.std() + 1e-6)
    vol = np.clip(np.log(volume / volume.mean()), -3, 3)
    return np.concatenate([path, vol * 0.5, r[-5:] / 3.0, [r.sum() / 20.0]]).astype(np.float32)


def load_bars(sources=SOURCES) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """三个库拼起来、按日期去重：code → (dates, close, volume)。"""
    rows: dict[str, dict[str, tuple[float, float]]] = {}
    for src in sources:
        if not Path(src).exists():
            continue
        conn = sqlite3.connect(f"{Path(src).as_uri()}?mode=ro", uri=True)
        try:
            for code, date, close, volume in conn.execute("SELECT code, date, close, volume FROM k"):
                if close and volume:
                    rows.setdefault(code, {})[date] = (close, volume)
        finally:
            conn.close()
    out = {}
    for code, by_date in rows.items():
        dates = sorted(by_date)
        if len(dates) > WINDOW + 5:
            out[code] = (np.array(dates), np.array([by_date[d][0] for d in dates], dtype=np.float64),
                         np.array([by_date[d][1] for d in dates], dtype=np.float64))
    return out


def build_samples(bars: dict) -> dict[str, np.ndarray]:
    """每个「今天涨幅 ≥ EVENT_MIN_PCT」的日子一条样本：形状向量、之后 1 天 / 3 天的涨跌（收盘到收盘，%）。"""
    X, dates, codes, f1, f3, pcts = [], [], [], [], [], []
    for code, (ds, close, volume) in bars.items():
        day = (close[1:] / close[:-1] - 1) * 100
        for t in np.flatnonzero((day >= EVENT_MIN_PCT) & (day <= MAX_DAY_PCT)) + 1:      # day[t-1] 是第 t 天的涨幅
            if t < WINDOW or t >= len(close) - 3:
                continue
            ahead = (close[t + 1:t + 4] / close[t:t + 3] - 1) * 100
            if np.any(np.abs(ahead) > MAX_DAY_PCT):
                continue
            v = features(close[t - WINDOW:t + 1], volume[t - WINDOW + 1:t + 1])
            if v is None:
                continue
            X.append(v)
            dates.append(int(str(ds[t]).replace('-', '')))        # 日期存成整数 20250102：numpy 的字符串数组不能取最小最大
            codes.append(code)
            pcts.append(day[t - 1])
            f1.append((close[t + 1] / close[t] - 1) * 100)
            f3.append((close[t + 3] / close[t] - 1) * 100)
    return {"X": np.array(X, dtype=np.float32), "date": np.array(dates, dtype=np.int64), "code": np.array(codes),
            "f1": np.array(f1, dtype=np.float32), "f3": np.array(f3, dtype=np.float32), "pct": np.array(pcts, dtype=np.float32)}


def nearest(lib: np.ndarray, queries: np.ndarray, k: int = K, chunk: int = 128) -> np.ndarray:
    """每个 query 在 lib 里欧氏距离最近的 k 个下标。分块矩阵乘，纯 numpy。"""
    lib_sq = (lib ** 2).sum(axis=1)
    out = np.empty((len(queries), min(k, len(lib))), dtype=np.int64)
    for i in range(0, len(queries), chunk):
        q = queries[i:i + chunk]
        d = lib_sq[None, :] - 2.0 * (q @ lib.T)                     # 差一个只跟 query 有关的常数，不影响排序
        kk = out.shape[1]
        idx = np.argpartition(d, kk - 1, axis=1)[:, :kk] if kk < d.shape[1] else np.argsort(d, axis=1)
        out[i:i + chunk] = idx
    return out


def summarize(f1: np.ndarray, f3: np.ndarray) -> dict:
    """一组相似走势之后的统计——这就是给交易员看的那一行。"""
    return {"n": int(len(f1)), "up1": round(float((f1 > 0).mean() * 100), 1), "mean1": round(float(f1.mean()), 2),
            "mean3": round(float(f3.mean()), 2), "worst1": round(float(f1.min()), 2), "best1": round(float(f1.max()), 2)}


def _rank(a: np.ndarray) -> np.ndarray:
    order = a.argsort()
    r = np.empty(len(a))
    r[order] = np.arange(len(a))
    return r


def validate(samples: dict, max_queries: int = 30000, seed: int = 7, buyable_only: bool = False) -> dict:
    """样本外检验：库 = LIBRARY_END 之前；考题 = TEST_START 之后（太多就随机抽 max_queries 条，种子固定）。"""
    lib_mask = samples["date"] <= int(LIBRARY_END.replace("-", ""))
    test_ok = samples["date"] >= int(TEST_START.replace("-", ""))
    if buyable_only:                      # 更严的一道：当天涨停（≥9.5%）的票收盘买不进，考题里去掉；历史库也只留买得进的
        test_ok &= samples["pct"] < LIMIT_PCT
        lib_mask &= samples["pct"] < LIMIT_PCT
    test_idx = np.flatnonzero(test_ok)
    if len(test_idx) > max_queries:
        test_idx = np.sort(np.random.default_rng(seed).choice(test_idx, max_queries, replace=False))
    lib_X, lib_f1, lib_f3 = samples["X"][lib_mask], samples["f1"][lib_mask], samples["f3"][lib_mask]
    nn = nearest(lib_X, samples["X"][test_idx])
    pred1, pred3 = lib_f1[nn].mean(axis=1), lib_f3[nn].mean(axis=1)
    real1, real3, dates = samples["f1"][test_idx], samples["f3"][test_idx], samples["date"][test_idx]
    ics, spreads, tops, bottoms = [], [], [], []
    for d in np.unique(dates):
        m = dates == d
        if m.sum() < 25:
            continue
        p, y = pred1[m], real1[m]
        ics.append(float(np.corrcoef(_rank(p), _rank(y))[0, 1]))
        cut = np.quantile(p, [0.2, 0.8])
        top, bottom = y[p >= cut[1]].mean(), y[p <= cut[0]].mean()
        tops.append(float(top))
        bottoms.append(float(bottom))
        spreads.append(float(top - bottom))
    ic, spread = float(np.mean(ics)), float(np.mean(spreads))
    q = np.quantile(pred1, [0.2, 0.4, 0.6, 0.8])
    groups = np.digitize(pred1, q)
    return {"library": int(lib_mask.sum()), "queries": int(len(test_idx)), "days": len(ics),
            "rankIC": round(ic, 4), "icPositiveDays%": round(float((np.array(ics) > 0).mean() * 100), 1),
            "topMinusBottom1d%": round(spread, 3), "top1d%": round(float(np.mean(tops)), 3), "bottom1d%": round(float(np.mean(bottoms)), 3),
            "base": {"mean1": round(float(real1.mean()), 3), "up1": round(float((real1 > 0).mean() * 100), 1), "mean3": round(float(real3.mean()), 3)},
            "quintiles": [{"group": g + 1, "n": int((groups == g).sum()), "pred1": round(float(pred1[groups == g].mean()), 3),
                           "real1": round(float(real1[groups == g].mean()), 3), "up1": round(float((real1[groups == g] > 0).mean() * 100), 1),
                           "real3": round(float(real3[groups == g].mean()), 3)} for g in range(5)],
            "passed": bool(ic >= IC_FLOOR and spread > SPREAD_FLOOR),
            "rule": f"Rank IC ≥ {IC_FLOOR} 且 最高组−最低组 > {SPREAD_FLOOR}%"}


def main() -> int:
    bars = load_bars()
    print(f"股票 {len(bars)} 只")
    samples = build_samples(bars)
    print(f"样本 {len(samples['f1'])} 条（当日涨幅 ≥ {EVENT_MIN_PCT}% 的日子），{samples['date'].min()} ～ {samples['date'].max()}")
    report = {"all": validate(samples), "buyable": validate(samples, buyable_only=True)}
    report["passed"] = bool(report["buyable"]["passed"])        # 以买得进的那一道为准
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
