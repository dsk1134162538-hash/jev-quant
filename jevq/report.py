import json
import sqlite3
from datetime import date
from pathlib import Path

from jevq import accounts as accounts_mod
from jevq.config import LIVE_ACCOUNT, REPORT_DIR, STRATEGIES
from jevq.data import custom_store, hi189, sell_followups
from jevq.ledger import Ledger
from jevq.live import store as live_store



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


# 实盘段只统计当前模式的委托：演练的 dryrun 行按委托价、委托数量估算成交；实盘不含演练留下的行
_DONE = {"dryrun": "status='dryrun'", "live": "status<>'dryrun' AND filled > 0"}
_FILLED = {"dryrun": "shares", "live": "filled"}
_AVG = {"dryrun": "price", "live": "avg_price"}


def _closed_trade_returns(conn: sqlite3.Connection, upto: str, mode: str) -> list[float]:
    """每只代码从建仓到净持仓归零算一笔：买卖各自按股数加权算均价，平仓时记一笔 (卖出均价/买入均价 - 1)。"""
    rows = conn.execute(
        f"SELECT code, side, {_AVG[mode]}, {_FILLED[mode]} FROM live_orders "
        f"WHERE {_DONE[mode]} AND substr(ts, 1, 10) <= ? ORDER BY code, ts, id", (upto,)).fetchall()
    returns = []
    cycles: dict[str, dict] = {}
    for code, side, avg_price, filled in rows:
        cy = cycles.setdefault(code, {"buy_shares": 0, "buy_cost": 0.0, "sell_shares": 0, "sell_cost": 0.0, "open": 0})
        if side == "buy":
            cy["buy_shares"] += filled
            cy["buy_cost"] += avg_price * filled
            cy["open"] += filled
        else:
            cy["sell_shares"] += filled
            cy["sell_cost"] += avg_price * filled
            cy["open"] -= filled
            if cy["open"] <= 0 and cy["buy_shares"] > 0:
                buy_avg = cy["buy_cost"] / cy["buy_shares"]
                sell_avg = cy["sell_cost"] / cy["sell_shares"]
                returns.append(sell_avg / buy_avg - 1)
                cycles[code] = {"buy_shares": 0, "buy_cost": 0.0, "sell_shares": 0, "sell_cost": 0.0, "open": 0}
    return returns


