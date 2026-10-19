"""每周核对（用户 2026-10-19「都做」第 3 条）：规则是同一段 77 天数据调出来的，实盘/模拟盘跑出来要和回测对得上。

每周最后一个交易日收盘后，按主账户把成交配成一买一卖的「回合」，算本周和规则改动以来的笔数、胜率、每笔平均盈亏，
对比回测期望（config.REVIEW_EXPECT）；笔数够了、明显差于回测 → 标「建议复盘」。只提醒，不停手、不改规则。"""
import sqlite3
import statistics
from datetime import date, timedelta

from jevq import config


def round_trips(conn: sqlite3.Connection, account: str, since: date) -> list[dict]:
    """买入日 ≥ since 的回合：{code, buy_date, sell_date, ret%}（含买卖手续费）。还没卖的不算。"""
    open_buys: dict[str, tuple] = {}
    out = []
    for code, side, price, shares, fee, ts in conn.execute(
            "SELECT code, side, price, shares, fee, ts FROM fills WHERE account=? ORDER BY ts, id", (account,)):
        if side == "buy":
            open_buys[code] = (price, shares, fee, ts[:10])
            continue
        b = open_buys.pop(code, None)
        if b is None or b[3] < since.isoformat():
            continue
        cost = b[0] * b[1] + b[2]
        got = price * shares - fee
        out.append({"code": code, "buy_date": b[3], "sell_date": ts[:10], "ret": (got / cost - 1) * 100})
    return out


def _stats(trips: list[dict]) -> tuple[int, float | None, float | None]:
    if not trips:
        return 0, None, None
    rs = [t["ret"] for t in trips]
    return len(rs), sum(r > 0 for r in rs) / len(rs) * 100, statistics.mean(rs)


def is_week_end(day: date, calendar: list[date] | None) -> bool:
    """这周最后一个交易日：下一个交易日在下周（日历不可用就按周五）。"""
    if not calendar:
        return day.weekday() == 4
    nxt = next((d for d in sorted(calendar) if d > day), None)
    return nxt is None or nxt.isocalendar()[:2] != day.isocalendar()[:2]


def weekly_review(conn: sqlite3.Connection, day: date) -> tuple[list[str], bool]:
    """返回（报告行, 有没有该复盘的）。"""
    since = date.fromisoformat(config.REVIEW_SINCE)
    week_start = day - timedelta(days=day.weekday())
    lines = ["", "## 每周核对（实盘规则 vs 回测）", "",
             f"规则改动以来（买入日 ≥ {since:%m-%d}）按回合算，含手续费；回测是日线近似，只当参照。", "",
             "| 账户 | 本周 笔数/胜率/每笔 | 累计 笔数/胜率/每笔 | 回测 胜率/每笔 | 结论 |", "|---|---|---|---|---|"]
    flagged = False
    fmt = lambda k, w, m: "0 笔" if not k else f"{k} 笔 / {w:.0f}% / {m:+.2f}%"   # noqa: E731
    for account, (exp_win, exp_mean, note) in config.REVIEW_EXPECT.items():
        trips = round_trips(conn, account, since)
        week = [t for t in trips if t["sell_date"] >= week_start.isoformat()]
        n, win, mean = _stats(trips)
        wn, wwin, wmean = _stats(week)
        verdict = "笔数还少，先看着"
        if n >= config.REVIEW_MIN_TRADES:
            bad = (mean < exp_mean - config.REVIEW_MEAN_GAP) or (win < exp_win - config.REVIEW_WIN_GAP)
            verdict = "明显比回测差，建议复盘" if bad else "和回测大致对得上"
            flagged = flagged or bad
        lines.append(f"| {config.ACCOUNT_NAMES.get(account, account)} | {fmt(wn, wwin, wmean)} | {fmt(n, win, mean)} "
                     f"| {exp_win:.0f}% / {exp_mean:+.2f}%（{note}） | {verdict} |")
    for account in config.REVIEW_ALSO:       # 情绪轮动（实盘跟它）：跟着上面几个走，没有单独的回测期望
        trips = round_trips(conn, account, since)
        week = [t for t in trips if t["sell_date"] >= week_start.isoformat()]
        lines.append(f"| {config.ACCOUNT_NAMES.get(account, account)} | {fmt(*_stats(week))} | {fmt(*_stats(trips))} "
                     f"| — | 实盘跟的账户，看上面各策略 |")
    return lines, flagged


def push_text(lines: list[str]) -> str:
    rows = [ln for ln in lines if ln.startswith("| ") and not ln.startswith("| 账户")]
    return "；".join(r.strip("| ").replace(" | ", "，") for r in rows)
