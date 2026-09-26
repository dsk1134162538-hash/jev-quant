import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import akshare as ak
import pandas as pd

from jevq.config import MIN_LISTED_TRADING_DAYS
from jevq.data.timeout import call_with_timeout

MAIN_BOARD = ("60", "00")
NOTICE_ID = re.compile(r"(AN\d+)")
AK_TIMEOUT = 20.0


def _ak(fn, *args, **kwargs):
    return call_with_timeout(fn, *args, timeout=AK_TIMEOUT, **kwargs)


@dataclass(frozen=True)
class Notice:
    id: str
    code: str
    title: str
    type: str
    date: str


@dataclass(frozen=True)
class News:
    title: str
    content: str
    published: datetime


def trade_days() -> list[date]:
    df = _ak(ak.tool_trade_date_hist_sina)
    return sorted(pd.Timestamp(d).date() for d in df["trade_date"])


def fetch_daily_sina(code: str, today: date | None = None, days: int = 60) -> list[tuple[str, float, float]]:
    """新浪日 K（不复权，ak.stock_zh_a_daily）最近 days 个自然日：[(date_str, close, volume_手)]。
    新浪的 volume 单位是股，这里 /100 换成手，与 Quote.volume、closes.volume 同口径。
    给 closes.backfill_closes 当 fetch_kline 用（腾讯 K 线接口被 WAF 封了）。"""
    today = today or date.today()
    sym = ("sh" if code.startswith("6") else "sz") + code
    df = _ak(ak.stock_zh_a_daily, symbol=sym, start_date=(today - timedelta(days=days)).strftime("%Y%m%d"),
             end_date=today.strftime("%Y%m%d"), adjust="")
    return [(pd.Timestamp(r["date"]).date().isoformat(), float(r["close"]), float(r["volume"]) / 100)
            for _, r in df.iterrows()]


def filter_universe(rows: Iterable[tuple[str, str, date]], today: date,
                    days: list[date]) -> dict[str, str]:
    out = {}
    for code, name, listed in rows:
        if not code.startswith(MAIN_BOARD) or "ST" in name.upper():
            continue
        if bisect_right(days, today) - bisect_left(days, listed) < MIN_LISTED_TRADING_DAYS:
            continue
        out[code] = name
    return out


def load_universe(today: date, days: list[date]) -> dict[str, str]:
    sh = _ak(ak.stock_info_sh_name_code, symbol="主板A股")
    sz = _ak(ak.stock_info_sz_name_code, symbol="A股列表")
    sz = sz[sz["板块"] == "主板"]
    rows = [(str(r["证券代码"]).zfill(6), str(r["证券简称"]), pd.Timestamp(r["上市日期"]).date())
            for _, r in sh.iterrows()]
    rows += [(str(r["A股代码"]).zfill(6), str(r["A股简称"]).replace(" ", ""),
              pd.Timestamp(r["A股上市日期"]).date()) for _, r in sz.iterrows()]
    return filter_universe(rows, today, days)


def notices_from_frame(df: pd.DataFrame) -> list[Notice]:
    out = []
    for _, r in df.iterrows():
        code = str(r["代码"]).zfill(6)
        if not code.startswith(MAIN_BOARD):
            continue
        title = str(r["公告标题"])
        m = NOTICE_ID.search(str(r["网址"]))
        nid = m.group(1) if m else f"{code}:{title}"
        out.append(Notice(nid, code, title, str(r["公告类型"]), str(r["公告日期"])[:10]))
    return out


def fetch_notices_for_date(d: date) -> list[Notice]:
    return notices_from_frame(_ak(ak.stock_notice_report, symbol="全部", date=d.strftime("%Y%m%d")))


def fetch_news(code: str) -> list[News]:
    df = _ak(ak.stock_news_em, symbol=code)
    return [News(str(r["新闻标题"]), str(r["新闻内容"]),
                 pd.Timestamp(r["发布时间"]).to_pydatetime()) for _, r in df.iterrows()]


def summarize_pools(zt: int, dt: int, zb: int, max_board: int) -> dict:
    rate = round(zb / (zt + zb) * 100, 1) if zt + zb else 0.0
    return {"涨停家数": zt, "跌停家数": dt, "炸板率%": rate, "最高连板": max_board}


def fetch_zt_pool(d: date) -> pd.DataFrame:
    """涨停池（含首次封板时间、连板数等列），用于收盘后的封板时段统计。"""
    return _ak(ak.stock_zt_pool_em, date=d.strftime("%Y%m%d"))


def fetch_pool_stats(d: date) -> dict:
    ds = d.strftime("%Y%m%d")
    zt = _ak(ak.stock_zt_pool_em, date=ds)
    dt = _ak(ak.stock_zt_pool_dtgc_em, date=ds)
    zb = _ak(ak.stock_zt_pool_zbgc_em, date=ds)
    max_board = int(zt["连板数"].max()) if len(zt) else 0
    return summarize_pools(len(zt), len(dt), len(zb), max_board)
