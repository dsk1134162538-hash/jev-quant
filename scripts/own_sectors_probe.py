"""自建板块的数据探测（家里电脑用户本人手动运行；只读：只联网取数、打印，不写库、不下单、不碰 data/）。
设计 docs/specs/2026-10-04-own-sectors-design.md「风险与限制」：主营构成接口在云端沙箱访问不了，上线前要在家里实测。
用法：
  .venv\\Scripts\\python scripts\\own_sectors_probe.py [--codes 603256,300162,...] [--n 3] [--full]
对每只票取：
  ① 东方财富主营构成（akshare.stock_zygc_em）→ 最近一期「按产品分类」各产品行的收入与收入比例，比例之和（应≈100%）；
  ② 同花顺主营介绍（akshare.stock_zyjs_ths）→ 主营业务/产品名称原文（构成取不到时 Jev 读它做判断）。
最后给出两个接口的成功率与平均耗时，并说明 收入比例 是「小数」还是「百分数」（后面的占比线要按它算）。
把整段输出贴给 Claude 即可。--full 会多打印每只票最近一期的全部行（含按行业/地区）。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import akshare as ak  # noqa: E402

from jevq.data.timeout import call_with_timeout  # noqa: E402

# 2026-09-30 晚报里出现过的票 + 对照（茅台=单一产品，平安银行=金融）
DEFAULT_CODES = ["603256", "300162", "688331", "600519", "000001"]
TIMEOUT = 30.0


def em_symbol(code: str) -> str:
    return ("SH" if code.startswith(("6", "9")) else "BJ" if code.startswith(("4", "8")) else "SZ") + code


def probe_zygc(code: str):
    df = call_with_timeout(ak.stock_zygc_em, symbol=em_symbol(code), timeout=TIMEOUT)
    return df


def probe_zyjs(code: str):
    return call_with_timeout(ak.stock_zyjs_ths, symbol=code, timeout=TIMEOUT)


def show_products(code: str, df, full: bool):
    if df is None or df.empty:
        print("  构成：空表")
        return None
    latest = df["报告日期"].max()
    cur = df[df["报告日期"] == latest]
    prod = cur[cur["分类类型"] == "按产品分类"]
    top = prod[~prod["主营构成"].astype(str).str.startswith("其中")]      # 「其中:xx」是上一行的细分，不能重复相加
    print(f"  构成：共 {len(df)} 行，最近报告期 {latest}，其中按产品 {len(prod)} 行")
    for _, r in prod.iterrows():
        print(f"    {str(r['主营构成'])[:24]:<24} 收入 {r['主营收入']:>16,.0f}  收入比例 {r['收入比例']}")
    total = top["收入比例"].sum()
    if len(prod):
        print(f"  按产品的收入比例之和（不含「其中:」细分行）= {total:.4f}")
    if full:
        print(cur.to_string())
    return total if len(prod) else None


def show_intro(df):
    if df is None or df.empty:
        print("  介绍：空表")
        return
    row = df.iloc[0]
    for k in df.columns:
        if k == "股票代码":
            continue
        print(f"  介绍·{k}：{str(row[k])[:90]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codes", default=",".join(DEFAULT_CODES))
    ap.add_argument("--n", type=int, default=1, help="每只票每个接口重复取几次（看稳定性）")
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args()
    codes = [c.strip().zfill(6) for c in a.codes.split(",") if c.strip()]
    stats = {"zygc": [0, 0, 0.0], "zyjs": [0, 0, 0.0]}      # 成功, 失败, 总耗时
    totals = []
    for code in codes:
        print(f"== {code} ==")
        for name, fn in (("zygc", probe_zygc), ("zyjs", probe_zyjs)):
            shown = False
            for _ in range(a.n):
                t0 = time.time()
                try:
                    df = fn(code)
                    ok = True
                except Exception as e:
                    ok, df = False, None
                    print(f"  {name} 失败：{e!r}"[:300])
                dt = time.time() - t0
                stats[name][0 if ok else 1] += 1
                stats[name][2] += dt
                if ok and not shown:
                    shown = True
                    print(f"  [{name}] {dt:.1f}s")
                    if name == "zygc":
                        t = show_products(code, df, a.full)
                        if t is not None:
                            totals.append(t)
                    else:
                        show_intro(df)
    print("\n== 汇总 ==")
    for name, (ok, bad, secs) in stats.items():
        n = ok + bad
        print(f"{name}: 成功 {ok}/{n}，平均 {secs / n:.1f}s" if n else f"{name}: 未运行")
    if totals:
        scale = "小数（0~1）" if max(totals) <= 1.5 else "百分数（0~100）"
        print(f"收入比例口径：按产品之和在 {min(totals):.2f}~{max(totals):.2f} → 看起来是{scale}")
    print("把以上整段贴给 Claude。")


if __name__ == "__main__":
    main()
