from datetime import date

import pytest
import requests

from jevq.data.tencent import (fetch_kline, five_day_return, parse_index_pcts, parse_quotes,
                               parse_sina_a50, symbol)


def tencent_line(sym, name, code, price, pre, op, high, low, vol, amount_wan, up, down,
                 pct="0.00", ts="20260923100547"):
    f = [""] * 50
    f[0], f[1], f[2], f[3], f[4], f[5], f[6] = "1", name, code, price, pre, op, vol
    f[30], f[32], f[33], f[34], f[37], f[47], f[48] = ts, pct, high, low, amount_wan, up, down
    return f'v_{sym}="{"~".join(f)}";'


def test_symbol():
    assert symbol("600519") == "sh600519"
    assert symbol("000001") == "sz000001"


def test_parse_quotes():
    text = (tencent_line("sh603136", "天目湖", "603136", "9.36", "9.32", "9.33", "9.40",
                         "9.21", "53106", "4972", "10.25", "8.39")
            + "\n" + tencent_line("sz000001", "平安银行", "000001", "11.59", "11.71", "11.68",
                                  "11.71", "11.58", "97618", "11302.5", "12.88", "10.54"))
    qs = parse_quotes(text)
    q = qs["603136"]
    assert q.name == "天目湖" and q.price == 9.36 and q.pre_close == 9.32
    assert q.volume == 53106 and q.amount == 49_720_000.0
    assert q.limit_up == 10.25 and q.limit_down == 8.39
    assert q.ts.hour == 10 and q.ts.minute == 5
    assert qs["000001"].pct == -1.02


def test_parse_quotes_skips_garbage():
    assert parse_quotes('v_pv_none_match="1";') == {}


def test_five_day_return_excludes_today():
    rows = [("2026-09-15", 10.0), ("2026-09-16", 10.2), ("2026-09-17", 10.4),
            ("2026-09-18", 10.6), ("2026-09-21", 10.8), ("2026-09-22", 11.0),
            ("2026-09-23", 20.0)]
    assert five_day_return(rows, date(2026, 9, 23)) == 10.0
    assert five_day_return(rows[:5], date(2026, 9, 23)) is None


def test_parse_index_pcts():
    text = (tencent_line("usDJI", "道琼斯", "DJI", "51863.69", "52048.83", "0", "0", "0",
                         "0", "0", "0", "0", pct="-0.36")
            + tencent_line("hkHSI", "恒生指数", "HSI", "24850.9", "25087.7", "0", "0", "0",
                           "0", "0", "0", "0", pct="-0.94"))
    assert parse_index_pcts(text) == {"usDJI": -0.36, "hkHSI": -0.94}


def test_parse_sina_a50():
    text = 'var hq_str_hf_CHA50CFD="14573.730,,14573.000,14574.000,14714.000,14571.000,10:05:50,14654.000,14659.000,765012";'
    assert parse_sina_a50(text) == -0.55


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, resp):
        self.resp = resp

    def get(self, url, params=None, timeout=None):
        return self.resp


def test_fetch_kline_parses_qfqday():
    payload = {"data": {"sh600519": {"qfqday": [
        ["2026-09-21", "1", "1", "1", "1", "1"],
        ["2026-09-22", "1", "2", "1", "1", "1"]]}}}
    s = FakeSession(FakeResponse(200, payload))
    assert fetch_kline("600519", session=s) == [("2026-09-21", 1.0), ("2026-09-22", 2.0)]


def test_fetch_kline_raises_on_501_waf_block():
    s = FakeSession(FakeResponse(501, None))
    with pytest.raises(requests.HTTPError):
        fetch_kline("600519", session=s)


def test_fetch_overseas_prints_a50_failure(capsys):
    from jevq.data.tencent import fetch_overseas

    class R:
        content = b""

    class S:
        def get(self, url, **kw):
            if "sina" in url:
                raise RuntimeError("a50 down")
            return R()

    assert fetch_overseas(session=S()) == {}
    assert "a50 down" in capsys.readouterr().out
