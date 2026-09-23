import time

import pytest

from jevq.data import akdata
from jevq.data.timeout import call_with_timeout


def slow(x, delay=0.0):
    time.sleep(delay)
    return x * 2


def test_returns_value_within_timeout():
    assert call_with_timeout(slow, 3, delay=0.0, timeout=1.0) == 6


def test_raises_timeout_error_when_too_slow():
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="slow 超时"):
        call_with_timeout(slow, 3, delay=1.0, timeout=0.05)
    assert time.monotonic() - t0 < 0.5


def test_propagates_exceptions():
    def bad():
        raise ValueError("x")

    with pytest.raises(ValueError):
        call_with_timeout(bad)


def test_akdata_calls_are_bounded(monkeypatch):
    monkeypatch.setattr(akdata, "AK_TIMEOUT", 0.05)
    monkeypatch.setattr(akdata.ak, "tool_trade_date_hist_sina", lambda: time.sleep(1.0))
    with pytest.raises(TimeoutError):
        akdata.trade_days()