def live_report_lines(conn: sqlite3.Connection, day: date) -> list[str]:
    """「实盘跟单」段：只写百分比和笔数，不写金额、股数（报告会推到 GitHub）。
    live_orders 从没有过记录、且当前模式为 off（从没跑过实盘）→ 不出这一段。"""
    ever_run = conn.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0]
    current_mode = live_store.mode(conn)
    if ever_run == 0 and current_mode == "off":
        return []

    d = day.isoformat()
    title = "实盘跟单（演练）" if current_mode == "dryrun" else "实盘跟单"
    mode = "dryrun" if current_mode == "dryrun" else "live"
    lines = ["", f"## {title}", ""]

    sim_n = conn.execute("SELECT COUNT(*) FROM fills WHERE account=? AND substr(ts, 1, 10)=?",
                         (LIVE_ACCOUNT, d)).fetchone()[0]
    live_n = conn.execute(f"SELECT COUNT(*) FROM live_orders WHERE {_DONE[mode]} AND substr(ts, 1, 10)=?",
                          (d,)).fetchone()[0]
    skip_rows = conn.execute(
        "SELECT note, COUNT(*) FROM live_intents WHERE status='skipped' AND substr(ts, 1, 10)=? "
        "GROUP BY note ORDER BY COUNT(*) DESC, note", (d,)).fetchall()
    skip_n = sum(cnt for _, cnt in skip_rows)
    lines.append(f"今天模拟 {LIVE_ACCOUNT} 成交 {sim_n} 笔 → 实盘成交 {live_n} 笔、跳过 {skip_n} 笔")
    if skip_rows:
        lines.append("跳过原因：" + "、".join(f"{note or '未注明'} {cnt} 笔" for note, cnt in skip_rows))

    if mode == "live":   # 演练的委托价是算出来的，滑点没有意义
        # 买单挂着意图（模拟成交价）；卖单按持仓对齐、不挂意图 → 对照这一轮持仓期间模拟盘最近的卖出意图
        slip_rows = conn.execute(
            "SELECT o.side, o.code, o.avg_price, CASE WHEN o.side='buy' THEN "
            "  (SELECT i.sim_price FROM live_intents i WHERE i.id=o.intent_id) ELSE "
            "  (SELECT i.sim_price FROM live_intents i WHERE i.source='sim' AND i.side='sell' AND i.code=o.code "
            "     AND i.ts<=o.ts AND i.ts>=COALESCE((SELECT MAX(b.ts) FROM live_orders b WHERE b.side='buy' "
            "       AND b.code=o.code AND b.filled>0 AND b.ts<=o.ts), '') ORDER BY i.id DESC LIMIT 1) END "
            "FROM live_orders o WHERE o.status<>'dryrun' AND o.filled > 0 AND substr(o.ts, 1, 10)=? "
            "ORDER BY o.id", (d,)).fetchall()
        for side, label in (("buy", "买入滑点"), ("sell", "卖出滑点")):
            items = [(code, (avg / sim - 1) * 100) for s, code, avg, sim in slip_rows if s == side and sim]
            if items:
                lines.append(f"{label}：" + "、".join(f"{code} {pct:+.2f}%" for code, pct in items))

    base_day = conn.execute(
        f"SELECT MIN(substr(ts, 1, 10)) FROM live_orders WHERE {_DONE[mode]} AND substr(ts, 1, 10) <= ?",
        (d,)).fetchone()[0]
    if base_day:
        base_equity = conn.execute("SELECT value FROM equity WHERE date=? AND account=?",
                                   (base_day, LIVE_ACCOUNT)).fetchone()
        now_equity = conn.execute("SELECT value FROM equity WHERE date=? AND account=?",
                                  (d, LIVE_ACCOUNT)).fetchone()
        if base_equity and now_equity and base_equity[0]:
            sim_pct = (now_equity[0] / base_equity[0] - 1) * 100
            trades = _closed_trade_returns(conn, d, mode)
            if trades:
                prod = 1.0
                for r in trades:
                    prod *= (1 + r)
                real_str = f"{(prod - 1) * 100:+.2f}%"
            else:
                real_str = "—"
            lines.append(f"累计收益率：实盘 {real_str}｜模拟 {sim_pct:+.2f}%")
    return lines


def write_report(conn: sqlite3.Connection, ledger: Ledger, day: date, prices: dict[str, float],
                 overseas: str | None, sentiment: str | None, out_dir=REPORT_DIR,
                 names: dict[str, str] | None = None, articles: list[dict] | None = None) -> Path:
    names = names or {}
    fills = ledger.fills_on(day)
    account_names = accounts_mod.account_names(conn, day)
    groups, strategy_labels = _strategy_groups(conn, day)
    lines = [f"# 复盘 {day.isoformat()}", "",
             f"外围：{overseas or '未知'}　情绪：{sentiment or '未知'}",
             "今日参考文章：" + ("；".join(f"{a['title']}（{a['link']}）" + ("" if hi189.in_judgement(a) else "【只记录，未参与判断】")
                                         for a in articles) if articles else "未取到"),
             "",
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
    lines += [f"| {ts[11:16]} | {strategy_labels.get(sid) or sid or '未知'} | {code} "
              f"| {names.get(code, '')} | {VERDICT_LABELS.get(verdict, verdict)} | {detail} |"
              for ts, sid, code, verdict, detail in rows]

    try:
        from jevq.data import own_sectors
        lines += own_sectors.report_lines(conn, day, names)
    except Exception as e:      # 自建板块只是记录，报告不能因它失败
        print(f"[report] 自建板块一节生成失败：{e!r}")

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

    lines += live_report_lines(conn, day)

    lines += ["", "> 成交价取分钟快照，开盘附近会比实盘略好。"]

    path = Path(out_dir) / f"{day.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
