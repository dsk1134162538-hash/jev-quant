from datetime import date, datetime
from pathlib import Path

import pytest

from jevq import db
from jevq.data import hi189
from jevq.data.hi189 import Article

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
    assert len(calls) == 2                                      # 14:40 前每次都查，但同一篇只存一次
    assert conn.execute("SELECT COUNT(*) FROM day_cache WHERE kind='hi189'").fetchone()[0] == 1


def test_refresh_stops_fetching_after_cutoff_but_reads_cache():
    conn = db.connect(":memory:")
    hi189.refresh(conn, TODAY, PREV, NOW, fetch=lambda: [art("晚报", "b", datetime(2026, 9, 29, 19, 0))])

    def boom():
        raise AssertionError("14:40 后不该联网")

    got = hi189.refresh(conn, TODAY, PREV, datetime(2026, 9, 30, 14, 41), fetch=boom)
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


def test_parse_sectors_from_real_evening_report():
    got = hi189.parse_sectors(FIXTURE.read_text(encoding="utf-8"))
    assert [s.name for s in got] == ["创新药", "人形机器人", "固态电池", "汽车", "AI 应用", "乳业", "白酒", "电力",
                                     "房地产", "农业", "分散染料", "玻璃基板", "光伏", "风电设备"]
    pharma = got[0]
    assert pharma.limit_up[0] == "康希诺" and len(pharma.limit_up) == 8 and pharma.strong[-1] == "药石科技"
    assert got[4].limit_up == ["新华传媒", "龙版传媒", "亚联发展", "德才股份"]
    assert got[5].name == "乳业" and got[5].limit_up == ["阳光乳业", "均瑶健康"]    # 说明文字被硬换行拆成两行
    assert got[8].limit_up == ["深物业A", "陆家嘴", "中天服务"]
    wind = got[-1]                                                            # 只有「强势股：…。」
    assert wind.limit_up == [] and wind.strong[0] == "大金重工" and wind.strong[-1] == "振宏股份"
    assert hi189.parse_sectors("没有任何结构的文字") == []


def test_article_hot_on_real_report():
    sectors = hi189.parse_sectors(FIXTURE.read_text(encoding="utf-8"))
    assert hi189.article_hot("固态电池", ["1"], {}, sectors) == "板块：固态电池"
    assert hi189.article_hot("人形机器人概念", ["1"], {}, sectors) == "板块：人形机器人"   # 归一化去掉「概念」
    assert hi189.article_hot("新能源汽车", ["1"], {}, sectors) == "板块：汽车"           # 板块名被题材名包含
    assert hi189.article_hot("芯片", ["1", "2"], {"1": "无关", "2": "雷曼光电"}, sectors) == "个股：雷曼光电"
    assert hi189.article_hot("芯片", ["1"], {"1": "无关"}, sectors) == ""


def test_sectors_of_merges_articles():
    text = FIXTURE.read_text(encoding="utf-8")
    assert len(hi189.sectors_of([{"text": text}, {"text": ""}, {}])) == 14
    assert hi189.sectors_of([]) == []


MORNING = Path(__file__).parent / "fixtures" / "hi189_morning.txt"


@pytest.mark.parametrize("title,kind", [
    ("2026/09/29（早）机构研究总结和市场消息汇总解读", "早"),
    ("2026/09/28-(早) 机构研究总结和市场消息汇总解读", "早"),
    ("2026/09/23-(中) 机构研究总结和市场消息汇总解读", "中"),
    ("2026/09/30(晚) 机构研究总结和市场消息汇总解读", "晚"),
    ("2026/09/20 机构研究总结和市场消息汇总解读", ""),           # 周末版没有标早/中/晚
    ("9.28 深度复盘/操作计划", ""),
])
def test_kind_of_reads_morning_midday_evening_from_title(title, kind):
    assert hi189.kind_of(title) == kind


