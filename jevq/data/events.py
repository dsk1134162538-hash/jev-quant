"""持有期内有财报、大额解禁的票主账户不买（用户 2026-10-19「都做」第 1、4 条）。

- 财报：东财定期报告预约披露时间（akshare stock_yysj_em），取最近两个报告期（三季报季就是 0930 和 0630），
  有效日期 = 实际披露时间，没有就取最后一次变更日期，再没有就是首次预约。盘前每天刷新（公司常改期）。
  买入日 ≤ 披露日 ≤ 最晚卖出日 → 不买：业绩暴雷第二天常低开 -5%~-10%，硬止损兜不住；已经披露过的不拦（公告排雷会看）。
- 解禁：东财限售解禁明细（stock_restricted_release_detail_em），未来 UNLOCK_LOOKAHEAD_DAYS 天；
  买入日之后、最晚卖出日之前（含）有解禁、且占解禁前流通市值 ≥ UNLOCK_MIN_RATIO → 不买。
「最晚卖出日」按各主账户最多拿几个交易日算（超跌 3、低吸 2、扫首板 1），交易日历由调用方给。
数据拿不到 → 不拦（照常买），日志里说明。"""
import sqlite3
from datetime import date, timedelta

import akshare as ak
import pandas as pd

from jevq import config
from jevq.data.timeout import call_with_timeout

_DATE_COLS = ("实际披露时间", "三次变更日期", "二次变更日期", "一次变更日期", "首次预约时间")


def report_periods(today: date) -> list[str]:
    """最近两个已经结束的报告期（YYYYMMDD）：10-19 → 20260930、20260630。"""
    ends = [date(y, m, d) for y in (today.year - 1, today.year) for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))]
    past = [e for e in ends if e < today]
    return [e.strftime("%Y%m%d") for e in past[-2:]][::-1]


