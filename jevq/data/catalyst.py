"""隔夜消息催化（dj 2026-10-10 要：首板很多是消息驱动，前一晚出了产业新闻、第二天题材起来）。

上一交易日 15:00 → 今天盘前的 RSS 新闻（jevq.data.rss_news），按题材名匹配：
题材 = 我们的概念板块（concepts 表）+ 自建板块（名字和别名）；英文源靠 config.CATALYST_ALIASES 的关键词。
数出每个题材几条新闻，存 day_cache（kind='catalyst'），写进复盘报告，顺带对一下「有催化的题材当天出了几个首板」。
**只记录，不参与交易**——攒几周看有没有用再说。
"""
import json
import re
from datetime import date, datetime

from jevq import config

KIND = "catalyst"
# 东财概念里不是题材的（指数成分、资金标签、财务状态），不拿来匹配新闻
_NOT_TOPIC = re.compile(r"融资融券|深股通|沪股通|标准普尔|标普|富时|MSCI|重仓|持股|预盈|预增|预亏|预减|转债|昨日|次新|"
                        r"上证|中证|深成|深证|HS300|央视|养老金|茅指数|宁组合|破净|低价|高送转|股权激励|举牌|百元股|"
                        r"AH股|B股|GDR|ST|参股|创业板综|IPO受益|彭博|核心资产|微盘|大盘|中盘|小盘|长期破净|超级品牌|"
                        r"同花顺|漂亮|出海50|高股息|国企改革")
_SUFFIX = re.compile(r"(概念|板块|题材)$")


def _norm(name: str) -> str:
    return _SUFFIX.sub("", re.sub(r"\s+", "", name or ""))


def _curated() -> set[str]:
    return {w.lower() for k, v in config.CATALYST_ALIASES.items() for w in (k, *v)}


def _term_ok(word: str, curated: set[str]) -> bool:
    """拿来匹配新闻的词：英文至少 3 个字母（「AI」「5G」误伤太多）；中文两个字的只用关键词表里列过的
    （「光伏」「算力」可以，「电商」「汽车」「黄金」太泛——黄金周、以军空袭里的汽车都会算进来）。"""
    if len(word) < 2 or _NOT_TOPIC.search(word):
        return False
    if word.isascii():
        return len(word) >= 3
    return len(word) >= 3 or word.lower() in curated


def topic_terms(conn) -> dict[str, set[str]]:
    """题材名 → 匹配用的词（都转小写）；一个能用的词都没有的题材不出现。"""
    out: dict[str, set[str]] = {}
    curated = _curated()

    def add(topic: str, words):
        n = _norm(topic)
        if len(n) < 2 or _NOT_TOPIC.search(n):
            return
        terms = {w.lower() for w in (n, *words) if w and _term_ok(w, curated)}
        for key, aliases in config.CATALYST_ALIASES.items():
            if key in n:
                terms.update(a.lower() for a in aliases)
        if terms:
            out.setdefault(topic, set()).update(terms)
    for (concept,) in conn.execute("SELECT DISTINCT concept FROM concepts ORDER BY concept"):
        add(concept, ())
    for name, aliases in conn.execute("SELECT name, aliases FROM own_sectors WHERE status != 'rejected'"):
        add(name, json.loads(aliases or "[]"))
    return out


def _hit(term: str, text: str) -> bool:
    if term.isascii():                   # 英文词按字母边界（HBM 能匹配 HBM4，CPO 不匹配 CPOS）
        return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text) is not None
    return term in text


def digest(items: list[dict], terms: dict[str, set[str]]) -> list[dict]:
    """[{topic, count, news: [{title, source, link, published}]（新的在前）}]，按新闻数从多到少。"""
    hits: dict[str, list[dict]] = {}
    for it in items:
        text = f"{it.get('title', '')} {it.get('summary', '')}".lower()
        for topic, words in terms.items():
            if any(_hit(w, text) for w in words):
                hits.setdefault(topic, []).append(it)
    out = []
    for topic, news in hits.items():
        news = sorted(news, key=lambda x: x["published"], reverse=True)
        out.append({"topic": topic, "count": len(news),
                    "news": [{"title": n["title"], "source": n.get("source", ""), "link": n.get("link", ""),
                              "published": n["published"].strftime("%m-%d %H:%M")} for n in news[:5]]})
    return sorted(out, key=lambda t: (-t["count"], t["topic"]))


def record(conn, day: date, data: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO day_cache(date, kind, key, value) VALUES (?, ?, 'digest', ?)",
                 (day.isoformat(), KIND, json.dumps(data, ensure_ascii=False)))
    conn.commit()


def cached(conn, day: date) -> dict | None:
    row = conn.execute("SELECT value FROM day_cache WHERE date=? AND kind=? AND key='digest'",
                       (day.isoformat(), KIND)).fetchone()
    return json.loads(row[0]) if row else None


def run(conn, day: date, start: datetime, end: datetime, fetch=None) -> dict:
    """抓新闻 → 匹配 → 存库，返回摘要（题材数、源成功/失败数）。"""
    from jevq.data import rss_news
    res = (fetch or rss_news.fetch_all)(rss_news.load_sources(), start, end)
    topics = digest(res["items"], topic_terms(conn))
    record(conn, day, {"window": [start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")],
                       "ok": res["ok"], "failed": res["failed"], "news": len(res["items"]), "topics": topics})
    return {"news": len(res["items"]), "topics": len(topics), "ok": res["ok"], "failed": len(res["failed"])}


def report_lines(conn, day: date) -> list[str]:
    data = cached(conn, day)
    if not data:
        return []
    boards = dict(conn.execute("SELECT concept, COUNT(*) FROM board_events WHERE date=? GROUP BY concept",
                               (day.isoformat(),)).fetchall())
    a, b = data.get("window", ["?", "?"])
    lines = ["", "## 隔夜消息催化", "",
             f"{a} → {b} 的新闻，{data.get('ok', 0)} 个源抓到、{len(data.get('failed', []))} 个失败，共 "
             f"{data.get('news', 0)} 条；按题材名匹配。只记录，不参与交易。", ""]
    topics = data.get("topics", [])[:config.CATALYST_REPORT_TOP]
    if not topics:
        return lines + ["没有匹配到题材。"]
    lines += ["| 题材 | 新闻数 | 今日首板 | 代表新闻 |", "|---|---|---|---|"]
    for t in topics:
        news = "；".join(f"{n['title'][:40]}（{n['source']}）" for n in t["news"][:2])
        lines.append(f"| {t['topic']} | {t['count']} | {boards.get(t['topic'], 0)} | {news} |")
    return lines
