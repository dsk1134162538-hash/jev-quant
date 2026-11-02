from research import entry_points as ep


def _b(t, o, h, lo, c):
    return (t, o, h, lo, c, 100)


def test_nine_percent_entry_flags_bar_that_closes_at_limit_and_skips_limit_open():
    b = [_b("09:35", 10.2, 10.5, 10.1, 10.4), _b("09:40", 10.4, 11.0, 10.4, 11.0)]
    assert ep.entry_at(b, 10.0, 1.09, "10:30") == (10.9, True)
    assert ep.entry_at([_b("09:35", 11.0, 11.0, 11.0, 11.0)], 10.0, 1.09, "10:30") is None


def test_velocity_entry_uses_only_lows_before_the_trigger():
    slow = [_b("09:35", 10.5, 10.6, 10.5, 10.6), _b("09:40", 10.6, 10.75, 10.55, 10.7)]
    fast = [_b("09:35", 10.2, 10.3, 10.2, 10.3), _b("09:40", 10.3, 10.8, 10.3, 10.8)]
    assert ep.entry_at(slow, 10.0, 1.07, "10:30", velocity=0.03) is None     # 10.7/10.5 ≈ 1.9%
    assert ep.entry_at(fast, 10.0, 1.07, "10:30", velocity=0.03) == (10.7, False)   # 10.7/10.2 ≈ 4.9%
    assert ep.entry_at(fast, 10.0, 1.07, "09:35", velocity=0.03) is None     # 过了截止时间
