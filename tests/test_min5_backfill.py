from research import min5_backfill as mb


def _bar(day, t, c, h=None):
    return {"day": f"{day} {t}:00", "open": c, "high": h or c, "low": c, "close": c, "volume": "100"}


def test_keep_only_touch_days_and_their_next_day():
    rows = [_bar("2026-10-28", "15:00", "10.0"),
            _bar("2026-10-29", "09:35", "10.2"), _bar("2026-10-29", "15:00", "10.1"),            # 最高 2%：丢
            _bar("2026-10-30", "09:35", "10.5", "10.75"), _bar("2026-10-30", "15:00", "10.6"),   # 6.4%：留
            _bar("2026-11-02", "09:35", "10.4"), _bar("2026-11-02", "15:00", "10.3")]            # 次日：留
    bars, days = mb.keep_days(rows)
    assert sorted(days) == [("2026-10-30", 10.1, "touch"), ("2026-11-02", 10.6, "next")]
    assert {b[0] for b in bars} == {"2026-10-30", "2026-11-02"} and len(bars) == 4
