"""股东户数太多、股性不活跃的票不买（用户 2026-10-19：「低活跃股和八万以上股东的股不买剔除掉」）。

- 股东户数：东财最新一期（akshare stock_zh_a_gdhs，季报/半年报披露，变化慢），存 holders 表，7 天刷新一次；
  > HOLDERS_MAX（8 万户）不买。
- 低活跃：近 LOW_ACTIVITY_DAYS（20）个交易日每天涨跌幅绝对值的平均 < LOW_ACTIVITY_ABSRET（1.5%）不买。
  线上只有收盘价历史，用它近似日内振幅（回测里和 20 日平均振幅相关系数 0.92，1.5% ≈ 振幅 3%）。
  按换手率定义的「低活跃」回测不成立（换手 < 1% 的反而不差），所以没用换手率。
回测（77 天、超跌主账户新卖法）：两条一起剔，普跌日日均 +1.67% → +1.81%，全部环境 +0.77% → +1.12%；
低吸影响持平；扫首板里股东 > 8 万的首板次日反而更好（+1.77% 对 +0.60%），所以扫首板不用。
数据拿不到的票不拦（宁可照常买，不因为缺数据错过）。"""
import sqlite3
import statistics
from datetime import date

import akshare as ak

from jevq import config
from jevq.data.timeout import call_with_timeout


def fetch_holders() -> list[tuple[str, int, str]]:
    df = call_with_timeout(ak.stock_zh_a_gdhs, symbol="最新", timeout=config.HOLDERS_FETCH_TIMEOUT)
    out = []
    for _, r in df.iterrows():
        try:
            out.append((str(r["代码"]).zfill(6), int(r["股东户数-本次"]), str(r["股东户数统计截止日-本次"])[:10]))
        except (TypeError, ValueError):
            continue
    return out


def needs_refresh(conn: sqlite3.Connection, today: date, max_age_days: int = 7) -> bool:
    row = conn.execute("SELECT MAX(updated) FROM holders").fetchone()
    return not row or not row[0] or (today - date.fromisoformat(row[0])).days >= max_age_days


def refresh(conn: sqlite3.Connection, today: date, fetch=fetch_holders, log=print, min_rows: int = 1000) -> int:
    """拿到的行太少（接口异常）就保留旧数据。返回写入条数。"""
    try:
        rows = fetch()
    except Exception as e:
        log(f"股东户数获取失败：{e!r}")
        return 0
    if len(rows) < min_rows:
        log(f"股东户数只拿到 {len(rows)} 条，保留旧数据")
        return 0
    with conn:
        conn.execute("DELETE FROM holders")
        conn.executemany("INSERT OR REPLACE INTO holders(code, holders, as_of, updated) VALUES (?, ?, ?, ?)",
                         [(c, h, d, today.isoformat()) for c, h, d in rows])
    log(f"股东户数刷新完成：{len(rows)} 只")
    return len(rows)


def load(conn: sqlite3.Connection) -> dict[str, int]:
    return dict(conn.execute("SELECT code, holders FROM holders"))


def mean_abs_change(conn: sqlite3.Connection, codes, today: date, days: int | None = None) -> dict[str, float]:
    """今天之前最近 days 个交易日（收盘价库里的日期）每天涨跌幅绝对值的平均（%）；数据不够一半的票不给。"""
    n = config.LOW_ACTIVITY_DAYS if days is None else days
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT ?",
                                        (today.isoformat(), n + 1))][::-1]
    if len(dates) < 2:
        return {}
    want = set(codes)
    px: dict[str, dict[str, float]] = {}
    marks = ",".join("?" * len(dates))
    for code, d, close in conn.execute(f"SELECT code, date, close FROM closes WHERE date IN ({marks}) AND close > 0",
                                       dates):
        if code in want:
            px.setdefault(code, {})[d] = close
    out = {}
    for code, s in px.items():
        v = [abs(s[b] / s[a] - 1) * 100 for a, b in zip(dates, dates[1:]) if a in s and b in s]
        if len(v) >= max(2, n // 2):
            out[code] = round(statistics.mean(v), 2)
    return out


class StockFilter:
    """主账户买入前的个股过滤：股东户数、股性活跃度。"""

    def __init__(self, holders: dict[str, int], activity: dict[str, float]):
        self.holders, self.activity = holders, activity

    @classmethod
    def load(cls, conn: sqlite3.Connection, codes, today: date) -> "StockFilter":
        return cls(load(conn), mean_abs_change(conn, codes, today))

    def blocked(self, code: str) -> str | None:
        h = self.holders.get(code)
        if h is not None and h > config.HOLDERS_MAX:
            return f"股东户数 {h / 1e4:.1f} 万（> {config.HOLDERS_MAX / 1e4:g} 万）"
        a = self.activity.get(code)
        if a is not None and a < config.LOW_ACTIVITY_ABSRET:
            return (f"股性不活跃：近 {config.LOW_ACTIVITY_DAYS} 日平均每天涨跌 {a:.2f}%"
                    f"（< {config.LOW_ACTIVITY_ABSRET:g}%）")
        return None
