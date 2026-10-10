from datetime import date

from jevq.data import ztpool


class Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class Session:
    def __init__(self, data):
        self.data, self.calls = data, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        return Resp(self.data)


def test_fetch_yesterday_parses_eastmoney_pool():
    s = Session({"data": {"pool": [{"c": "000869", "n": "张裕", "zdp": 3.3049, "ylbc": 1},
                                   {"c": "001300", "n": "三柏硕", "zdp": 9.9926, "ylbc": 2}]}})
    rows = ztpool.fetch_yesterday(date(2026, 10, 9), session=s)
    assert rows == [{"code": "000869", "name": "张裕", "pct": 3.3, "boards": 1},
                    {"code": "001300", "name": "三柏硕", "pct": 9.99, "boards": 2}]
    assert s.calls[0][1]["date"] == "20261009"


def test_fetch_yesterday_empty_when_no_data():
    assert ztpool.fetch_yesterday(date(2026, 10, 10), session=Session({"data": None})) == []


def test_yesterday_stats():
    rows = [{"pct": 10.0, "boards": 2}, {"pct": 2.0, "boards": 1}, {"pct": -6.0, "boards": 1}, {"pct": 0.0, "boards": 3}]
    assert ztpool.yesterday_stats(rows) == {
        "昨日涨停今日均涨%": 1.5, "昨日涨停今日红盘率%": 50.0, "昨日涨停今日跌超5%家数": 1,
        "昨日涨停今日再涨停家数": 1, "昨日连板今日均涨%": 5.0}
    assert ztpool.yesterday_stats([]) == {}
    assert "昨日连板今日均涨%" not in ztpool.yesterday_stats([{"pct": 1.0, "boards": 1}])


def test_fetch_reasons_parses_ths_pool():
    s = Session({"data": {"info": [{"code": "605058", "name": "澳弘电子", "reason_type": "PCB+AI数据中心电源",
                                    "high_days": "15天9板"}]}})
    assert ztpool.fetch_reasons(date(2026, 10, 9), session=s) == [
        {"code": "605058", "name": "澳弘电子", "reason": "PCB+AI数据中心电源", "high_days": "15天9板"}]
    assert s.calls[0][1]["date"] == "20261009"


def test_reason_sectors_counts_topics_and_keeps_hot_ones():
    rows = [{"name": "A", "reason": "PCB+AI数据中心电源"}, {"name": "B", "reason": "PCB＋业绩增长"},
            {"name": "C", "reason": "光模块+PCB"}, {"name": "D", "reason": "光模块"}, {"name": "E", "reason": ""}]
    sectors = ztpool.reason_sectors(rows, min_count=2)
    assert [(s.name, s.limit_up) for s in sectors] == [("涨停原因·PCB", ["A", "B", "C"]), ("涨停原因·光模块", ["C", "D"])]


def test_reason_sectors_skip_non_theme_words():
    rows = [{"name": n, "reason": "半年报增长+全产业链+业绩预增+回购+PCB"} for n in "AB"]
    assert [s.name for s in ztpool.reason_sectors(rows, min_count=2)] == ["涨停原因·PCB"]


def test_reason_sectors_match_concepts_through_article_hot():
    from jevq.data.hi189 import article_hot
    sectors = ztpool.reason_sectors([{"name": "A", "reason": "PCB"}, {"name": "B", "reason": "PCB"}], min_count=2)
    assert article_hot("PCB概念", ["000001"], {}, sectors) == "板块：涨停原因·PCB"
