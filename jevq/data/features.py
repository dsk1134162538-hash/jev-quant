"""历史特征：从本地 closes 表算近 N 日涨幅、近 N 日是否涨停过、连板数、均线。"""
from collections.abc import Iterable
from datetime import date

RET_NS = (3, 5, 10, 20)
LIMIT_UP_NS = (3, 5, 10, 20)
MA_NS = (5, 10, 20)


def history_features(conn, codes: Iterable[str], today: date) -> dict[str, dict]:
    """最近 21 个不同日期（date < today）算历史特征，一次性查询，按代码返回。

    每只代码返回 {"ret": {n: 涨幅}, "limit_up": {n: 是否涨停过}, "boards": 连板数, "ma": {n: 均线}}。
    ret/ma 数据不足对应 n 时该键缺失；limit_up 按已有的相邻日对判断，始终存在。
    """
    codes = list(codes)
    if not codes:
        return {}
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT 21",
        (today.isoformat(),)).fetchall()]
    if not dates:
        return {c: {"ret": {}, "limit_up": {n: False for n in LIMIT_UP_NS}, "boards": 0, "ma": {}}
                for c in codes}
    dates.sort()  # 升序，方便按相邻两天比较

    date_placeholders = ",".join("?" * len(dates))
    code_placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, date, close FROM closes WHERE date IN ({date_placeholders}) "
        f"AND code IN ({code_placeholders})", (*dates, *codes)).fetchall()
    by_code: dict[str, dict[str, float]] = {}
    for code, d, close in rows:
        by_code.setdefault(code, {})[d] = close

    out: dict[str, dict] = {}
    for code in codes:
        vals = by_code.get(code, {})
        c = [vals[d] for d in dates if d in vals]  # 该代码在这些日期上实际存在的收盘价，按日期升序

        ret = {}
        for n in RET_NS:
            if len(c) >= n + 1 and c[-1 - n]:
                ret[n] = round((c[-1] / c[-1 - n] - 1) * 100, 2)

        ma = {}
        for n in MA_NS:
            if len(c) >= n:
                ma[n] = round(sum(c[-n:]) / n, 2)

        # 相邻日对（缺日不跨比）：只在两天都有数据时才判断是否涨停
        flags: list[bool] = []
        for i in range(1, len(dates)):
            prev, cur = vals.get(dates[i - 1]), vals.get(dates[i])
            if prev is None or cur is None:
                continue
            flags.append(cur >= round(prev * 1.1, 2))

        limit_up = {n: any(flags[-n:]) for n in LIMIT_UP_NS}

        boards = 0
        for f in reversed(flags):
            if not f:
                break
            boards += 1

        out[code] = {"ret": ret, "limit_up": limit_up, "boards": boards, "ma": ma}
    return out
