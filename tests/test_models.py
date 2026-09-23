from factories import quote


def test_pct_relative_to_pre_close():
    q = quote(pct=-3.0, open_pct=-1.5, low_pct=-3.2)
    assert q.pct == -3.0
    assert q.open_pct == -1.5
    assert q.low_pct == -3.2


def test_suspended_when_no_volume():
    assert quote(volume=0).suspended
    assert not quote().suspended


def test_is_st():
    assert quote(name="*ST海润").is_st
    assert quote(name="ST康美").is_st
    assert not quote(name="贵州茅台").is_st
