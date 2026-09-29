import json
import sqlite3
from datetime import date
from pathlib import Path

from jevq import accounts as accounts_mod
from jevq.config import REPORT_DIR, STRATEGIES
from jevq.data import custom_store, sell_followups
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


def notice_risk_stats(conn, day: date) -> dict:
    """当天所有决策留痕里的疑似公告（按公告 id 去重）统计。旧格式（list）trace 跳过。"""
    seen: dict[str, dict] = {}
    for (raw,) in conn.execute("SELECT trace FROM decisions WHERE substr(ts, 1, 10)=? AND trace IS NOT NULL "
                               "ORDER BY id", (day.isoformat(),)):     # 同一公告取当天最后一次判定
        try:
            notices = (json.loads(raw) or {}).get("notices")
        except (ValueError, AttributeError):
            continue
        if not isinstance(notices, dict):
            continue
        for s in notices.get("suspects") or []:
            if isinstance(s, dict) and s.get("id"):
                seen[s["id"]] = s
    by_cat: dict[str, int] = {}
    for s in seen.values():
        by_cat[s.get("category") or "?"] = by_cat.get(s.get("category") or "?", 0) + 1
    return {"judged": len(seen),
            "reject": sum(1 for s in seen.values() if s.get("verdict") == "reject"),
            "unsure": sum(1 for s in seen.values() if s.get("verdict") == "unsure"),
            "unread": sum(1 for s in seen.values() if not s.get("body_read")),
            "by_category": by_cat}


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
    lines += [f"| {ts[11:16]} | {strategy_labels.get(sid, sid)} | {code} "
              f"| {names.get(code, '')} | {VERDICT_LABELS.get(verdict, verdict)} | {detail} |"
              for ts, sid, code, verdict, detail in rows]

    st = notice_risk_stats(conn, day)
    lines += ["", "## 公告风险统计", "",
              f"细判 {st['judged']} 条｜否决：高风险 {st['reject']}、拿不准 {st['unsure']}｜未读到正文 {st['unread']}", ""]
    if st["by_category"]:
        lines += ["| 类别 | 条数 |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in sorted(st["by_category"].items(), key=lambda kv: -kv[1])]

    bucket_rows = conn.execute(
        "SELECT bucket, COUNT(*) FROM board_events WHERE date=? GROUP BY bucket",
        (day.isoformat(),)).fetchall()
    lines += ["", "## 今日首板时段分布", "", "| 时段 | 首板数 |", "|---|---|"]
    lines += [f"| {bucket} | {count} |" for bucket, count in bucket_rows] or ["| 无 | 0 |"]

    followups = sell_followups.report_rows(conn, day)
    if followups:
        def pct(v, base):
            return f"{(v / base - 1) * 100:+.2f}%" if v is not None and base else "—"
        lines += ["", "## 卖飞回看", "",
                  "相对卖出价：卖后当日最高 / 当日收盘 / 次日最高 / 次日收盘（只记录，不参与交易）", "",
                  "| 卖出日 | 账户 | 代码 | 名称 | 原因 | 卖出价 | 卖后最高 | 当日收盘 | 次日最高 | 次日收盘 |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        lines += [f"| {d} | {account_names.get(a, a)} | {code} | {names.get(code, '')} | {reason} | {price:.2f} "
                  f"| {pct(high, price)} | {pct(close, price)} | {pct(nhigh, price)} | {pct(nclose, price)} |"
                  for d, a, code, reason, price, high, close, nhigh, nclose in followups]

    lines += ["", "> 成交价取分钟快照，开盘附近会比实盘略好。"]

    path = Path(out_dir) / f"{day.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
