"""RSS 新闻抓取：jevq/data/rss_sources.json 的 106 个源（清单来自 Vibe-Research，MIT）。

解析照 Vibe 的 sources/rss.py（标准库 xml.etree，RSS 2.0 / Atom 都认），时间统一成北京时间、不带时区。
单源失败只记进 failed，不拖垮整体；并发抓，每源有超时。只读网络，不碰数据库。
"""
import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

from jevq import config

SOURCES_PATH = Path(__file__).with_name("rss_sources.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"
CN = timezone(timedelta(hours=8))
_NS = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}


def load_sources() -> list[dict]:
    cfg = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    return [s for s in cfg["sources"] if s.get("type", "rss") == "rss"]


def _text(el, *paths) -> str:
    for p in paths:
        x = el.find(p, _NS)
        if x is not None:
            t = (x.text or "").strip()
            if not t and x.get("href"):
                return x.get("href")
            if t:
                return t
    return ""


def _parse_date(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CN).replace(tzinfo=None)


def _clean(s: str) -> str:
    # 标题摘要常是 HTML 文本：XML 只解一层实体，里面的 &#8217; 之类还要再解一次
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", unescape(s or "")))).strip()


def parse_feed(content: bytes, limit: int = 30) -> list[dict]:
    """RSS 2.0 / Atom → [{title, link, published(北京时间 | None), summary}]"""
    root = ET.fromstring(content)
    items = []
    for it in root.iter():
        if it.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title = _clean(_text(it, "title", "atom:title"))
        if not title:
            continue
        items.append({"title": title,
                      "link": _text(it, "link", "atom:link[@rel='alternate']", "atom:link", "guid"),
                      "published": _parse_date(_text(it, "pubDate", "atom:published", "atom:updated", "dc:date")),
                      "summary": _clean(_text(it, "description", "atom:summary", "atom:content"))[:300]})
        if len(items) >= limit:
            break
    return items


def _get(url, timeout=None, headers=None):
    import requests
    return requests.get(url, timeout=timeout, headers=headers)


def fetch_all(sources: list[dict], start: datetime, end: datetime, get=_get,
              timeout: float = config.RSS_TIMEOUT, workers: int = config.RSS_WORKERS,
              budget: float = config.RSS_BUDGET) -> dict:
    """抓所有源，只留发布时间在 [start, end] 之间的（没写时间的丢掉——分不清是不是旧闻）。
    budget 秒是总时限：到点还没回来的源算失败、不再等（requests 的 timeout 只管单次读写，慢慢吐字节的源能拖很久）。
    返回 {items（新的在前）, ok, failed: [源名]}。"""
    def one(src):
        r = get(src["url"], timeout=timeout, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"})
        r.raise_for_status()
        return parse_feed(r.content)
    ex = ThreadPoolExecutor(max_workers=workers)
    futs = {ex.submit(one, s): s for s in sources}
    done, _ = wait(futs, timeout=budget)
    ex.shutdown(wait=False, cancel_futures=True)        # 没回来的线程自己超时结束，不在这里等
    items, failed, ok = [], [], 0
    for fut, src in futs.items():                        # 按清单顺序，结果稳定
        if fut not in done or fut.exception() is not None:   # 单源失败不拖垮整体
            failed.append(src["name"])
            continue
        ok += 1
        items += [{**it, "source": src["name"], "industry": src.get("hint", "")} for it in fut.result()
                  if it["published"] and start <= it["published"] <= end]
    items.sort(key=lambda x: x["published"], reverse=True)
    return {"items": items, "ok": ok, "failed": failed}
