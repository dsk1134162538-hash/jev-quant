"""卖飞统计：每笔卖出之后这只票又走了多少——卖后当日最高、当日收盘、次日最高、次日收盘。

只记录，不参与交易（和封板时段统计一样，用来攒数据评估卖出规则）。任何一步失败由调用方兜底，
不影响交易和报告。卖后当日最高按分钟快照取最大值，会略低于真实最高（分钟内的冲高看不到）；
没跟踪到（盘中重启、这只票不在行情里）就记 None，不拿别的数顶替，但收盘价高于卖出价时至少记收盘价。
"""
from datetime import date

from jevq.models import Quote


def _sells_on(conn, day: date):
    return conn.execute(
        "SELECT id, account, code, price FROM fills WHERE side='sell' AND substr(ts, 1, 10)=? ORDER BY id",
        (day.isoformat(),)).fetchall()


class AfterSellTracker:
    """盘中每分钟调用 update：今天每笔卖出（fill id）之后见过的最高价，从卖出价起算。"""

    def __init__(self):
        self.highs: dict[int, float] = {}

    def update(self, conn, today: date, quotes: dict[str, Quote]) -> None:
        for fid, _account, code, price in _sells_on(conn, today):
            high = self.highs.get(fid, price)
            q = quotes.get(code)
            if q is not None and q.ts.date() == today:
                high = max(high, q.price)
            self.highs[fid] = high


def codes_to_quote(conn, today: date, prev_day: date | None) -> set[str]:
    """收盘时要额外拉行情的代码：今天和上一交易日卖出的票（可能已不在股票池里）。"""
    days = [today] + ([prev_day] if prev_day else [])
    return {code for d in days for _fid, _a, code, _p in _sells_on(conn, d)}


def record_day(conn, today: date, quotes: dict[str, Quote], after_highs: dict[int, float]) -> int:
    """收盘后给今天的每笔卖出写一行：卖出价、卖后当日最高、当日收盘。重复调用覆盖当日字段。"""
    rows = []
    for fid, account, code, price in _sells_on(conn, today):
        q = quotes.get(code)
        close = q.price if q is not None and q.ts.date() == today else None
        high = after_highs.get(fid)
        if close is not None and close > price:
            high = max(high or close, close)
        rows.append((fid, account, code, today.isoformat(), price, high, close))
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT INTO sell_followups(fill_id, account, code, sell_date, sell_price, after_high, day_close) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(fill_id) DO UPDATE SET "
            "after_high = COALESCE(excluded.after_high, after_high), "
            "day_close = COALESCE(excluded.day_close, day_close)", rows)
    return len(rows)


def fill_next_day(conn, prev_day: date, quotes: dict[str, Quote], today: date) -> int:
    """用今天收盘时的报价回填上一交易日卖出的次日最高/次日收盘；只用 ts 是今天的报价。"""
    pending = conn.execute(
        "SELECT fill_id, code FROM sell_followups WHERE sell_date=? AND next_close IS NULL",
        (prev_day.isoformat(),)).fetchall()
    updates = []
    for fid, code in pending:
        q = quotes.get(code)
        if q is None or q.ts.date() != today:
            continue
        updates.append((today.isoformat(), q.high, q.price, fid))
    if not updates:
        return 0
    with conn:
        conn.executemany("UPDATE sell_followups SET next_date=?, next_high=?, next_close=? WHERE fill_id=?",
                         updates)
    return len(updates)


def report_rows(conn, day: date) -> list[tuple]:
    """报告用：今天卖出的（只有当日数据）+ 今天回填了次日数据的（上一交易日卖出）。"""
    return conn.execute(
        "SELECT s.sell_date, s.account, s.code, f.reason, s.sell_price, s.after_high, s.day_close, "
        "s.next_high, s.next_close FROM sell_followups s JOIN fills f ON f.id = s.fill_id "
        "WHERE s.next_date = ? OR s.sell_date = ? ORDER BY s.sell_date, s.fill_id",
        (day.isoformat(), day.isoformat())).fetchall()
