"""超跌反弹看板块（用户 2026-10-15）：个股 → 所属板块（新浪概念 + 新浪行业 + 自建板块）、板块 5 日涨幅、
板块黑名单（白酒等酒类不买）。

板块 5 日涨幅 = 成员（在股票池、有 5 日涨幅的）5 日涨幅中位数，成员不足 SECTOR_MIN_MEMBERS 的板块不算。
一只票属于多个板块时取「跌得最多」的那个（回测口径，docs/HANDOFF.md 第 29 项）。
板块跌幅现在只用来给 Claude 看、写进留痕；「个股自己跌」不拦（config.REBOUND_LONE_FILTER，回测不稳）。
新浪行业 7 天刷新一次，存 industries 表；拿不到就用旧的，旧的也没有就只用概念。"""
import sqlite3
import statistics
import threading
import time as _time
from datetime import date, datetime

import akshare as ak

from jevq import config
from jevq.data.timeout import call_with_timeout

# 概念/行业里不是题材的（交易制度、指数成分、机构持仓、财务事件、股本结构），不当板块
NOT_SECTOR = {"融资融券", "转融券标的", "深股通", "沪股通", "标普道琼斯A股", "MSCI概念", "富时罗素概念股",
              "参股金融", "金融参股", "专精特新", "股权激励", "业绩预升", "业绩预降", "重组概念", "整体上市", "分拆上市",
              "资产注入", "本月解禁", "送转潜力", "摘帽概念", "ST板块", "准ST股", "未股改", "含H股", "含B股", "含GDR",
              "社保重仓", "基金重仓", "券商重仓", "信托重仓", "保险重仓", "QFII重仓", "科创50", "央企50", "超大盘",
              "外资背景", "高校背景", "三板精选", "股期概念"}
NOT_INDUSTRY = {"次新股", "其它行业", "综合行业", "开发区"}
INDUSTRY_PREFIX = "行业·"
OWN_PREFIX = "自建·"


def fetch_industry_list() -> list[tuple[str, str]]:
    df = call_with_timeout(ak.stock_sector_spot, indicator="新浪行业")
    return [(str(r["label"]), str(r["板块"])) for _, r in df.iterrows()]


def fetch_industry_members(label: str) -> list[str]:
    df = call_with_timeout(ak.stock_sector_detail, sector=label)
    return [str(c).zfill(6) for c in df["code"]]


def needs_refresh(conn: sqlite3.Connection, today: date, max_age_days: int = 7) -> bool:
    row = conn.execute("SELECT MAX(updated) FROM industries").fetchone()
    return not row or not row[0] or (today - date.fromisoformat(row[0])).days >= max_age_days


def refresh_industries(conn: sqlite3.Connection, today: date, fetch_list=fetch_industry_list,
                       fetch_members=fetch_industry_members, pause: float = 0.3,
                       deadline: datetime | None = None, stop: threading.Event | None = None, log=print) -> int:
    """同 concepts.refresh_concepts：到点/收到停止就中断，凑不够一半保留旧数据。返回写入条数。"""
    try:
        boards = fetch_list()
    except Exception as e:
        log(f"新浪行业列表获取失败：{e!r}")
        return 0
    rows, ok = [], 0
    for label, name in boards:
        if (deadline is not None and datetime.now() >= deadline) or (stop is not None and stop.is_set()):
            log("新浪行业刷新中断（到时间或收到停止）")
            break
        try:
            rows += [(code, name) for code in fetch_members(label)]
            ok += 1
        except Exception as e:
            log(f"新浪行业 {name} 成分获取失败：{e!r}")
        _time.sleep(pause)
    if not boards or ok * 2 < len(boards):
        log(f"新浪行业刷新成功 {ok}/{len(boards)}，不足一半，保留旧数据")
        return 0
    with conn:
        conn.execute("DELETE FROM industries")
        conn.executemany("INSERT OR REPLACE INTO industries(code, industry, updated) VALUES (?, ?, ?)",
                         [(c, n, today.isoformat()) for c, n in rows])
    log(f"新浪行业刷新完成：{ok} 个行业，{len(rows)} 条成分")
    return len(rows)


def load_map(conn: sqlite3.Connection, day: date) -> dict[str, set[str]]:
    """代码 → 板块名集合。行业名加「行业·」前缀、自建板块加「自建·」前缀，和概念同名也不会混。"""
    out: dict[str, set[str]] = {}
    for code, concept in conn.execute("SELECT code, concept FROM concepts"):
        if concept not in NOT_SECTOR:
            out.setdefault(code, set()).add(concept)
    for code, ind in conn.execute("SELECT code, industry FROM industries"):
        if ind not in NOT_INDUSTRY:
            out.setdefault(code, set()).add(INDUSTRY_PREFIX + ind)
    for code, name in conn.execute(
            "SELECT m.code, s.name FROM own_members m JOIN own_sectors s ON s.id = m.sector_id "
            "WHERE s.status = 'active' AND m.tier = 'core' AND m.status IN ('auto', 'confirmed') AND m.valid_to >= ?",
            (day.isoformat(),)):
        out.setdefault(code, set()).add(OWN_PREFIX + name)
    return out


