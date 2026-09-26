"""历史特征：从本地 closes 表算近 N 日涨幅、近 N 日是否涨停过、连板数、均线。"""
from collections.abc import Iterable
from datetime import date

RET_NS = (3, 5, 10, 20)
LIMIT_UP_NS = (3, 5, 10, 20)
MA_NS = (5, 10, 20)
VOL_DAYS = 5          # 阶梯缩量/放量的 N 最大 5，保留最近 5 个日期的成交量


def history_features(conn, codes: Iterable[str], today: date) -> dict[str, dict]:
    """最近 21 个不同日期（date < today）算历史特征，一次性查询，按代码返回。

    每只代码返回 {"ret": {n: 涨幅}, "limit_up": {n: 是否涨停过}, "boards": 连板数, "ma": {n: 均线},
    "up_streak": 连涨天数（截至昨日）, "down_streak": 连跌天数（截至昨日）,
    "vols": 最近 VOL_DAYS 个全局日期的成交量（手，升序；缺行或 NULL 为 None）, "vols_until": 最新日期}。
    ret/ma 数据不足对应 n 时该键缺失；limit_up 按已有的相邻日对判断，始终存在。
    """
    codes = list(codes)
    if not codes:
        return {}
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM closes WHERE date < ? ORDER BY date DESC LIMIT 21",
        (today.isoformat(),)).fetchall()]
    if not dates:
        return {c: {"ret": {}, "limit_up": {n: False for n in LIMIT_UP_NS}, "boards": 0, "ma": {},
                    "up_streak": 0, "down_streak": 0, "vols": [], "vols_until": None}
                for c in codes}
    dates.sort()  # 升序，方便按相邻两天比较

    date_placeholders = ",".join("?" * len(dates))
    code_placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, date, close, volume FROM closes WHERE date IN ({date_placeholders}) "
        f"AND code IN ({code_placeholders})", (*dates, *codes)).fetchall()
    by_code: dict[str, dict[str, float]] = {}
    vol_by_code: dict[str, dict[str, float]] = {}
    for code, d, close, volume in rows:
        by_code.setdefault(code, {})[d] = close
        if volume is not None and volume > 0:
            vol_by_code.setdefault(code, {})[d] = volume

    out: dict[str, dict] = {}
    for code in codes:
        vals = by_code.get(code, {})

        # ret_n：精确对上全局日期表里 N 个交易日前的那一天，那天没有行情就是缺失，
        # 不用别的日期顶替（否则会把「N 天前」悄悄挪到别的交易日）。
        ret = {}
        for n in RET_NS:
            if len(dates) >= n + 1:
                cur, prev = vals.get(dates[-1]), vals.get(dates[-1 - n])
                if cur is not None and prev:
                    ret[n] = round((cur / prev - 1) * 100, 2)

        # ma_n：窗口内不要求逐日连续，凑够 n 个实际存在的收盘价就用它们的均值。
        c = [vals[d] for d in dates if d in vals]  # 该代码在这些日期上实际存在的收盘价，按日期升序
        ma = {}
        for n in MA_NS:
            if len(c) >= n:
                ma[n] = round(sum(c[-n:]) / n, 2)

        # 涨停标记：严格对齐全局相邻日期对（不压缩、不跳过），缺数据的那一对记为断点（False），
        # 不跨过缺口比较——否则两段本不相连的涨停会被缺口“焊”成一条连板。
        flags: list[bool] = []
        for i in range(1, len(dates)):
            prev, cur = vals.get(dates[i - 1]), vals.get(dates[i])
            flags.append(prev is not None and cur is not None and cur >= round(prev * 1.1, 2))

        limit_up = {n: any(flags[-n:]) for n in LIMIT_UP_NS}

        boards = 0
        for f in reversed(flags):
            if not f:
                break
            boards += 1

        # 连涨天数：按收盘价逐日比较（不要求涨停），截至昨日；今日是否上涨由调用方另行判断。
        up_flags: list[bool] = []
        for i in range(1, len(dates)):
            prev, cur = vals.get(dates[i - 1]), vals.get(dates[i])
            up_flags.append(prev is not None and cur is not None and cur > prev)

        up_streak = 0
        for f in reversed(up_flags):
            if not f:
                break
            up_streak += 1

        # 连跌天数：与连涨天数镜像（收盘价逐日比较、截至昨日、缺口处断开）。
        down_flags: list[bool] = []
        for i in range(1, len(dates)):
            prev, cur = vals.get(dates[i - 1]), vals.get(dates[i])
            down_flags.append(prev is not None and cur is not None and cur < prev)

        down_streak = 0
        for f in reversed(down_flags):
            if not f:
                break
            down_streak += 1

        # 成交量：对齐全局最近 VOL_DAYS 个日期，缺行/NULL 留 None，由取值函数判断窗口内是否齐全。
        vols = [vol_by_code.get(code, {}).get(d) for d in dates[-VOL_DAYS:]]

        out[code] = {"ret": ret, "limit_up": limit_up, "boards": boards, "ma": ma, "up_streak": up_streak,
                     "down_streak": down_streak, "vols": vols, "vols_until": dates[-1]}
    return out
