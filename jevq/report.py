import sqlite3
from datetime import date
from pathlib import Path

from jevq.config import ACCOUNTS, INITIAL_CASH, REPORT_DIR
from jevq.ledger import Ledger

NAMES = {"main": "主账户(Jev)", "shadow": "影子账户"}


def write_report(conn: sqlite3.Connection, ledger: Ledger, day: date, prices: dict[str, float],
                 overseas: str | None, sentiment: str | None, out_dir=REPORT_DIR) -> Path:
    fills = ledger.fills_on(day)
    lines = [f"# 复盘 {day.isoformat()}", "",
             f"外围：{overseas or '未知'}　情绪：{sentiment or '未知'}", "",
             "## 净值", "", "| 账户 | 净值 | 累计收益 | 今日费用 | 持仓 |", "|---|---|---|---|---|"]
    for a in ACCOUNTS:
        value = ledger.equity(a, prices)
        conn.execute("INSERT OR REPLACE INTO equity VALUES (?, ?, ?)", (day.isoformat(), a, value))
        fee = sum(f.fee for f in fills if f.account == a)
        held = "、".join(p.code for p in ledger.positions(a)) or "空仓"
        lines.append(f"| {NAMES[a]} | {value:,.2f} | {(value / INITIAL_CASH - 1) * 100:+.2f}% "
                     f"| {fee:.2f} | {held} |")
    conn.commit()

    lines += ["", "## 成交", "", "| 时间 | 账户 | 代码 | 方向 | 价格 | 股数 | 费用 | 理由 |",
              "|---|---|---|---|---|---|---|---|"]
    for f in fills:
        side = "买" if f.side == "buy" else "卖"
        lines.append(f"| {f.ts:%H:%M} | {NAMES[f.account]} | {f.code} | {side} | {f.price:.2f} "
                     f"| {f.shares} | {f.fee:.2f} | {f.reason} |")

    rows = conn.execute("SELECT ts, code, verdict, detail FROM decisions "
                        "WHERE substr(ts, 1, 10)=? AND code != '' ORDER BY id",
                        (day.isoformat(),)).fetchall()
    lines += ["", "## 主账户检查记录", "", "| 时间 | 代码 | 结果 | 原因 |", "|---|---|---|---|"]
    lines += [f"| {ts[11:16]} | {code} | {verdict} | {detail} |" for ts, code, verdict, detail in rows]
    lines += ["", "> 成交价取分钟快照，开盘附近会比实盘略好。"]

    path = Path(out_dir) / f"{day.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
