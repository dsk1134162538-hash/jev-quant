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
                   "boards": 0, "ma": {}, "up_streak": 0, "down_streak": 0, "vols": [], "vols_until": None}
    }


def test_no_codes_returns_empty():
    c = conn()
    assert history_features(c, [], TODAY) == {}


def test_up_streak_counts_trailing_consecutive_gains():
    c = conn()
    dates = dates_before(TODAY, 21)
    # 最后三天逐日上涨（不要求涨停），倒数第四天走平/下跌，不计入
    closes = [10.0] * 17 + [10.0, 10.1, 10.5, 11.0]
    seed(c, "600009", dates, closes)
    feats = history_features(c, ["600009"], TODAY)["600009"]
    assert feats["up_streak"] == 3


def test_up_streak_does_not_cross_missing_day_gap():
    c = conn()
    dates = dates_before(TODAY, 21)
    seed(c, "FILLER", dates, [10.0] * 21)

    own_dates = dates[:-2] + dates[-1:]  # 缺倒数第二天
    own_closes = [10.0] * (len(own_dates) - 1) + [12.0]
    seed(c, "600010", own_dates, own_closes)
    feats = history_features(c, ["600010"], TODAY)["600010"]
    assert feats["up_streak"] == 0


def test_boards_does_not_bridge_a_gap_between_two_streaks():
    """涨停 3 个交易日前收尾，中间缺一天，昨天又收一次涨停：不能被数据缺口拼成一条连板。"""
    c = conn()
    dates = dates_before(TODAY, 21)
    seed(c, "FILLER", dates, [10.0] * 21)  # 让这 21 个日期都出现在全局日期集合里

    full = [10.0] * 17 + [11.00, 10.0, 10.0, 11.00]  # idx16=10.0(前收) idx17=11.00(涨停收尾)
    #                                                    idx18 缺 idx19=10.0 idx20=11.00(涨停收尾)
    own_dates = dates[:18] + dates[19:]
    own_closes = full[:18] + full[19:]
    seed(c, "600006", own_dates, own_closes)
    feats = history_features(c, ["600006"], TODAY)["600006"]

    assert feats["boards"] == 1                # 只算昨天收尾的这一条，不跟 3 天前的合并
    assert feats["limit_up"][3] is True         # 最近 3 对里含昨天那次涨停
    assert feats["limit_up"][5] is True         # 最近 5 对里，中间那对因缺数据算断点，不算涨停，但仍含昨天那次


def test_ret_n_missing_when_exact_anchor_date_has_no_row():
    """ret_n 要精确对上 N 个交易日前那一天；那天缺数据就算缺失，不用最近可得的收盘价顶替。"""
    c = conn()
    dates = dates_before(TODAY, 21)
    seed(c, "FILLER", dates, [10.0] * 21)  # 让这 21 个日期都留在全局日期集合里
    closes_full = [round(10.0 + i * 0.1, 2) for i in range(21)]
    own_dates = dates[:15] + dates[16:]        # 缺 dates[15]，正好是 ret_5 的精确锚点
    own_closes = closes_full[:15] + closes_full[16:]
    seed(c, "600007", own_dates, own_closes)
    feats = history_features(c, ["600007"], TODAY)["600007"]

    assert 5 not in feats["ret"]
    assert feats["ret"][3] == round((closes_full[20] / closes_full[17] - 1) * 100, 2)
    assert feats["ret"][10] == round((closes_full[20] / closes_full[10] - 1) * 100, 2)
    assert feats["ret"][20] == round((closes_full[20] / closes_full[0] - 1) * 100, 2)


def test_ma_n_uses_available_closes_in_window_despite_a_gap():
    """ma_n 不要求逐日连续，窗口内只要凑够 n 个实际存在的收盘价就算，用它们的均值。"""
    c = conn()
    dates = dates_before(TODAY, 21)
    closes_full = [round(10.0 + i * 0.1, 2) for i in range(21)]
    own_dates = dates[:18] + dates[19:]        # 缺 dates[18] 这一天
    own_closes = closes_full[:18] + closes_full[19:]
    seed(c, "600008", own_dates, own_closes)
    feats = history_features(c, ["600008"], TODAY)["600008"]

    assert feats["ma"][5] == round(sum(own_closes[-5:]) / 5, 2)
    assert feats["ma"][10] == round(sum(own_closes[-10:]) / 10, 2)
    assert feats["ma"][20] == round(sum(own_closes[-20:]) / 20, 2)


def test_down_streak_counts_trailing_consecutive_losses():
    c = conn()
    dates = dates_before(TODAY, 21)
    # 最后三天逐日下跌，倒数第四天是上涨，不计入（与 up_streak 镜像）
    closes = [10.0] * 16 + [10.0, 11.0, 10.8, 10.5, 10.1]
    seed(c, "600011", dates, closes)
    feats = history_features(c, ["600011"], TODAY)["600011"]
    assert feats["down_streak"] == 3
    assert feats["up_streak"] == 0


def test_down_streak_flat_day_breaks_and_up_streak_unaffected():
    c = conn()
    dates = dates_before(TODAY, 21)
    closes = [10.0] * 18 + [9.5, 9.5, 9.0]        # 走平一天打断连跌，只剩最后 1 天
    seed(c, "600012", dates, closes)
    feats = history_features(c, ["600012"], TODAY)["600012"]
    assert feats["down_streak"] == 1 and feats["up_streak"] == 0


def test_down_streak_does_not_cross_missing_day_gap():
    c = conn()
    dates = dates_before(TODAY, 21)
    seed(c, "FILLER", dates, [10.0] * 21)
    own_dates = dates[:-2] + dates[-1:]            # 缺倒数第二天
    own_closes = [12.0] * (len(own_dates) - 1) + [10.0]
    seed(c, "600013", own_dates, own_closes)
    feats = history_features(c, ["600013"], TODAY)["600013"]
    assert feats["down_streak"] == 0


def seed_vol(c, code, dates, vols):
    c.executemany("INSERT INTO closes(date, code, close, volume) VALUES (?, ?, 10.0, ?)",
                  [(d, code, v) for d, v in zip(dates, vols)])
    c.commit()


def test_vols_are_last_five_global_dates_oldest_first():
    c = conn()
    dates = dates_before(TODAY, 8)
    seed_vol(c, "600014", dates, [1, 2, 3, 4, 5, 6, 7, 8])
    feats = history_features(c, ["600014"], TODAY)["600014"]
    assert feats["vols"] == [4.0, 5.0, 6.0, 7.0, 8.0]
    assert feats["vols_until"] == dates[-1]


def test_vols_keep_null_and_gap_as_none():
    c = conn()
    dates = dates_before(TODAY, 5)
    seed_vol(c, "FILLER", dates, [1] * 5)
    seed_vol(c, "600015", dates[:2] + dates[3:], [1, 2, None, 5])   # 缺中间一天，最后一天量为 NULL
    feats = history_features(c, ["600015"], TODAY)["600015"]
    assert feats["vols"] == [1.0, 2.0, None, None, 5.0]     # 缺行（第 3 天）和 NULL（第 4 天）都是 None
