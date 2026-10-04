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

from jevq.config import HI189_ENABLED, HI189_FEED_URL, HI189_FETCH_UNTIL, HI189_TIMEOUT

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
_LIMIT_UP = re.compile(r"^(?:涨停板|涨停股|涨停)\s*[:：]\s*(.*)$")
_STRONG = re.compile(r"^(?:其他强势股|其它强势股|强势股)\s*[:：]\s*(.*)$")
_LEAD_NO = re.compile(r"^(?:\d+|[一二三四五六七八九十]+)\s*[.、)）]\s*")
_PAREN = re.compile(r"[（(][^）)]*[）)]")


def _clean_sector(s: str) -> str:
    return _LEAD_NO.sub("", s).strip(" 【】[]★☆●■◆▶:：-—")


def _stocks(s: str) -> list[str]:
    s = _PAREN.sub("", s)
    return [x for x in re.split(r"[、，,；;\s]+", s) if x]


def parse_sectors(text: str) -> list[Sector]:
    """晚报格式：「板块名 / 涨停板：… / 其他强势股：…」。认不出结构 → 空列表。"""
    sectors: list[Sector] = []
    prev_plain, last = "", None
    for line in (ln.strip() for ln in (text or "").splitlines()):
        if not line:
            continue
        if m := _LIMIT_UP.match(line):
            sectors.append(Sector(_clean_sector(prev_plain), _stocks(m.group(1))))
            last = "lu"
        elif m := _STRONG.match(line):
            if last == "lu" and sectors and not sectors[-1].strong:
                sectors[-1].strong = _stocks(m.group(1))
            else:
                sectors.append(Sector(_clean_sector(prev_plain), [], _stocks(m.group(1))))
            last = "st"
        else:
            prev_plain, last = line, None
    return [s for s in sectors if s.name]


def sectors_of(articles: list[dict]) -> list[Sector]:
    return [s for a in articles for s in parse_sectors(a.get("text", ""))]


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
