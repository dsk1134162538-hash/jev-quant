"""7x24 实时快讯（财联社电报 + 东方财富全球快讯，akshare 取），给 Jev 判断市场情绪/情绪拐点作参考。
只读、只取标题；任何一个源失败就用另一个，都失败返回空列表，不影响交易。"""
from collections.abc import Callable
from datetime import datetime, timedelta
from time import monotonic

from jevq.data.timeout import call_with_timeout

CACHE_SECONDS = 60
FETCH_TIMEOUT = 8.0


def _cls() -> list[tuple[datetime, str]]:
    import akshare as ak
    df = ak.stock_info_global_cls(symbol="全部")
    return [(datetime.combine(r["发布日期"], r["发布时间"]), str(r["标题"]).strip()) for r in df.to_dict("records")]


def _em() -> list[tuple[datetime, str]]:
    import akshare as ak
    df = ak.stock_info_global_em()
    return [(datetime.strptime(str(r["发布时间"])[:19], "%Y-%m-%d %H:%M:%S"), str(r["标题"]).strip())
            for r in df.to_dict("records")]


SOURCES: dict[str, Callable[[], list[tuple[datetime, str]]]] = {"财联社": _cls, "东方财富": _em}


class Flash:
    """快讯缓存：同一分钟内多处要用只联网一次。"""

    def __init__(self, sources: dict | None = None, log: Callable[[str], None] = print, clock=monotonic):
        self.sources = SOURCES if sources is None else sources
        self.log, self.clock = log, clock
        self._items: list[tuple[datetime, str, str]] = []
        self._at: float | None = None

    def recent(self, now: datetime, minutes: int = 120, limit: int = 30) -> list[str]:
        """最近 minutes 分钟的快讯标题，新的在前，去重，形如「13:05 财联社｜标题」。"""
        if self._at is None or self.clock() - self._at >= CACHE_SECONDS:
            self._refresh()
        since = now - timedelta(minutes=minutes)
        seen, out = set(), []
        for ts, src, title in sorted(self._items, key=lambda x: x[0], reverse=True):
            if not title or title in seen or ts < since or ts > now + timedelta(minutes=1):
                continue
            seen.add(title)
            out.append(f"{ts:%H:%M} {src}｜{title}")
            if len(out) >= limit:
                break
        return out

    def items(self) -> list[tuple[datetime, str, str]]:
        """缓存里的原始快讯 [(时间, 来源, 标题)]（过期先刷新）；快讯存档用。"""
        if self._at is None or self.clock() - self._at >= CACHE_SECONDS:
            self._refresh()
        return list(self._items)

    def _refresh(self) -> None:
        items = []
        for name, fetch in self.sources.items():
            try:
                items += [(ts, name, title) for ts, title in call_with_timeout(fetch, timeout=FETCH_TIMEOUT)]
            except Exception as e:      # noqa: BLE001
                self.log(f"[7x24] {name} 快讯获取失败：{e!r}")
        self._items, self._at = items, self.clock()
