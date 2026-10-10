"""涨停相关的两份补充数据（10-10 dj 要接）：

- 昨日涨停今日表现（东财 push2ex getYesterdayZTPool）：昨天涨停的票今天平均涨多少、多少红盘——
  短线赚钱效应最直接的读数，进 Jev 的情绪判断。
- 涨停揭秘（同花顺 data.10jqka limit_up_pool）：每只涨停的原因题材（如「PCB+AI数据中心电源」），
  按题材数涨停家数，≥ ZT_REASON_MIN 的当成「点名」的板块，进扫首板 / 低吸的主线判断。

接口与字段对照 Vibe-Research 的 data-access 脚本（MIT，原出 a-stock-data），这里只取用得到的几列。
都是零鉴权公开接口；失败返回空，不影响原有逻辑。
"""
import re
from collections import Counter
from datetime import date

from jevq import config
from jevq.data.hi189 import Sector

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"
YZT_URL = "https://push2ex.eastmoney.com/getYesterdayZTPool"
YZT_UT = "7eea3edcaed734bea9cbfc24409ed989"
THS_URL = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
THS_FIELDS = "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004"
REASON_PREFIX = "涨停原因·"
_SPLIT = re.compile(r"[+＋、/]+")
# 不是题材的原因词（业绩、分红这类个股事件），不算「点名」
_NOT_THEME = re.compile(r"业绩|年报|季报|预增|扭亏|增长|分红|回购|全产业链|增持|中标")


def _session(session):
    if session is not None:
        return session
    import requests
    return requests


def fetch_yesterday(d: date, session=None) -> list[dict]:
    """d 当天，上一交易日涨停的票：[{code, name, pct(今天涨幅%), boards(昨天是几连板)}]。"""
    r = _session(session).get(YZT_URL, params={"ut": YZT_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 10000,
                                               "sort": "zs:desc", "date": d.strftime("%Y%m%d")},
                              headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"},
                              timeout=config.ZT_EXTRA_TIMEOUT)
    r.raise_for_status()
    pool = (r.json().get("data") or {}).get("pool") or []
    return [{"code": p["c"], "name": p.get("n", ""), "pct": round(float(p["zdp"]), 2), "boards": int(p.get("ylbc") or 1)}
            for p in pool]


def yesterday_stats(rows: list[dict]) -> dict:
    if not rows:
        return {}
    pcts = [r["pct"] for r in rows]
    out = {"昨日涨停今日均涨%": round(sum(pcts) / len(pcts), 2),
           "昨日涨停今日红盘率%": round(sum(p > 0 for p in pcts) / len(pcts) * 100, 1),
           "昨日涨停今日跌超5%家数": sum(p <= -5 for p in pcts),
           "昨日涨停今日再涨停家数": sum(p >= 9.8 for p in pcts)}
    multi = [r["pct"] for r in rows if r["boards"] >= 2]
    if multi:
        out["昨日连板今日均涨%"] = round(sum(multi) / len(multi), 2)
    return out


def fetch_reasons(d: date, session=None) -> list[dict]:
    """d 当天涨停的票和同花顺给的原因题材：[{code, name, reason, high_days}]。"""
    r = _session(session).get(THS_URL, params={"page": 1, "limit": 200, "field": THS_FIELDS, "filter": "HS,GEM2STAR",
                                               "order_field": "330324", "order_type": "0", "date": d.strftime("%Y%m%d")},
                              headers={"User-Agent": UA}, timeout=config.ZT_EXTRA_TIMEOUT)
    r.raise_for_status()
    info = (r.json().get("data") or {}).get("info") or []
    return [{"code": it.get("code"), "name": it.get("name") or "", "reason": it.get("reason_type") or "",
             "high_days": it.get("high_days") or ""} for it in info]


def reason_sectors(rows: list[dict], min_count: int = config.ZT_REASON_MIN) -> list[Sector]:
    """按原因题材数涨停家数；≥ min_count 的题材变成一个「点名」板块（名字带「涨停原因·」前缀，
    文章点名的匹配函数 hi189.article_hot 直接能用）。家数多的在前。"""
    names: dict[str, list[str]] = {}
    for r in rows:
        for topic in dict.fromkeys(t.strip() for t in _SPLIT.split(r.get("reason") or "")):
            if topic and not _NOT_THEME.search(topic):
                names.setdefault(topic, []).append(r["name"])
    count = Counter({t: len(v) for t, v in names.items()})
    return [Sector(REASON_PREFIX + t, list(names[t]), []) for t, n in count.most_common() if n >= min_count]


def safe_yesterday_stats(d: date) -> dict:
    try:
        return yesterday_stats(fetch_yesterday(d))
    except Exception as e:
        print(f"[ztpool] 昨日涨停表现失败: {e!r}")
        return {}


def safe_reason_sectors(d: date) -> list[Sector]:
    try:
        return reason_sectors(fetch_reasons(d))
    except Exception as e:
        print(f"[ztpool] 涨停原因失败: {e!r}")
        return []