def blacklisted_codes(conn: sqlite3.Connection, sector_map: dict[str, set[str]], day: date) -> dict[str, str]:
    """黑名单板块里的票 → 命中的板块名。自建板块按 id（REBOUND_SECTOR_BLACKLIST_IDS，含 related 成员），
    概念/行业按名称关键词（REBOUND_SECTOR_BLACKLIST_WORDS）。"""
    out: dict[str, str] = {}
    ids = tuple(config.REBOUND_SECTOR_BLACKLIST_IDS)
    if ids:
        marks = ",".join("?" * len(ids))
        for code, name in conn.execute(
                f"SELECT m.code, s.name FROM own_members m JOIN own_sectors s ON s.id = m.sector_id "
                f"WHERE m.sector_id IN ({marks}) AND m.status IN ('auto', 'confirmed') AND m.valid_to >= ?",
                (*ids, day.isoformat())):
            out.setdefault(code, OWN_PREFIX + name)
    words = config.REBOUND_SECTOR_BLACKLIST_WORDS
    for code, names in sector_map.items():
        if code in out:
            continue
        hit = next((n for n in sorted(names) if any(w in n for w in words)), None)
        if hit:
            out[code] = hit
    return out


def sector_ret5(sector_map: dict[str, set[str]], ret5: dict[str, float],
                min_members: int | None = None) -> dict[str, float]:
    """板块 → 成员 5 日涨幅中位数（%），成员不足 min_members 的不算。"""
    need = config.SECTOR_MIN_MEMBERS if min_members is None else min_members
    members: dict[str, list[float]] = {}
    for code, names in sector_map.items():
        r = ret5.get(code)
        if r is None:
            continue
        for n in names:
            members.setdefault(n, []).append(r)
    return {n: round(statistics.median(v), 2) for n, v in members.items() if len(v) >= need}


def weakest_sector(code: str, sector_map: dict[str, set[str]], sret5: dict[str, float]) -> tuple[str, float] | None:
    """这只票所属板块里 5 日跌得最多的（板块名, 5 日涨幅）；没有可算的板块 → None。"""
    got = [(sret5[n], n) for n in sector_map.get(code, ()) if n in sret5]
    if not got:
        return None
    r, n = min(got)
    return n, r


class SectorView:
    """盘前算一次，给超跌主账户用：黑名单、板块 5 日涨幅、每只票最弱的板块。"""

    def __init__(self, sector_map: dict[str, set[str]], ret5: dict[str, float], blacklist: dict[str, str]):
        self.map = sector_map
        self.ret5 = ret5
        self.blacklist = blacklist
        self.sret5 = sector_ret5(sector_map, ret5)

    @classmethod
    def load(cls, conn: sqlite3.Connection, day: date, ret5: dict[str, float]) -> "SectorView":
        m = load_map(conn, day)
        return cls(m, ret5, blacklisted_codes(conn, m, day))

    def weakest(self, code: str) -> tuple[str, float] | None:
        return weakest_sector(code, self.map, self.sret5)

    def names(self, code: str) -> list[str]:
        return sorted(self.map.get(code, ()))

    def lone_drop(self, code: str) -> tuple[str, float] | None:
        """「个股自己跌」：个股 5 日跌超 REBOUND_LONE_RET5、所属板块（最弱的那个）跌不到 REBOUND_LONE_SECTOR_MAX。
        没有板块归属的不算。回测对板块口径太敏感，默认不拦（config.REBOUND_LONE_FILTER）。"""
        r = self.ret5.get(code)
        w = self.weakest(code)
        if r is None or w is None:
            return None
        if r <= config.REBOUND_LONE_RET5 and w[1] > config.REBOUND_LONE_SECTOR_MAX:
            return w
        return None

    def reason_blocked(self, code: str) -> str | None:
        """主账户不买的理由（黑名单 / 个股自己跌）；能买 → None。"""
        if code in self.blacklist:
            return f"板块黑名单（{self.blacklist[code]}）"
        if not config.REBOUND_LONE_FILTER:
            return None
        w = self.lone_drop(code)
        if w is not None:
            return (f"个股自己跌：5 日 {self.ret5[code]:+.2f}%，所属板块最多跌 {w[1]:+.2f}%（{w[0]}），"
                    f"板块没跟着跌")
        return None
