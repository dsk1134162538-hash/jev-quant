import json
from datetime import date, datetime
from pathlib import Path

import pytest

from jevq import db
from jevq.data import hi189
from jevq.data.hi189 import Article, Sector

FIXTURE = Path(__file__).parent / "fixtures" / "hi189_evening.txt"
PREV, TODAY = date(2026, 9, 29), date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 8, 50)
LONG = "正文" * 150

RSS = """<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item><title>收评 &amp; 晚报</title><link>https://hi189.com/1.html</link><pubDate>Tue, 29 Sep 2026 11:00:00 +0000</pubDate>
<content:encoded><![CDATA[<p>第一段</p><p>第二段<br/>第三段</p><script>x()</script>]]></content:encoded></item>
<item><title>无日期</title><link>https://hi189.com/2.html</link></item>
</channel></rss>"""


def art(title, link, published, text=LONG):
    return Article(title, link, published, text)


def test_parse_feed_converts_to_beijing_time_and_cleans_html():
    (a,) = hi189.parse_feed(RSS)
    assert a.title == "收评 & 晚报" and a.published == datetime(2026, 9, 29, 19, 0)
    assert a.text == "第一段\n第二段\n第三段"


def test_eligible_filters_time_and_password():
    arts = [art("早", "a", datetime(2026, 9, 29, 14, 59)), art("晚报", "b", datetime(2026, 9, 29, 19, 0)),
            art("密码保护：晚报", "c", datetime(2026, 9, 29, 20, 0)), art("未来", "d", datetime(2026, 9, 30, 9, 0))]
    assert [a.link for a in hi189.eligible(arts, PREV, NOW)] == ["b"]


def test_refresh_caches_and_does_not_refetch():
    conn = db.connect(":memory:")
    calls = []

    def feed():
        calls.append(1)
        return [art("晚报", "b", datetime(2026, 9, 29, 19, 0))]

    got = hi189.refresh(conn, TODAY, PREV, NOW, fetch=feed, fetch_page=lambda l: "")
    assert [d["title"] for d in got] == ["晚报"] and got[0]["published"] == "2026-09-29T19:00"
    hi189.refresh(conn, TODAY, PREV, datetime(2026, 9, 30, 9, 0), fetch=feed, fetch_page=lambda l: "")
    assert len(calls) == 2                                      # 11:30 前每次都查，但同一篇只存一次
    assert conn.execute("SELECT COUNT(*) FROM day_cache WHERE kind='hi189'").fetchone()[0] == 1


def test_refresh_stops_fetching_after_cutoff_but_reads_cache():
    conn = db.connect(":memory:")
    hi189.refresh(conn, TODAY, PREV, NOW, fetch=lambda: [art("晚报", "b", datetime(2026, 9, 29, 19, 0))])

    def boom():
        raise AssertionError("11:30 后不该联网")

    got = hi189.refresh(conn, TODAY, PREV, datetime(2026, 9, 30, 11, 31), fetch=boom)
    assert [d["title"] for d in got] == ["晚报"]


def test_refresh_short_feed_text_falls_back_to_page():
    conn = db.connect(":memory:")
    got = hi189.refresh(conn, TODAY, PREV, NOW, fetch=lambda: [art("晚报", "b", datetime(2026, 9, 29, 19, 0), "短")],
                        fetch_page=lambda l: "页面正文")
    assert got[0]["text"] == "页面正文"


def test_refresh_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(hi189, "HI189_ENABLED", False)
    conn = db.connect(":memory:")
    assert hi189.refresh(conn, TODAY, PREV, NOW, fetch=lambda: pytest.fail("不该联网")) == []


def test_fetch_feed_failure_returns_empty():
    class Boom:
        def get(self, *a, **k):
            raise OSError("down")

    assert hi189.fetch_feed(Boom()) == []


def test_parse_sectors_from_evening_report():
    got = hi189.parse_sectors(FIXTURE.read_text(encoding="utf-8"))
    assert got == [Sector("机器人概念", ["甲股份", "乙科技"], ["丙电子", "丁智能"]),
                   Sector("算力", ["戊通信"], ["己光电", "庚网络"]),
                   Sector("光伏板块", [], ["辛能源", "壬材料"])]
    assert hi189.parse_sectors("没有任何结构的文字") == []


def test_article_hot_by_sector_name_either_direction():
    sectors = hi189.parse_sectors(FIXTURE.read_text(encoding="utf-8"))
    assert hi189.article_hot("机器人", ["1"], {}, sectors) == "板块：机器人概念"     # 题材名被板块名包含
    assert hi189.article_hot("光伏", ["1"], {}, sectors) == "板块：光伏板块"
    assert hi189.article_hot("算力租赁", ["1"], {}, sectors) == "板块：算力"        # 板块名被题材名包含
    assert hi189.article_hot("芯片", ["1"], {"1": "无关"}, sectors) == ""


def test_article_hot_by_member_stock_name():
    sectors = hi189.parse_sectors(FIXTURE.read_text(encoding="utf-8"))
    assert hi189.article_hot("芯片", ["600001", "600002"], {"600001": "无关", "600002": "丙电子"},
                             sectors) == "个股：丙电子"


def test_sectors_of_merges_articles():
    assert [s.name for s in hi189.sectors_of([{"text": FIXTURE.read_text(encoding="utf-8")}, {"text": ""}])] \
        == ["机器人概念", "算力", "光伏板块"]
    assert json.dumps(hi189.sectors_of([])) == "[]"
