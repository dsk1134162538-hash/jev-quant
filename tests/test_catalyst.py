from datetime import date, datetime

from jevq import db
from jevq.data import catalyst, market_extra, rss_news

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Humanoid robot &amp;#8217;s debut</title><link>https://a/1</link>
<pubDate>Fri, 09 Oct 2026 13:30:00 GMT</pubDate><description>&lt;p&gt;Tesla Optimus&lt;/p&gt;</description></item>
<item><title>Old news</title><link>https://a/2</link><pubDate>Mon, 05 Oct 2026 01:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>HBM4 sampling</title><link rel="alternate" href="https://b/1"/><updated>2026-10-10T00:10:00Z</updated>
<summary>memory</summary></entry></feed>"""


def test_parse_feed_rss_and_atom_to_beijing_time():
    items = rss_news.parse_feed(RSS)
    assert items[0]["title"] == "Humanoid robot ’s debut" and items[0]["link"] == "https://a/1"
    assert items[0]["published"] == datetime(2026, 10, 9, 21, 30) and items[0]["summary"] == "Tesla Optimus"
    atom = rss_news.parse_feed(ATOM)
    assert atom[0]["link"] == "https://b/1" and atom[0]["published"] == datetime(2026, 10, 10, 8, 10)


class Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_fetch_all_keeps_window_and_reports_failures():
    sources = [{"name": "A", "url": "u1", "hint": "robot"}, {"name": "B", "url": "u2", "hint": "semi"},
               {"name": "C", "url": "u3"}]

    def get(url, timeout=None, headers=None):
        if url == "u3":
            raise TimeoutError("slow")
        return Resp(RSS if url == "u1" else ATOM)
    res = rss_news.fetch_all(sources, start=datetime(2026, 10, 9, 15, 0), end=datetime(2026, 10, 10, 9, 0), get=get)
    assert [(i["source"], i["title"][:5]) for i in res["items"]] == [("B", "HBM4 "), ("A", "Human")]
    assert res["ok"] == 2 and res["failed"] == ["C"]


def _conn():
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO concepts VALUES (?, ?, '2026-10-09')",
                     [("000001", "人形机器人"), ("000002", "HBM概念"), ("000003", "融资融券"), ("000004", "固态电池"),
                      ("000005", "AI"), ("000006", "人形机器人")])
    conn.execute("INSERT INTO own_sectors VALUES ('s1', '光模块', '', '[\"CPO\"]', 'active', '2026-10-04')")
    conn.commit()
    return conn


def test_topic_terms_from_concepts_own_sectors_and_aliases():
    terms = catalyst.topic_terms(_conn())
    assert terms["人形机器人"] >= {"人形机器人", "humanoid", "optimus"}
    assert "hbm" in terms["HBM概念"] and "光模块" in terms["光模块"] and "cpo" in terms["光模块"]
    assert "融资融券" not in terms           # 不是题材的板块不匹配
    assert "AI" not in terms                  # 太短的名字（两个字母）误伤太多，不单独匹配


def test_generic_two_char_chinese_words_not_used_unless_curated():
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO concepts VALUES (?, ?, '2026-10-09')", [("1", "电商概念"), ("2", "光伏概念"), ("3", "黄金概念")])
    conn.execute("INSERT INTO own_sectors VALUES ('s1', '汽车零部件', '', '[\"汽车\", \"智能驾驶\"]', 'active', 'x')")
    conn.commit()
    terms = catalyst.topic_terms(conn)
    assert "电商概念" not in terms and "黄金概念" not in terms        # 「电商」「黄金」太泛（黄金周……）
    assert "光伏" in terms["光伏概念"]                                 # 关键词表里列过的两字词照用
    assert "汽车" not in terms["汽车零部件"] and "智能驾驶" in terms["汽车零部件"]


def test_digest_counts_news_per_topic():
    items = [{"title": "人形机器人量产提速", "summary": "", "source": "36氪", "link": "l1", "published": datetime(2026, 10, 9, 20)},
             {"title": "Humanoid robot debut", "summary": "", "source": "IEEE", "link": "l2", "published": datetime(2026, 10, 9, 22)},
             {"title": "固态电池新突破", "summary": "人形机器人也用", "source": "IT之家", "link": "l3", "published": datetime(2026, 10, 10, 7)},
             {"title": "无关新闻", "summary": "", "source": "x", "link": "l4", "published": datetime(2026, 10, 10, 7)}]
    d = catalyst.digest(items, catalyst.topic_terms(_conn()))
    assert [(t["topic"], t["count"]) for t in d] == [("人形机器人", 3), ("固态电池", 1)]
    assert d[0]["news"][0]["title"] == "固态电池新突破"            # 新的在前


def test_record_and_cached_roundtrip():
    conn = _conn()
    catalyst.record(conn, date(2026, 10, 12), {"window": ["a", "b"], "ok": 3, "failed": [], "topics": [{"topic": "X", "count": 2, "news": []}]})
    assert catalyst.cached(conn, date(2026, 10, 12))["topics"][0]["topic"] == "X"
    assert catalyst.cached(conn, date(2026, 10, 13)) is None


def test_report_lines_show_topics_and_first_board_hits():
    conn = _conn()
    catalyst.record(conn, date(2026, 10, 12), {"window": ["2026-10-09 15:00", "2026-10-12 08:50"], "ok": 90, "failed": ["x"],
                                              "topics": [{"topic": "人形机器人", "count": 3,
                                                          "news": [{"title": "量产提速", "source": "36氪", "link": "l"}]},
                                                         {"topic": "固态电池", "count": 1, "news": []}]})
    conn.execute("INSERT INTO board_events(date, code, name, concept) VALUES ('2026-10-12', '000001', '甲', '人形机器人')")
    conn.commit()
    text = "\n".join(catalyst.report_lines(conn, date(2026, 10, 12)))
    assert "## 隔夜消息催化" in text and "| 人形机器人 | 3 | 1 |" in text and "量产提速" in text
    assert "| 固态电池 | 1 | 0 |" in text and "90 个源" in text


def test_report_lines_empty_when_not_recorded():
    assert catalyst.report_lines(_conn(), date(2026, 10, 12)) == []


# ---- 人气榜 / 龙虎榜 / 资金流 ----

def test_market_extra_snapshot_and_report(monkeypatch):
    conn = db.connect(":memory:")
    monkeypatch.setattr(market_extra, "FETCHERS", {
        "em_hot": lambda d: [{"rank": 1, "code": "600418"}, {"rank": 2, "code": "600825"}],
        "ths_hot": lambda d: [{"rank": 1, "code": "600825", "name": "新华传媒", "pct": 10.0,
                               "concepts": ["上海国企"], "tag": "9天9板"}],
        "concept_flow": lambda d: [{"concept": "同花顺漂亮100", "pct": 0.9, "net": 92.4, "leader": "x"},
                                   {"concept": "AI视频", "pct": 5.48, "net": 23.42, "leader": "某某"},
                                   {"concept": "白酒", "pct": -1.0, "net": -10.0, "leader": "x"}],
        "lhb": lambda d: (_ for _ in ()).throw(RuntimeError("down")),
    })
    got = market_extra.snapshot(conn, date(2026, 10, 9))
    assert got == {"em_hot": 2, "ths_hot": 1, "concept_flow": 3, "lhb": None}
    text = "\n".join(market_extra.report_lines(conn, date(2026, 10, 9), {"600418": "甲"}))
    assert "## 人气与资金" in text and "新华传媒" in text and "9天9板" in text and "AI视频" in text and "甲" in text
    assert "600825 新华传媒" in text                              # 人气榜只有代码，名字借同花顺热榜的
    assert "白酒" in text                                         # 净流出也列
    assert "漂亮100" not in text                                  # 指数、标签类不是题材


def test_fetch_all_stops_at_deadline_and_counts_unfinished_as_failed():
    """bug B：总时限到了就收手，没回来的源算失败，不让盘前抓新闻拖住交易。"""
    import threading
    gate = threading.Event()

    def get(url, timeout=None, headers=None):
        if url == "slow":
            gate.wait(5)
        return Resp(RSS)
    t0 = datetime.now()
    res = rss_news.fetch_all([{"name": "快", "url": "fast"}, {"name": "慢", "url": "slow"}],
                             start=datetime(2026, 10, 9, 15, 0), end=datetime(2026, 10, 10, 9, 0), get=get, budget=0.5)
    gate.set()
    assert (datetime.now() - t0).total_seconds() < 3
    assert res["ok"] == 1 and res["failed"] == ["慢"]


def test_market_extra_lhb_uses_previous_day_and_dedupes(monkeypatch):
    """bug C：收盘后当天龙虎榜还没出，报告里放上一交易日的（傍晚已公布）；同一只票多条上榜理由只列一次。"""
    conn = db.connect(":memory:")
    asked = []
    monkeypatch.setattr(market_extra, "FETCHERS", {
        "em_hot": lambda d: [], "ths_hot": lambda d: [], "concept_flow": lambda d: [],
        "lhb": lambda d: asked.append(d) or [{"code": "1", "name": "甲", "reason": "日涨幅偏离", "pct": 10.0, "net": 500.0},
                                              {"code": "1", "name": "甲", "reason": "三日涨幅", "pct": 10.0, "net": 800.0},
                                              {"code": "2", "name": "乙", "reason": "x", "pct": 5.0, "net": 100.0}]})
    market_extra.snapshot(conn, date(2026, 10, 12), lhb_day=date(2026, 10, 9))
    assert asked == [date(2026, 10, 9)]
    text = "\n".join(market_extra.report_lines(conn, date(2026, 10, 12), {}, lhb_day=date(2026, 10, 9)))
    assert "上一交易日（10-09）龙虎榜" in text and text.count("甲") == 1 and "+800" in text


def test_digest_merges_topics_hit_by_exactly_the_same_news():
    """「光伏 / 光伏概念」「华为汽车 / 华为海思 / 华为鸿蒙」匹配到的是同一批新闻，合并成一行；首板数按合并的几个题材加总。"""
    items = [{"title": "光伏装机创新高", "summary": "", "source": "a", "link": "l1", "published": datetime(2026, 10, 9, 20)},
             {"title": "光伏组件涨价", "summary": "", "source": "b", "link": "l2", "published": datetime(2026, 10, 9, 21)}]
    terms = {"光伏": {"光伏"}, "光伏概念": {"光伏"}, "储能": {"储能"}}
    d = catalyst.digest(items, terms)
    assert [(t["topic"], t["topics"], t["count"]) for t in d] == [("光伏 / 光伏概念", ["光伏", "光伏概念"], 2)]
    conn = _conn()
    catalyst.record(conn, date(2026, 10, 12), {"window": ["a", "b"], "ok": 1, "failed": [], "news": 2, "topics": d})
    conn.execute("INSERT INTO board_events(date, code, name, concept) VALUES ('2026-10-12', '1', '甲', '光伏概念')")
    conn.execute("INSERT INTO board_events(date, code, name, concept) VALUES ('2026-10-12', '2', '乙', '光伏')")
    conn.commit()
    assert "| 光伏 / 光伏概念 | 2 | 2 |" in "\n".join(catalyst.report_lines(conn, date(2026, 10, 12)))
