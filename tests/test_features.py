from datetime import date, timedelta

from jevq import db
from jevq.data.features import history_features

TODAY = date(2026, 9, 24)


def conn():
    return db.connect(":memory:")


def dates_before(today, n):
    """从 today 往前数 n 个交易日（周一到周五），升序返回。"""
    out = []
    d = today
    while len(out) < n:
        d = d - timedelta(days=1)
        if d.weekday() >= 5:
            continue
        out.append(d.isoformat())
    out.reverse()  # 升序
    return out


def seed(c, code, dates, closes):
    c.executemany("INSERT INTO closes(date, code, close) VALUES (?, ?, ?)",
                  [(d, code, p) for d, p in zip(dates, closes)])
    c.commit()


def test_ret_ma_computed_with_21_days():
    c = conn()
    dates = dates_before(TODAY, 21)
    closes = [round(10.0 + i * 0.1, 2) for i in range(21)]
    seed(c, "600001", dates, closes)
    feats = history_features(c, ["600001"], TODAY)["600001"]

    assert feats["ret"][3] == round((closes[-1] / closes[-1 - 3] - 1) * 100, 2)
    assert feats["ret"][5] == round((closes[-1] / closes[-1 - 5] - 1) * 100, 2)
    assert feats["ret"][10] == round((closes[-1] / closes[-1 - 10] - 1) * 100, 2)
    assert feats["ret"][20] == round((closes[-1] / closes[-1 - 20] - 1) * 100, 2)

    assert feats["ma"][5] == round(sum(closes[-5:]) / 5, 2)
    assert feats["ma"][10] == round(sum(closes[-10:]) / 10, 2)
    assert feats["ma"][20] == round(sum(closes[-20:]) / 20, 2)

    # 温和上涨，没有涨停
    assert feats["boards"] == 0
    assert all(v is False for v in feats["limit_up"].values())


def test_limit_up_and_boards_count_trailing_streak():
    c = conn()
    dates = dates_before(TODAY, 21)
    closes = [10.0] * 18 + [10.0, 11.00, 12.10]  # 最后两天涨停（>=10%），倒数第三天不是
    seed(c, "600002", dates, closes)
    feats = history_features(c, ["600002"], TODAY)["600002"]

    assert feats["boards"] == 2
    assert feats["limit_up"][3] is True
    assert feats["limit_up"][5] is True


def test_limit_up_does_not_cross_missing_day_gap():
    c = conn()
    dates = dates_before(TODAY, 21)
    seed(c, "FILLER", dates, [10.0] * 21)  # 让这 21 个日期都出现在全局日期集合里

    # 600003 缺倒数第二天的数据：若跨过缺失的一天直接拿倒数第三天和最后一天比较会像涨停，
    # 但两笔比较都因为中间那天缺数据而被跳过，不该被计入
    own_dates = dates[:-2] + dates[-1:]
    own_closes = [10.0] * (len(own_dates) - 1) + [12.0]
    seed(c, "600003", own_dates, own_closes)
    feats = history_features(c, ["600003"], TODAY)["600003"]

    assert feats["boards"] == 0
    assert all(v is False for v in feats["limit_up"].values())


def test_insufficient_days_leaves_ret_and_ma_keys_missing():
    c = conn()
    dates = dates_before(TODAY, 2)
    closes = [10.0, 10.2]
    seed(c, "600004", dates, closes)
    feats = history_features(c, ["600004"], TODAY)["600004"]

    assert feats["ret"] == {}
    assert feats["ma"] == {}
    # limit_up 仍然按已有的 1 对数据判断（这里不是涨停）
    assert feats["limit_up"][3] is False
    assert feats["boards"] == 0


def test_no_history_returns_empty_dict():
    c = conn()
    assert history_features(c, ["600005"], TODAY) == {
        "600005": {"ret": {}, "limit_up": {3: False, 5: False, 10: False, 20: False},
                   "boards": 0, "ma": {}}
    }


def test_no_codes_returns_empty():
    c = conn()
    assert history_features(c, [], TODAY) == {}