def _d(v) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return None
    try:
        ts = pd.Timestamp(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(ts) else ts.date().isoformat()


def fetch_report_dates(period: str) -> list[tuple[str, str, str, bool]]:
    """[(代码, 报告期, 有效披露日, 是否已实际披露)]。"""
    df = call_with_timeout(ak.stock_yysj_em, symbol="沪深A股", date=period, timeout=config.EVENTS_FETCH_TIMEOUT)
    out = []
    for _, r in df.iterrows():
        actual = _d(r.get("实际披露时间"))
        eff = next((x for x in (_d(r.get(c)) for c in _DATE_COLS) if x), None)
        if eff:
            out.append((str(r["股票代码"]).zfill(6), period, eff, actual is not None))
    return out


def fetch_unlocks(start: date, end: date) -> list[tuple[str, str, float, str]]:
    """[(代码, 解禁日, 占解禁前流通市值比例 0~1, 限售股类型)]。"""
    df = call_with_timeout(ak.stock_restricted_release_detail_em, start_date=start.strftime("%Y%m%d"),
                           end_date=end.strftime("%Y%m%d"), timeout=config.EVENTS_FETCH_TIMEOUT)
    out = []
    for _, r in df.iterrows():
        d = _d(r.get("解禁时间"))
        try:
            ratio = float(r.get("占解禁前流通市值比例"))
        except (TypeError, ValueError):
            continue
        if d and not pd.isna(ratio):
            out.append((str(r["股票代码"]).zfill(6), d, ratio, str(r.get("限售股类型") or "")))
    return out


def fetch_ex_rights(period: str) -> list[tuple[str, str, float, float]]:
    """东财分红送配（stock_fhps_em）：[(代码, 除权除息日, 每 10 股送转, 每 10 股派现)]，只要已经实施的。"""
    df = call_with_timeout(ak.stock_fhps_em, date=period, timeout=config.EVENTS_FETCH_TIMEOUT)
    out = []
    for _, r in df.iterrows():
        d = _d(r.get("除权除息日"))
        if not d:
            continue
        bonus = r.get("送转股份-送转总比例")
        cash = r.get("现金分红-现金分红比例")
        bonus = 0.0 if bonus is None or pd.isna(bonus) else float(bonus)
        cash = 0.0 if cash is None or pd.isna(cash) else float(cash)
        if bonus or cash:
            out.append((str(r["代码"]).zfill(6), d, bonus, cash))
    return out


def dividend_periods(today: date) -> list[str]:
    """最近两个年报/中报期（分红送配按这两种期）。"""
    ends = [date(y, m, d) for y in (today.year - 1, today.year) for m, d in ((6, 30), (12, 31))]
    return [e.strftime("%Y%m%d") for e in [e for e in ends if e < today][-2:]][::-1]


def backfill_ex_rights(conn: sqlite3.Connection, today: date, fetch=fetch_ex_rights, log=print) -> int:
    """收盘价库里 10-19 之前没存调整后的昨收，除权除息日会被当成下跌：按东财分红送配补上。"""
    from jevq.data import closes
    rows = []
    for p in dividend_periods(today):
        try:
            rows += fetch(p)
        except Exception as e:
            log(f"分红送配获取失败（{p}）：{e!r}")
    cutoff = (today - timedelta(days=config.EX_RIGHTS_BACKFILL_DAYS)).isoformat()
    return closes.backfill_ex_rights(conn, [r for r in rows if cutoff <= r[1] < today.isoformat()])


def refresh(conn: sqlite3.Connection, today: date, fetch_reports=fetch_report_dates, fetch_unlock=fetch_unlocks,
            log=print) -> dict:
    """每个来源单独成败：失败的那份保留库里旧数据。"""
    stats = {"reports": 0, "unlocks": 0}
    rows = []
    try:
        for p in report_periods(today):
            rows += fetch_reports(p)
    except Exception as e:
        log(f"财报预约披露日获取失败：{e!r}")
        rows = None
    if rows:
        with conn:
            conn.execute("DELETE FROM report_dates")
            conn.executemany("INSERT OR REPLACE INTO report_dates(code, period, date, disclosed, updated) "
                             "VALUES (?, ?, ?, ?, ?)", [(c, p, d, int(a), today.isoformat()) for c, p, d, a in rows])
        stats["reports"] = len(rows)
    try:
        ul = fetch_unlock(today, today + timedelta(days=config.UNLOCK_LOOKAHEAD_DAYS))
    except Exception as e:
        log(f"限售解禁获取失败：{e!r}")
        ul = None
    if ul is not None:
        with conn:
            conn.execute("DELETE FROM unlocks")
            conn.executemany("INSERT OR REPLACE INTO unlocks(code, date, ratio, kind, updated) VALUES (?, ?, ?, ?, ?)",
                             [(c, d, r, k, today.isoformat()) for c, d, r, k in ul])
        stats["unlocks"] = len(ul)
    return stats


def exit_day(today: date, max_hold: int, calendar: list[date] | None) -> date:
    """最晚卖出日：今天之后第 max_hold 个交易日；日历不够就按工作日（只会偏早）。"""
    days = sorted(d for d in (calendar or ()) if d > today)
    if len(days) >= max_hold:
        return days[max_hold - 1]
    cur, left = today, max_hold
    while left:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            left -= 1
    return cur


class EventFilter:
    """主账户买入前：持有期内有财报 / 大额解禁的不买。"""

    def __init__(self, reports: dict[str, list[tuple[str, str]]], unlocks: dict[str, list[tuple[str, float]]],
                 today: date, calendar: list[date] | None):
        self.reports, self.unlocks = reports, unlocks
        self.today, self.calendar = today, calendar

    @classmethod
    def load(cls, conn: sqlite3.Connection, today: date, calendar: list[date] | None) -> "EventFilter":
        reports: dict[str, list[tuple[str, str]]] = {}
        for code, period, d in conn.execute("SELECT code, period, date FROM report_dates WHERE disclosed = 0"):
            reports.setdefault(code, []).append((d, period))
        unlocks: dict[str, list[tuple[str, float]]] = {}
        for code, d, ratio in conn.execute("SELECT code, date, ratio FROM unlocks WHERE ratio >= ?",
                                           (config.UNLOCK_MIN_RATIO,)):
            unlocks.setdefault(code, []).append((d, ratio))
        return cls(reports, unlocks, today, calendar)

    def blocked(self, code: str, max_hold: int) -> str | None:
        last = exit_day(self.today, max_hold, self.calendar).isoformat()
        t = self.today.isoformat()
        for d, period in sorted(self.reports.get(code, ())):
            if t <= d <= last:
                return f"持有期内要发财报：{period[:4]}-{period[4:6]} 期报告预约 {d} 披露（最晚 {last} 卖）"
        for d, ratio in sorted(self.unlocks.get(code, ())):
            if t < d <= last:
                return f"持有期内大额解禁：{d} 解禁占流通市值 {ratio * 100:.1f}%（最晚 {last} 卖）"
        return None
