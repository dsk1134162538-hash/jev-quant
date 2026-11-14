"""回测员的底账：按「前期涨了多少」分档，看每一档的票之后怎么走（dj 2026-11-12「加一个股票回测员，专门看当前前期涨跌幅」）。

起因：11-12 灵活交易员和经理账户都买了冰山冷热，20 日涨 79%，买在山顶。它们只看当天盘口，看不到前期涨幅。

口径（和交易员的实际买法对齐）：
- 第 t 天收盘后算位置：rise20 = 收盘比 20 个交易日前涨了多少（%），rise5 同理。
- 第 t+1 天开盘买（交易员在 09:26 / 10:00 下单，开盘价是最接近的近似）；开盘就涨停的买不到，剔除。
- 持有到 t+1 收盘（1 日）、t+5 收盘（5 日）。另记 5 天里最低点相对买入价跌了多少（最大浮亏）。
- 只要沪深主板（60 / 00 开头）。
- 另按 MA5 乖离（第 t 天收盘比最近 5 个收盘均值高多少）分一张表（dj 2026-11-14）：20 日涨幅没到 60% 硬线、但短期冲太快的票
  （11-12 买的 002310：20 日 +35%、乖离 +16%）只有这张表能看出来。

数据：research/artifacts/first_board/kline_2025.sqlite（2024-11～2026-05）+ kline.sqlite（2026-04～2026-10），按日期去重。
输出：dashboard/data/position_base_rates.json（提交进仓库，live 直接读）。只是历史统计，不是买卖信号；样本外没单独验证。

运行：.venv/Scripts/python -m research.position_base_rates
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "research" / "artifacts" / "first_board"
SOURCES = [ART / "kline_2025.sqlite", ART / "kline.sqlite"]
OUT = ROOT / "dashboard" / "data" / "position_base_rates.json"

# 20 日涨幅分档（%）：左闭右开
BUCKETS = [(-1e9, -20), (-20, 0), (0, 20), (20, 40), (40, 60), (60, 100), (100, 1e9)]
# MA5 乖离分档（%，dj 2026-11-14 借 daily_stock_analysis 的「乖离率 > 5% 不追高」）：昨收比最近 5 个收盘均值高多少
BIAS_BUCKETS = [(-1e9, -5), (-5, 0), (0, 2), (2, 5), (5, 8), (8, 12), (12, 1e9)]
LIMIT_OPEN = 9.5            # 开盘涨幅 ≥ 这个值当作开盘涨停、买不到


def bucket_label(lo: float, hi: float) -> str:
    if lo <= -1e8:
        return f"<{hi:g}%"
    if hi >= 1e8:
        return f"≥{lo:g}%"
    return f"{lo:g}~{hi:g}%"


def load(sources=SOURCES) -> dict[str, dict[str, np.ndarray]]:
    rows: dict[str, dict[str, tuple]] = {}
    for src in sources:
        if not Path(src).exists():
            continue
        conn = sqlite3.connect(f"{Path(src).as_uri()}?mode=ro", uri=True)
        try:
            for code, d, o, c, low in conn.execute("SELECT code, date, open, close, low FROM k"):
                if code.startswith(("60", "00")) and o and c and low:
                    rows.setdefault(code, {})[d] = (o, c, low)
        finally:
            conn.close()
    out = {}
    for code, by in rows.items():
        ds = sorted(by)
        out[code] = {"date": np.array(ds), "open": np.array([by[d][0] for d in ds]), "close": np.array([by[d][1] for d in ds]),
                     "low": np.array([by[d][2] for d in ds])}
    return out


def samples(bars: dict) -> dict[str, np.ndarray]:
    r20, r5, bias, ret1, ret5, dd5, day = [], [], [], [], [], [], []
    for b in bars.values():
        o, c, low = b["open"], b["close"], b["low"]
        n = len(c)
        for t in range(20, n - 5):
            entry = o[t + 1]
            if entry / c[t] - 1 >= LIMIT_OPEN / 100:          # 开盘涨停买不到
                continue
            if np.any(np.abs(c[t - 19:t + 6] / c[t - 20:t + 5] - 1) > 0.22):   # 除权、停牌复牌的跳空：价格不连续，丢掉
                continue
            r20.append((c[t] / c[t - 20] - 1) * 100)
            r5.append((c[t] / c[t - 5] - 1) * 100)
            bias.append((c[t] / c[t - 4:t + 1].mean() - 1) * 100)
            ret1.append((c[t + 1] / entry - 1) * 100)
            ret5.append((c[t + 5] / entry - 1) * 100)
            dd5.append((low[t + 1:t + 6].min() / entry - 1) * 100)
            day.append(b["date"][t])
    return {k: np.array(v) for k, v in {"rise20": r20, "rise5": r5, "biasMa5": bias, "ret1": ret1, "ret5": ret5, "dd5": dd5,
                                        "date": day}.items()}


def table(s: dict, key: str = "rise20", buckets=BUCKETS) -> list[dict]:
    out = []
    for lo, hi in buckets:
        m = (s[key] >= lo) & (s[key] < hi)
        n = int(m.sum())
        if not n:
            continue
        out.append({"lo": lo if lo > -1e8 else None, "hi": hi if hi < 1e8 else None, "label": bucket_label(lo, hi), "n": n,
                    "avg1": round(float(s["ret1"][m].mean()), 2), "avg5": round(float(s["ret5"][m].mean()), 2),
                    "med5": round(float(np.median(s["ret5"][m])), 2), "win5": round(float((s["ret5"][m] > 0).mean() * 100), 1),
                    "deep5": round(float((s["dd5"][m] <= -10).mean() * 100), 1)})
    return out


def main() -> None:
    bars = load()
    s = samples(bars)
    days = sorted(set(s["date"].tolist()))
    half = days[len(days) // 2]
    early, late = (s["date"] < half), (s["date"] >= half)
    result = {"built": date.today().isoformat(), "from": days[0], "to": days[-1], "stocks": len(bars), "samples": int(len(s["rise20"])),
              "rule": "第 t 天收盘算 20 日涨幅；t+1 开盘买（开盘涨停剔除），持有到 t+1 / t+5 收盘；只算沪深主板",
              "all": table(s),
              "firstHalf": table({k: v[early] for k, v in s.items()}), "secondHalf": table({k: v[late] for k, v in s.items()}),
              "biasRule": "同上口径，按第 t 天收盘比最近 5 个收盘均值高多少（MA5 乖离）分档",
              "bias": table(s, "biasMa5", BIAS_BUCKETS),
              "biasFirstHalf": table({k: v[early] for k, v in s.items()}, "biasMa5", BIAS_BUCKETS),
              "biasSecondHalf": table({k: v[late] for k, v in s.items()}, "biasMa5", BIAS_BUCKETS)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{result['from']} ~ {result['to']}，{result['stocks']} 只，{result['samples']} 个样本 → {OUT}")
    for name in ("all", "firstHalf", "secondHalf", "bias", "biasFirstHalf", "biasSecondHalf"):
        print(f"\n{name}")
        print(f"{'乖离MA5' if name.startswith('bias') else '20日涨幅':>10} {'样本':>8} {'次日':>7} {'5日均':>7} {'5日中位':>8} {'5日胜率':>8} {'5日内跌超10%':>12}")
        for r in result[name]:
            print(f"{r['label']:>10} {r['n']:>8} {r['avg1']:>+7.2f} {r['avg5']:>+7.2f} {r['med5']:>+8.2f} {r['win5']:>7.1f}% {r['deep5']:>11.1f}%")


if __name__ == "__main__":
    main()