def test_parse_morning_hot_from_real_morning_report():
    got = hi189.parse_morning_hot(MORNING.read_text(encoding="utf-8"))
    assert [(s.name, s.strong) for s in got] == [
        ("并购", ["新华传媒"]), ("机器人", ["襄阳轴承"]), ("AI影视", ["博纳影业"]),
        ("AI安全", ["中新赛克"]), ("汽车", ["众泰汽车"])]
    assert all(s.limit_up == [] for s in got)        # 只记「点名」，不假定是涨停
    # 后面每条要闻末尾的「板块：个股」（AI安全：启明信息…、固态电池：…）不在「昨日热点」里，不读
    assert "固态电池" not in [s.name for s in got] and "启明信息" not in [x for s in got for x in s.strong]


def test_parse_morning_hot_handles_multi_stock_and_other_titles():
    text = "【盘前热点事件】\n一、昨日热点\n传媒：新华传媒、新华文轩\n福建：国统股份、平潭发展\n二、上交所三季报预约\n机器人：襄阳轴承"
    got = hi189.parse_morning_hot(text)
    assert [(s.name, s.strong) for s in got] == [("传媒", ["新华传媒", "新华文轩"]), ("福建", ["国统股份", "平潭发展"])]
    assert hi189.parse_morning_hot("盘前热点事件\n昨日热点\n机器人：甲")[0].name == "机器人"   # 没有序号也认
    assert hi189.parse_morning_hot("没有昨日热点这一节\n机器人：甲") == []
    assert hi189.parse_morning_hot("") == [] and hi189.parse_morning_hot(None) == []


def test_parse_morning_hot_stops_at_first_non_topic_line():
    text = "一、昨日热点\n机器人：甲\n这是一段说明文字，不是板块行\n汽车：乙"
    assert [s.name for s in hi189.parse_morning_hot(text)] == ["机器人"]


def test_sectors_of_includes_morning_hot_and_evening_together():
    evening = FIXTURE.read_text(encoding="utf-8")
    got = hi189.sectors_of([{"text": evening}, {"text": MORNING.read_text(encoding="utf-8")}])
    assert len(got) == 14 + 5
    assert hi189.article_hot("AI影视概念", ["1"], {}, got) == "板块：AI影视"


def test_midday_report_published_this_afternoon_is_eligible_and_cached_until_cutoff():
    conn = db.connect(":memory:")
    mid = art("2026/09/30-(中) 机构研究总结和市场消息汇总解读", "m", datetime(2026, 9, 30, 13, 20))
    got = hi189.refresh(conn, TODAY, PREV, datetime(2026, 9, 30, 14, 0), fetch=lambda: [mid])
    assert [d["link"] for d in got] == ["m"]
    # 第二天它早于「上一交易日 15:00」，不会再被当成新文章
    assert hi189.eligible([mid], date(2026, 9, 30), datetime(2026, 10, 8, 8, 50)) == []


def test_midday_report_not_judged_while_switch_off_but_morning_and_evening_are(monkeypatch):
    arts = [{"title": "2026/09/30(晚) x"}, {"title": "2026/09/30-(中) x"}, {"title": "2026/09/30-(早) x"}, {"title": "周末版"}]
    monkeypatch.setattr(hi189, "HI189_MIDDAY_JUDGE", False)
    assert [a["title"][-5:] for a in hi189.for_judge(arts)] == ["(晚) x", "(早) x", "周末版"]
    assert hi189.in_judgement(arts[1]) is False
    monkeypatch.setattr(hi189, "HI189_MIDDAY_JUDGE", True)
    assert hi189.for_judge(arts) == arts and hi189.in_judgement(arts[1]) is True


def test_fetch_feed_and_page_report_captcha_challenge_without_bypass(capsys):
    class Resp:
        status_code = 202
        text = '<meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/?r=%2Ffeed">'
        headers = {"sg-captcha": "challenge"}

        def raise_for_status(self):
            pass

    class Sess:
        calls = 0

        def get(self, *a, **k):
            Sess.calls += 1
            return Resp()

    assert hi189.fetch_feed(Sess()) == [] and hi189.fetch_page_text("https://hi189.com/1.html", Sess()) == ""
    assert Sess.calls == 2 and "验证码" in capsys.readouterr().out      # 各只请求一次，不重试、不绕过
