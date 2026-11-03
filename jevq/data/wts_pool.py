"""弱转强候选池（盘前算）：昨天炸板的高标。

炸板 = 昨天最高价摸到涨停价、收盘没封住；高标 = 炸板前（前天收盘）已经 ≥WTS_MIN_PRIOR_STREAK 连板。
库里 closes 没有最高价，所以只对「前天还在 ≥2 连板、昨天没继续涨停」的少数票（一般十几二十只）去新浪取日 K 看昨天的最高价。
取不到的票跳过（宁可少一只，不拿猜的）。
"""
from collections.abc import Callable, Iterable
from datetime import date

from jevq import config
from jevq.data.closes import limit_streaks


def build_pool(conn, codes: Iterable[str], today: date, fetch_daily: Callable[[str], list[dict]],
               log: Callable[[str], None] = print) -> dict[str, int]:
    """fetch_daily(code) -> [{"day": "YYYY-MM-DD", "high": .., "close": ..}, ...] 按日期升序。返回 {代码: 炸板前连板数}。"""
    codes = list(codes)
    row = conn.execute("SELECT MAX(date) FROM closes WHERE date < ?", (today.isoformat(),)).fetchone()
    if not row or not row[0]:
        return {}
    yday = date.fromisoformat(row[0])
    before = limit_streaks(conn, codes, yday)           # 截至前天的连板数
    after = limit_streaks(conn, codes, today)           # 截至昨天的连板数：还在涨停的不是断板
    cands = {c: n for c, n in before.items() if n >= config.WTS_MIN_PRIOR_STREAK and c not in after}
    pool = {}
    for code, n in cands.items():
        try:
            bars = fetch_daily(code)
        except Exception as e:                           # noqa: BLE001
            log(f"弱转强：取 {code} 日线失败，跳过：{e!r}")
            continue
        idx = next((i for i, b in enumerate(bars) if b["day"] == yday.isoformat()), None)
        if idx is None or idx == 0:
            continue
        limit = round(bars[idx - 1]["close"] * 1.1 + 1e-9, 2)
        if bars[idx]["high"] >= limit and bars[idx]["close"] < limit:
            pool[code] = n
    return pool
