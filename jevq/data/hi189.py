"""hi189「股票期货」栏目文章：RSS 抓取 + 当日缓存 + 晚报板块解析 + 点名判定。
设计 docs/specs/2026-10-04-hi189-articles-design.md。任何失败都退化成「没有文章」，不影响交易流程。"""
import html
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from jevq.config import HI189_ENABLED, HI189_FEED_URL, HI189_FETCH_UNTIL, HI189_MIDDAY_JUDGE, HI189_TIMEOUT

KIND = "hi189"
CN_TZ = timezone(timedelta(hours=8))
PUBLISHED_AFTER = time(15, 0)                 # 上一交易日 15:00 之后发布的才算（收盘后的晚报）
PASSWORD_PREFIXES = ("密码保护：", "密码保护:", "Protected:")
_UA = {"User-Agent": "Mozilla/5.0"}
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
MIN_FEED_TEXT = 200                           # RSS 里正文短于这个字数就去文章页取


@dataclass(frozen=True)
class Article:
    title: str
    link: str
    published: datetime                       # 北京时间，无时区
    text: str


@dataclass
class Sector:
    name: str
    limit_up: list[str] = field(default_factory=list)
    strong: list[str] = field(default_factory=list)


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", raw or "")
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr|section|article)>", "\n", raw)
    raw = html.unescape(re.sub(r"<[^>]+>", "", raw))
    lines = (re.sub(r"[ \t　\xa0]+", " ", ln).strip() for ln in raw.splitlines())
    return "\n".join(ln for ln in lines if ln)


def parse_feed(xml_text: str) -> list[Article]:
    out = []
    for item in ET.fromstring(xml_text).iter("item"):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        if not (title and link and pub):
            continue
        try:
            dt = parsedate_to_datetime(pub.strip())
        except (TypeError, ValueError):
            continue
        published = (dt.astimezone(CN_TZ) if dt.tzinfo else dt).replace(tzinfo=None)
        body = item.findtext(_CONTENT) or item.findtext("description") or ""
        out.append(Article(title, link, published, html_to_text(body)))
    return out


def fetch_feed(session=None) -> list[Article]:
    try:
        r = (session or requests).get(HI189_FEED_URL, timeout=HI189_TIMEOUT, headers=_UA)
        r.raise_for_status()
        return parse_feed(r.text)
    except Exception as e:
        print(f"[hi189] 抓取 RSS 失败: {e!r}")
        return []


def fetch_page_text(link: str, session=None) -> str:
    try:
        r = (session or requests).get(link, timeout=HI189_TIMEOUT, headers=_UA)
        r.raise_for_status()
        page = r.text
        m = re.search(r"(?is)<article\b.*?</article>", page)
        return html_to_text(m.group(0) if m else page)
    except Exception as e:
        print(f"[hi189] 抓取文章页失败 {link}: {e!r}")
        return ""


def eligible(articles: list[Article], prev_day: date, now: datetime) -> list[Article]:
    """上一交易日 15:00 之后、现在之前发布的公开文章；密码保护的一律跳过（不绕过）。"""
    start = datetime.combine(prev_day, PUBLISHED_AFTER)
    return [a for a in articles
            if start < a.published <= now and not a.title.startswith(PASSWORD_PREFIXES)]


_KIND = re.compile(r"[（(]\s*([早中晚])\s*[)）]")


def kind_of(title: str) -> str:
    """标题里的「(早)/(中)/(晚)」；周末版、复盘/操作计划这类没标的返回空串。"""
    m = _KIND.search(title or "")
    return m.group(1) if m else ""


def in_judgement(article: dict) -> bool:
    """这篇文章是否交给 Jev 判情绪：中报在 HI189_MIDDAY_JUDGE 关着时只记录，其余都参与。"""
    return HI189_MIDDAY_JUDGE or kind_of(article.get("title", "")) != "中"


def for_judge(articles: list[dict]) -> list[dict]:
    return [a for a in articles if in_judgement(a)]


def cached(conn: sqlite3.Connection, day: date) -> list[dict]:
    rows = conn.execute("SELECT value FROM day_cache WHERE date=? AND kind=?", (day.isoformat(), KIND)).fetchall()
    return sorted((json.loads(v) for (v,) in rows), key=lambda d: d["published"])


def refresh(conn: sqlite3.Connection, day: date, prev_day: date, now: datetime,
            fetch: Callable[[], list[Article]] = fetch_feed,
            fetch_page: Callable[[str], str] = fetch_page_text) -> list[dict]:
    """返回当天已缓存的文章（{title, link, published, text}，按发布时间升序）。
    HI189_FETCH_UNTIL 之前才联网查新文章，之后只读缓存；总开关关掉 = 永远空。"""
    if not HI189_ENABLED:
        return []
    if now.time() <= HI189_FETCH_UNTIL:
        have = {d["link"] for d in cached(conn, day)}
        for a in eligible(fetch(), prev_day, now):
            if a.link in have:
                continue
            text = a.text if len(a.text) >= MIN_FEED_TEXT else (fetch_page(a.link) or a.text)
            if not text:
                continue
            conn.execute("INSERT OR REPLACE INTO day_cache VALUES (?, ?, ?, ?)",
                         (day.isoformat(), KIND, a.link, json.dumps(
                             {"title": a.title, "link": a.link, "published": a.published.isoformat(timespec="minutes"),
                              "text": text}, ensure_ascii=False)))
        conn.commit()
    return cached(conn, day)


