import sqlite3
from datetime import date
from pathlib import Path

from jevq import accounts as accounts_mod
from jevq.config import REPORT_DIR, STRATEGIES
from jevq.data import custom_store
from jevq.ledger import Ledger



# 报告给人看：检查结果用中文，不露出程序里的代码
VERDICT_LABELS = {"pass": "通过", "reject": "否决", "error": "出错", "sentiment": "情绪", "skip": "跳过"}

def _strategy_groups(conn: sqlite3.Connection, day: date) -> tuple[list[dict], dict[str, str]]:
    """净值表按策略分组的账户列表（含策略 id，用来查净值基准），以及策略 id -> 名称
    （含自定义策略，供检查记录表用）。"""
    groups = [{"id": sid, "name": s["name"], "accounts": s["accounts"]} for sid, s in STRATEGIES.items()]
    labels = {sid: s["name"] for sid, s in STRATEGIES.items()}
    for s in custom_store.active_for(conn, day) + custom_store.holders(conn, day):
        labels[s["id"]] = s["name"]
        groups.append({"id": s["id"], "name": s["name"], "accounts": (f"{s['id']}_main", f"{s['id']}_shadow")})
    return groups, labels


def write_report(conn: sqlite3.Connection, ledger: Ledger, day: date, prices: dict[str, float],
                 overseas: str | None, sentiment: str | None, out_dir=REPORT_DIR,
                 names: dict[str, str] | None = None) -> Path:
    names = names or {}
    fills = ledger.fills_on(day)
    account_names = accounts_mod.account_names(conn, day)
    groups, strategy_labels = _strategy_groups(conn, day)
    lines = [f"# 复盘 {day.isoformat()}", "",
             f"外围：{overseas or '未知'}　情绪：{sentiment or '未知'}", "",
             "## 净值"]
    for group in groups:
        baseline = accounts_mod.capital_baseline(conn, group["id"])
        lines += ["", f"### {group['name']}", "",
                  "| 账户 | 净值 | 累计收益 | 今日费用 | 持仓 |", "|---|---|---|---|---|"]
        for a in group["accounts"]:
            value = ledger.equity(a, prices)
            conn.execute("INSERT OR REPLACE INTO equity VALUES (?, ?, ?)", (day.isoformat(), a, value))
            fee = sum(f.fee for f in fills if f.account == a)
            held = "、".join(f"{p.code} {names.get(p.code, '')}".strip() for p in ledger.positions(a)) or "空仓"
            lines.append(f"| {account_names.get(a, a)} | {value:,.2f} | {(value / baseline - 1) * 100:+.2f}% "
                         f"| {fee:.2f} | {held} |")
    conn.commit()

    lines += ["", "## 成交", "", "| 时间 | 账户 | 代码 | 名称 | 方向 | 价格 | 股数 | 费用 | 理由 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for f in fills:
        side = "买" if f.side == "buy" else "卖"
        lines.append(f"| {f.ts:%H:%M} | {account_names.get(f.account, f.account)} | {f.code} | {names.get(f.code, '')} | {side} | {f.price:.2f} "
                     f"| {f.shares} | {f.fee:.2f} | {f.reason} |")

    rows = conn.execute("SELECT ts, strategy, code, verdict, detail FROM decisions "
                        "WHERE substr(ts, 1, 10)=? AND code != '' ORDER BY id",
                        (day.isoformat(),)).fetchall()
    lines += ["", "## 主账户检查记录", "", "| 时间 | 策略 | 代码 | 名称 | 结果 | 原因 |", "|---|---|---|---|---|---|"]
    lines += [f"| {ts[11:16]} | {strategy_labels.get(sid or 'lowbuy', sid or 'lowbuy')} | {code} "
              f"| {names.get(code, '')} | {VERDICT_LABELS.get(verdict, verdict)} | {detail} |"
              for ts, sid, code, verdict, detail in rows]

    bucket_rows = conn.execute(
        "SELECT bucket, COUNT(*) FROM board_events WHERE date=? GROUP BY bucket",
        (day.isoformat(),)).fetchall()
    lines += ["", "## 今日首板时段分布", "", "| 时段 | 首板数 |", "|---|---|"]
    lines += [f"| {bucket} | {count} |" for bucket, count in bucket_rows] or ["| 无 | 0 |"]

    lines += ["", "> 成交价取分钟快照，开盘附近会比实盘略好。"]

    path = Path(out_dir) / f"{day.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