# ---- 晚报解析与点名 ----
# 真实版式（2026-09-30 晚报 https://hi189.com/6999.html）：
#   板块名（单独一行）/ 一到几行说明文字 / 涨停板：甲、乙 / 其他强势股：丙、丁
# 个别板块只有「强势股：…。」没有涨停板；说明文字可能被硬换行拆成两行。板块名 = 往回数第一个「像名字」的短行。
_LIMIT_UP = re.compile(r"^(?:涨停板|涨停股)\s*[:：]\s*(.*)$")
_STRONG = re.compile(r"^(?:其他强势股|其它强势股|强势股)\s*[:：]\s*(.*)$")
_LEAD_NO = re.compile(r"^(?:\d+|[一二三四五六七八九十]+)\s*[.、)）]\s*")
_PAREN = re.compile(r"[（(][^）)]*[）)]")
_NOT_NAME = re.compile(r"[。，、：:；;（）()《》“”%％]")
NAME_MAX_CHARS = 12
NAME_LOOKBACK = 8


def _clean_sector(s: str) -> str:
    return _LEAD_NO.sub("", s).strip(" 【】[]★☆●■◆▶:：-—")


def _stocks(s: str) -> list[str]:
    s = _PAREN.sub("", s).strip().rstrip("。.")
    return [x for x in re.split(r"[、，,；;\s]+", s) if x]


def _is_name(line: str) -> bool:
    return (0 < len(line) <= NAME_MAX_CHARS and not _NOT_NAME.search(line) and not line.startswith(("【", "[", "①", "②", "③"))
            and not _LEAD_NO.match(line))


def _sector_name(lines: list[str], idx: int) -> str:
    for k in range(idx - 1, max(idx - 1 - NAME_LOOKBACK, -1), -1):
        if _LIMIT_UP.match(lines[k]) or _STRONG.match(lines[k]):
            break
        if _is_name(lines[k]):
            return _clean_sector(lines[k])
    return ""


def parse_sectors(text: str) -> list[Sector]:
    """晚报板块：见上面的版式说明。认不出结构 → 空列表。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    sectors: list[Sector] = []
    last = None
    for i, line in enumerate(lines):
        if m := _LIMIT_UP.match(line):
            sectors.append(Sector(_sector_name(lines, i), _stocks(m.group(1))))
            last = "lu"
        elif m := _STRONG.match(line):
            if last == "lu" and sectors and not sectors[-1].strong:
                sectors[-1].strong = _stocks(m.group(1))
            else:
                sectors.append(Sector(_sector_name(lines, i), [], _stocks(m.group(1))))
            last = "st"
        else:
            last = None
    return [s for s in sectors if s.name]


# 早报版式（2026-09-29 早报 https://hi189.com/6988.html）：「一、昨日热点」后面每行「板块：个股、个股」，
# 到下一个带序号的小标题（「二、…」）为止。后面每条要闻末尾也有「板块：个股」，那是受益标的，这里不读。
_MORNING_HEAD = re.compile(r"^(?:[一二三四五六七八九十]+[、.]\s*)?昨日热点\s*$")
_HEADING = re.compile(r"^[一二三四五六七八九十]+[、.]")
_TOPIC_LINE = re.compile(r"^([^：:]+)[:：](.+)$")
MORNING_HOT_MAX_LINES = 30


def parse_morning_hot(text: str | None) -> list[Sector]:
    """早报「昨日热点」：只记点名（strong），不假定是涨停。没有这一节 → 空列表。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    head = next((i for i, ln in enumerate(lines) if _MORNING_HEAD.match(ln)), None)
    if head is None:
        return []
    out: list[Sector] = []
    for ln in lines[head + 1:head + 1 + MORNING_HOT_MAX_LINES]:
        m = _TOPIC_LINE.match(ln)
        if _HEADING.match(ln) or not m or not _is_name(m.group(1).strip()):
            break
        stocks = _stocks(m.group(2))
        if name := _clean_sector(m.group(1)):
            out.append(Sector(name, [], stocks))
    return out


def sectors_of(articles: list[dict]) -> list[Sector]:
    return [s for a in articles for s in (*parse_sectors(a.get("text", "")), *parse_morning_hot(a.get("text", "")))]


def _norm(name: str) -> str:
    return re.sub(r"(概念|板块|题材)$", "", re.sub(r"\s+", "", name))


def article_hot(concept: str, members: list[str], names: dict[str, str], sectors: list[Sector]) -> str:
    """文章是否点名了这个题材：板块名与题材名互相包含，或盘中强势成员在文章个股列表里。
    返回命中原因（空串 = 没点名）。纯代码比对，不经 Jev。"""
    c = _norm(concept)
    for s in sectors:
        n = _norm(s.name)
        if len(c) >= 2 and len(n) >= 2 and (c in n or n in c):
            return f"板块：{s.name}"
    mentioned = {x for s in sectors for x in (*s.limit_up, *s.strong)}
    for code in members:
        if (nm := names.get(code)) and nm in mentioned:
            return f"个股：{nm}"
    return ""
