"""自建板块（只记录，不进交易）：分类库、成员判定、龙头排序、日快照。
设计 docs/specs/2026-10-04-own-sectors-design.md。本模块不被任何策略 import；所有联网/Jev 调用都可注入，测试不联网。"""
import json
import re
import sqlite3
import threading
import time as _time
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from hashlib import sha1
from pathlib import Path

from jevq import config
from jevq.data import hi189
from jevq.data.timeout import call_with_timeout

SEED_PATH = Path(__file__).with_name("own_sectors_seed.json")
KIND_ZYGC, KIND_INTRO, KIND_INTROJ = "zygc", "intro", "introj"
NONE_OF_THEM = ""            # 产品映射：都不属于任何板块
UNUSABLE = "?"               # 产品映射：这行是收入类型/业务类别，不是具体产品
ACTIVE_MEMBER = ("auto", "confirmed")


# ---------- 板块定义 ----------
def load_seed(conn: sqlite3.Connection, today: date, path: Path = SEED_PATH) -> int:
    """种子板块写入库（已有的不动：用户后来的状态/改动优先）。返回新增条数。"""
    seed = json.loads(Path(path).read_text(encoding="utf-8"))["sectors"]
    n = 0
    for s in seed:
        cur = conn.execute("INSERT OR IGNORE INTO own_sectors(id, name, product_def, aliases, status, created) "
                           "VALUES (?, ?, ?, ?, 'active', ?)",
                           (s["id"], s["name"], s["product_def"], json.dumps(s.get("aliases") or [], ensure_ascii=False),
                            today.isoformat()))
        n += cur.rowcount
    conn.commit()
    return n


def sectors(conn: sqlite3.Connection, status: str = "active") -> list[dict]:
    return [{"id": i, "name": n, "product_def": d, "aliases": json.loads(a or "[]"), "status": st}
            for i, n, d, a, st in conn.execute(
                "SELECT id, name, product_def, aliases, status FROM own_sectors WHERE status=? ORDER BY rowid",
                (status,))]


def defs_hash(secs: list[dict]) -> str:
    """板块定义的指纹：定义变了，旧的「产品→板块」映射作废重判。"""
    return sha1(json.dumps([(s["id"], s["product_def"]) for s in secs], ensure_ascii=False).encode()).hexdigest()[:12]


def _norm(name: str) -> str:
    return re.sub(r"(概念|板块|题材|行业)$", "", re.sub(r"\s+", "", name or ""))


def names_match(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return len(a) >= 2 and len(b) >= 2 and (a in b or b in a)


def sector_terms(s: dict) -> list[str]:
    return [s["name"], *s["aliases"]]


# ---------- 取数与缓存 ----------
def _retry(fn: Callable, retries: int = 2, pause: float = 1.0):
    last = None
    for i in range(retries + 1):
        try:
            return fn()
        except Exception as e:      # noqa: BLE001 —— 取不到就当没有，调用方处理
            last = e
            if i < retries:
                _time.sleep(pause)
    raise last


def em_symbol(code: str) -> str:
    return ("SH" if code.startswith(("6", "9")) else "BJ" if code.startswith(("4", "8")) else "SZ") + code


def fetch_zygc(code: str) -> list[dict]:
    """东方财富主营构成（akshare.stock_zygc_em）→ [{date, type, name, income, ratio}]，ratio 是小数。"""
    import akshare as ak
    df = _retry(lambda: call_with_timeout(ak.stock_zygc_em, symbol=em_symbol(code), timeout=30.0))
    return [{"date": str(r["报告日期"]), "type": r["分类类型"], "name": str(r["主营构成"]),
             "income": None if r["主营收入"] != r["主营收入"] else float(r["主营收入"]),
             "ratio": None if r["收入比例"] != r["收入比例"] else float(r["收入比例"])}
            for _, r in df.iterrows()]


def fetch_intro(code: str) -> dict:
    """同花顺主营介绍（akshare.stock_zyjs_ths）→ {主营业务, 产品类型, 产品名称}。"""
    import akshare as ak
    df = _retry(lambda: call_with_timeout(ak.stock_zyjs_ths, symbol=code, timeout=30.0), retries=3, pause=2.0)
    row = df.iloc[0]
    return {k: str(row[k]) for k in ("主营业务", "产品类型", "产品名称") if k in df.columns}


def cache_get(conn: sqlite3.Connection, kind: str, code: str, today: date, max_age_days: int | None = None):
    row = conn.execute("SELECT fetched, value FROM own_cache WHERE kind=? AND code=?", (kind, code)).fetchone()
    if not row:
        return None
    max_age = config.OWN_SECTORS_REFRESH_DAYS if max_age_days is None else max_age_days
    if (today - date.fromisoformat(row[0])).days >= max_age:
        return None
    return json.loads(row[1])


def cache_put(conn: sqlite3.Connection, kind: str, code: str, today: date, value) -> None:
    conn.execute("INSERT OR REPLACE INTO own_cache VALUES (?, ?, ?, ?)",
                 (kind, code, today.isoformat(), json.dumps(value, ensure_ascii=False)))
    conn.commit()


def product_rows(rows: list[dict]) -> tuple[str | None, list[dict]]:
    """最近一期「按产品分类」的各产品行；去掉「其中:」细分行（是上一行的子项，重复相加会超过 100%）。"""
    prod = [r for r in rows if r.get("type") == "按产品分类" and r.get("ratio") is not None]
    if not prod:
        return None, []
    latest = max(r["date"] for r in prod)
    return latest, [r for r in prod if r["date"] == latest and not r["name"].startswith("其中")]


def shares_by_sector(prods: list[dict], mapping: dict[str, str]) -> tuple[dict[str, float], float]:
    """映射到同一板块的产品行占比相加；返回（板块→占比, 「无法判断」行占比）。"""
    out: dict[str, float] = {}
    unusable = 0.0
    for r in prods:
        sid = mapping.get(r["name"], NONE_OF_THEM)
        if sid == UNUSABLE:
            unusable += r["ratio"]
        elif sid != NONE_OF_THEM:
            out[sid] = out.get(sid, 0.0) + r["ratio"]
    return out, unusable


def tier_of(share: float) -> str | None:
    if share >= config.OWN_SECTORS_CORE_SHARE:
        return "core"
    if share >= config.OWN_SECTORS_RELATED_SHARE:
        return "related"
    return None


# ---------- 候选 ----------
def sina_candidates(conn: sqlite3.Connection, sector: dict, universe: dict[str, str]) -> set[str]:
    """新浪题材里名称与板块相近的成分股，只当候选（成员资格不由它决定）。"""
    terms = sector_terms(sector)
    out: set[str] = set()
    for code, concept in conn.execute("SELECT code, concept FROM concepts"):
        if code in universe and any(names_match(concept, t) for t in terms):
            out.add(code)
    return out


def article_candidates(art_sectors: list, sector: dict, universe: dict[str, str]) -> tuple[set[str], str]:
    """晚报里该方向点名的股票（名称→代码）；返回（候选代码, 点名的文章板块名，没点名为空）。"""
    by_name = {n: c for c, n in universe.items()}
    codes: set[str] = set()
    mentioned = ""
    for s in art_sectors:
        if any(names_match(s.name, t) for t in sector_terms(sector)):
            mentioned = mentioned or s.name
            codes |= {by_name[n] for n in (*s.limit_up, *s.strong) if n in by_name}
    return codes, mentioned


def article_sectors(conn: sqlite3.Connection, today: date, days: int = 7) -> list:
    """近几天缓存的 hi189 晚报解析出的板块。"""
    out = []
    for i in range(days):
        d = today - timedelta(days=i)
        out += hi189.sectors_of(hi189.cached(conn, d))
    return out


# ---------- 成员刷新 ----------
def _member_row(conn, sid: str, code: str):
    return conn.execute("SELECT status FROM own_members WHERE sector_id=? AND code=?", (sid, code)).fetchone()


def _upsert_member(conn, sid, code, tier, share, basis, source, status, today: date):
    row = _member_row(conn, sid, code)
    if row and row[0] == "rejected":
        return
    if row and row[0] == "confirmed":
        status = "confirmed"            # 用户确认过的不被自动结果降级
    conn.execute("INSERT OR REPLACE INTO own_members(sector_id, code, tier, share, basis, source, status, checked, "
                 "valid_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (sid, code, tier, share, json.dumps(basis, ensure_ascii=False), source, status, today.isoformat(),
                  (today + timedelta(days=config.OWN_SECTORS_VALID_DAYS)).isoformat()))


def refresh_members(conn: sqlite3.Connection, today: date, universe: dict[str, str], judge, *,
                    art_sectors: list | None = None, budget: float | None = None,
                    zygc: Callable[[str], list[dict]] = fetch_zygc, intro: Callable[[str], dict] = fetch_intro,
                    log: Callable[[str], None] = print) -> dict:
    """候选 → 取主营构成 → Jev 把产品行映射到板块 → 占比相加 → 记成员。没有可用构成的票走「Jev 读主营介绍」（pending）。
    budget（秒）到点就停，没处理的下个交易日接着来。返回统计。"""
    secs = sectors(conn)
    stats = {"sectors": len(secs), "candidates": 0, "checked": 0, "members": 0, "pending": 0, "failed": 0,
             "skipped_budget": 0}
    if not secs or not universe:
        return stats
    dh = defs_hash(secs)
    art = article_sectors(conn, today) if art_sectors is None else art_sectors
    cand: dict[str, set[str]] = {}          # code → 把它当候选的板块 id
    for s in secs:
        codes = sina_candidates(conn, s, universe) | article_candidates(art, s, universe)[0]
        for c in codes:
            cand.setdefault(c, set()).add(s["id"])
    stats["candidates"] = len(cand)
    deadline = None if budget is None else _time.monotonic() + budget
    by_id = {s["id"]: s for s in secs}
    for code in sorted(cand):
        if deadline is not None and _time.monotonic() >= deadline:
            stats["skipped_budget"] += 1
            continue
        try:
            _check_stock(conn, today, code, universe[code], cand[code], by_id, secs, dh, judge, zygc, intro, stats)
        except Exception as e:      # noqa: BLE001 —— 单只票出错不影响其它
            conn.rollback()
            stats["failed"] += 1
            log(f"[own_sectors] {code} 处理失败：{e!r}")
    conn.commit()
    return stats


def _check_stock(conn, today, code, name, cand_ids, by_id, secs, dh, judge, zygc, intro, stats):
    rows = cache_get(conn, KIND_ZYGC, code, today)
    if rows is None:
        rows = zygc(code)
        cache_put(conn, KIND_ZYGC, code, today, rows)
    period, prods = product_rows(rows)
    mapping: dict[str, str] = {}
    for r in prods:
        mapping[r["name"]] = _map_product(conn, today, r["name"], name, secs, dh, judge)
    stats["checked"] += 1
    shares, unusable = shares_by_sector(prods, mapping)
    if prods and unusable < config.OWN_SECTORS_CORE_SHARE:
        for sid, share in shares.items():
            tier = tier_of(share)
            if tier is None:
                continue
            basis = {"source": "主营构成", "period": period, "name": name,
                     "products": [{"name": r["name"], "ratio": r["ratio"], "sector": mapping[r["name"]]}
                                  for r in prods]}
            _upsert_member(conn, sid, code, tier, round(share, 4), basis, "revenue", "auto", today)
            stats["members"] += 1
        return
    # 没有可用的按产品构成（只有收入类型/金融业务类别，或根本没有）→ Jev 读主营介绍，pending 等用户确认
    info = cache_get(conn, KIND_INTRO, code, today)
    if info is None:
        info = intro(code)
        cache_put(conn, KIND_INTRO, code, today, info)
    for sid in sorted(cand_ids):
        verdict = _judge_intro(conn, today, code, name, info, by_id[sid], judge)
        if verdict and verdict["choice"] == "是":
            basis = {"source": "主营介绍", "name": name, "intro": info, "jev": verdict}
            _upsert_member(conn, sid, code, "core", None, basis, "jev", "pending", today)
            stats["pending"] += 1


# 产品名太泛（10-15 核对时发现）：「其他」被 Jev 结合公司映射成了电力、「化工产品」成了染料、「制药」成了创新药，
# 又按产品名全局缓存，别的公司写着同样的名字也跟着错。「其他…」这类直接算无法判断、不问 Jev；
# 其余泛名（短、档次/类别、行业大类）按「公司｜产品」缓存，不跨公司共用。
_ALWAYS_UNUSABLE = re.compile(r"^(其他|其它|补充|合计|小计|主营业务|其他业务)")
_GENERIC_NAMES = {"设备", "产品", "服务", "材料", "商品", "工程", "医药", "制药", "生物医药", "化工", "化工产品",
                  "化学产品", "精细化工产品", "高档", "中档", "低档", "零部件", "组件", "配件", "贸易", "制造业", "工业"}
_GRADE = re.compile(r"^[A-Z特高中低普]{0,3}[A-Z]?(类|档|级)(产品)?$")


def product_cache_key(product: str, company: str) -> str | None:
    """own_product_map 的键：None = 不用查/问，直接算 UNUSABLE；泛名带上公司。"""
    name = product.strip()
    if _ALWAYS_UNUSABLE.match(name):
        return None
    if len(name) <= 2 or name in _GENERIC_NAMES or _GRADE.match(name):
        return f"{company}｜{name}"
    return name


def _map_product(conn, today, product: str, company: str, secs: list[dict], dh: str, judge) -> str:
    key = product_cache_key(product, company)
    if key is None:
        return UNUSABLE
    row = conn.execute("SELECT sector_id, defs_hash FROM own_product_map WHERE product=?", (key,)).fetchone()
    if row and row[1] == dh:
        return row[0]
    ans = judge.map_product(product, company, secs)       # → 板块 id / NONE_OF_THEM / UNUSABLE，None = Jev 不可用
    if ans is None:
        return NONE_OF_THEM                               # 这次当「都不是」但不缓存，下次再问
    sid, conf = ans
    conn.execute("INSERT OR REPLACE INTO own_product_map VALUES (?, ?, ?, ?, ?)",
                 (key, sid, conf, dh, today.isoformat()))
    conn.commit()
    return sid


def _judge_intro(conn, today, code, name, info, sector, judge):
    key = f"{code}:{sector['id']}"
    cached_v = cache_get(conn, KIND_INTROJ, key, today)
    if cached_v is not None:
        return cached_v
    v = judge.judge_intro(name, info, sector)
    if v is not None:
        cache_put(conn, KIND_INTROJ, key, today, v)
    return v


# ---------- 龙头与日快照 ----------
def members_of(conn: sqlite3.Connection, sector_id: str, day: date) -> list[dict]:
    """生效中的 core 成员（auto/confirmed、未过期）。pending/rejected/related 不算。"""
    return [{"code": c, "share": s, "status": st, "source": src}
            for c, s, st, src in conn.execute(
                "SELECT code, share, status, source FROM own_members WHERE sector_id=? AND tier='core' "
                "AND status IN ('auto', 'confirmed') AND valid_to >= ?", (sector_id, day.isoformat()))]


def board_height(conn: sqlite3.Connection, code: str, day: date, today_limit: bool) -> int:
    """连板高度：今天封死/涨到涨停算 1，往前数连续涨停日（用 closes 里的收盘价判断）。"""
    if not today_limit:
        return 0
    rows = conn.execute("SELECT date, close FROM closes WHERE code=? AND date < ? ORDER BY date DESC LIMIT 8",
                        (code, day.isoformat())).fetchall()
    n = 1
    for i in range(len(rows) - 1):
        if rows[i][1] >= round(rows[i + 1][1] * 1.1, 2):
            n += 1
        else:
            break
    return n


def rank_leaders(conn, members: list[dict], quotes: dict, ret5: dict[str, float], day: date,
                 universe: dict[str, str], n: int | None = None) -> list[dict]:
    """龙头：当日涨幅、成交额、近 5 日涨幅、纯度（占比高的优先）、连板高度，名次相加，小者在前，同分看涨幅。
    每项名次都留在记录里，方便以后换权重重算。"""
    n = config.OWN_SECTORS_LEADERS if n is None else n
    pool = [m for m in members if m["code"] in quotes]
    if not pool:
        return []
    info = {}
    for m in pool:
        q = quotes[m["code"]]
        limit = q.high >= q.limit_up if q.limit_up else False
        info[m["code"]] = {"pct": q.pct, "amount": q.amount, "ret5": ret5.get(m["code"]),
                           "share": m["share"], "board": board_height(conn, m["code"], day, limit)}

    def rank(key):
        ordered = sorted(pool, key=lambda m: (info[m["code"]][key] is None, -(info[m["code"]][key] or 0)))
        return {m["code"]: i + 1 for i, m in enumerate(ordered)}

    ranks = {k: rank(k) for k in ("pct", "amount", "ret5", "share", "board")}
    scored = sorted(pool, key=lambda m: (sum(ranks[k][m["code"]] for k in ranks), -info[m["code"]]["pct"]))
    return [{"rank": i + 1, "code": m["code"], "name": universe.get(m["code"], ""),
             "score": sum(ranks[k][m["code"]] for k in ranks),
             "ranks": {k: ranks[k][m["code"]] for k in ranks}, **info[m["code"]]}
            for i, m in enumerate(scored[:n])]


def sector_news(conn: sqlite3.Connection, s: dict, day: date) -> dict:
    """这个板块的隔夜相关新闻（jevq.data.catalyst 的当天摘要里，板块名或别名对得上的那一条）：给 Claude 判板块情绪用。"""
    from jevq.data import catalyst
    data = catalyst.cached(conn, day)
    names = {s["name"], *(s.get("aliases") or [])}
    for t in (data or {}).get("topics", []):
        if names & set(t.get("topics") or [t["topic"]]):
            return {"隔夜相关新闻数": t["count"], "隔夜相关新闻": [f"{n['title'][:40]}（{n['source']}）" for n in t["news"][:3]]}
    return {}


def _sector_data(conn: sqlite3.Connection, s: dict, day: date, quotes: dict, ret5: dict[str, float],
                 universe: dict[str, str], judge, art: list) -> dict:
    """一个板块此刻的统计、龙头和 Jev 强弱（日快照、盘中快照共用）。"""
    members = members_of(conn, s["id"], day)
    qs = [quotes[m["code"]] for m in members if m["code"] in quotes and quotes[m["code"]].ts.date() >= day]
    strong = sum(q.pct >= config.OWN_SECTORS_STRONG_PCT for q in qs)
    limit_up = sum(bool(q.limit_up) and q.high >= q.limit_up for q in qs)
    leaders = rank_leaders(conn, members, quotes, ret5, day, universe)
    mentioned_name = article_candidates(art, s, universe)[1]
    data = {"members": len(members), "with_quote": len(qs), "strong": strong, "limit_up": limit_up,
            "avg_pct": round(sum(q.pct for q in qs) / len(qs), 2) if qs else None,
            "amount": sum(q.amount for q in qs), "max_board": max([l["board"] for l in leaders], default=0),
            "leaders": leaders, "article_mentioned": mentioned_name,
            "jev_strength": None, "jev_probs": None, "jev_confidence": None}
    if judge is not None and qs and (strong >= config.OWN_SECTORS_JEV_MIN_STRONG or mentioned_name):   # 线为 0 = 有行情就问
        res = judge.strength(s, {k: data[k] for k in ("members", "strong", "limit_up", "avg_pct", "amount",
                                                      "max_board")} | {"晚报点名": bool(mentioned_name)}
                             | sector_news(conn, s, day))
        if res:
            data["jev_strength"], data["jev_probs"], data["jev_confidence"] = res
    return data


def snapshot_day(conn: sqlite3.Connection, day: date, quotes: dict, ret5: dict[str, float],
                 universe: dict[str, str], judge=None, art_sectors: list | None = None,
                 log: Callable[[str], None] = print) -> int:
    """收盘后给每个生效板块记一行日快照（统计、龙头、Jev 强弱）。返回记录条数。"""
    art = article_sectors(conn, day) if art_sectors is None else art_sectors
    written = 0
    for s in sectors(conn):
        data = _sector_data(conn, s, day, quotes, ret5, universe, judge, art)
        conn.execute("INSERT OR REPLACE INTO own_sector_daily VALUES (?, ?, ?)",
                     (day.isoformat(), s["id"], json.dumps(data, ensure_ascii=False)))
        written += 1
    conn.commit()
    return written


def snapshot_intraday(conn: sqlite3.Connection, at: datetime, quotes: dict, ret5: dict[str, float],
                      universe: dict[str, str], judge=None, art_sectors: list | None = None) -> int:
    """盘中某个时点给有成员行情的板块各记一行（统计、龙头、Jev 强弱），时间记到分钟。返回记录条数。"""
    day = at.date()
    art = article_sectors(conn, day) if art_sectors is None else art_sectors
    ts = at.strftime("%Y-%m-%dT%H:%M")
    written = 0
    for s in sectors(conn):
        data = _sector_data(conn, s, day, quotes, ret5, universe, judge, art)
        if not data["with_quote"]:
            continue
        conn.execute("INSERT OR REPLACE INTO own_sector_intraday VALUES (?, ?, ?)",
                     (ts, s["id"], json.dumps(data, ensure_ascii=False)))
        conn.commit()           # 每个板块写完就提交：后台线程和交易进程共用一个库，别长时间占着写锁
        written += 1
    return written


def due_slot(t: time, slots=None, grace: int = 10) -> time | None:
    """现在该跑的盘中时点：最近一个已到、且没过 grace 分钟的时点（重启晚了不补跑旧时点）。"""
    slots = config.OWN_SECTORS_INTRADAY_SLOTS if slots is None else slots
    now_min = t.hour * 60 + t.minute
    for slot in sorted(slots, reverse=True):
        start = slot.hour * 60 + slot.minute
        if start <= now_min < start + grace:
            return slot
    return None


class IntradayRecorder:
    """盘中每个时点在后台线程记一次板块情绪（只记录，用户 2026-10-09 要求）。
    交易循环只调 tick()：不等 Jev、不共用交易的库连接；上一轮还在跑就跳过这一轮；出错只记日志。"""

    def __init__(self, open_conn: Callable[[], sqlite3.Connection], make_judge: Callable,
                 universe: dict[str, str], ret5: dict[str, float], log: Callable[[str], None] = print,
                 slots=None, thread=threading.Thread):
        self.open_conn, self.make_judge = open_conn, make_judge
        self.universe, self.ret5, self.log = universe, ret5, log
        self.slots = config.OWN_SECTORS_INTRADAY_SLOTS if slots is None else slots
        self.thread_cls = thread
        self.done: set = set()
        self._thread = None

    def tick(self, now: datetime, quotes: dict) -> bool:
        """返回这一分钟有没有启动一轮记录。"""
        if not config.OWN_SECTORS_ENABLED or not quotes:
            return False
        slot = due_slot(now.time(), self.slots)
        key = (now.date(), slot)
        if slot is None or key in self.done:
            return False
        self.done.add(key)
        if self._thread is not None and self._thread.is_alive():
            self.log(f"盘中板块 {slot:%H:%M}：上一轮还没跑完，这一轮跳过")
            return False
        snap = {c: q for c, q in quotes.items() if c in self.universe}
        at = datetime.combine(now.date(), slot)
        self._thread = self.thread_cls(target=self._run, args=(at, snap), daemon=True, name="own-sectors-intraday")
        self._thread.start()
        return True

    def _run(self, at: datetime, quotes: dict) -> None:
        conn = None
        try:
            conn = self.open_conn()
            n = snapshot_intraday(conn, at, quotes, self.ret5, self.universe, self.make_judge(conn))
            self.log(f"盘中板块 {at:%H:%M}：记录 {n} 个板块")
        except Exception as e:      # noqa: BLE001
            self.log(f"盘中板块 {at:%H:%M} 记录失败：{e!r}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:   # noqa: BLE001
                    pass


# ---------- 给面板/报告读 ----------
def overview(conn: sqlite3.Connection) -> dict:
    """面板「自建板块」页：每个板块最近一天快照 + 成员（含依据）+ 待确认。"""
    last = conn.execute("SELECT MAX(date) FROM own_sector_daily").fetchone()[0]
    daily = {sid: json.loads(d) for sid, d in conn.execute(
        "SELECT sector_id, data FROM own_sector_daily WHERE date=?", (last,))} if last else {}
    iday = conn.execute("SELECT MAX(substr(ts, 1, 10)) FROM own_sector_intraday").fetchone()[0]
    intraday: dict[str, list] = {}
    if iday:
        for ts, sid, d in conn.execute("SELECT ts, sector_id, data FROM own_sector_intraday "
                                       "WHERE substr(ts, 1, 10) = ? ORDER BY ts", (iday,)):
            d = json.loads(d)
            intraday.setdefault(sid, []).append({"time": ts[11:16], **{k: d.get(k) for k in (
                "jev_strength", "avg_pct", "strong", "limit_up")}})
    out = []
    for s in sectors(conn) + sectors(conn, "proposed"):
        members = [{"code": c, "tier": t, "share": sh, "source": src, "status": st, "checked": ck,
                    "valid_to": vt, "basis": json.loads(b or "{}")}
                   for c, t, sh, b, src, st, ck, vt in conn.execute(
                       "SELECT code, tier, share, basis, source, status, checked, valid_to FROM own_members "
                       "WHERE sector_id=? AND status != 'rejected' ORDER BY tier, share DESC", (s["id"],))]
        out.append({**s, "daily": daily.get(s["id"]), "members": members, "intraday": intraday.get(s["id"], [])})
    return {"date": last, "sectors": out, "intraday_date": iday}


def propose_from_articles(conn: sqlite3.Connection, today: date, art_sectors: list) -> int:
    """晚报里出现、现有板块都对不上的板块名，记为 proposed 等用户确认（不自动生效）。"""
    known = [t for st in ("active", "proposed", "rejected") for s in sectors(conn, st) for t in sector_terms(s)]
    n = 0
    for a in art_sectors:
        if any(names_match(a.name, t) for t in known):
            continue
        sid = "proposed_" + sha1(a.name.encode()).hexdigest()[:8]
        cur = conn.execute("INSERT OR IGNORE INTO own_sectors(id, name, product_def, aliases, status, created) "
                           "VALUES (?, ?, ?, ?, 'proposed', ?)",
                           (sid, a.name, f"（晚报提到的方向「{a.name}」，待补充产品定义）", json.dumps([a.name], ensure_ascii=False),
                            today.isoformat()))
        n += cur.rowcount
        known.append(a.name)
    conn.commit()
    return n


def snapshot_step(conn, today: date, universe: dict[str, str], quotes: dict, ret5: dict[str, float], judge, *,
                  log: Callable[[str], None] = print) -> int | None:
    """收盘后的日快照（快，要赶在报告之前）。整体不抛异常；关闭或失败返回 None。"""
    if not config.OWN_SECTORS_ENABLED:
        return None
    try:
        load_seed(conn, today)
        return snapshot_day(conn, today, quotes, ret5, universe, judge, None, log)
    except Exception as e:      # noqa: BLE001
        conn.rollback()
        log(f"[own_sectors] 日快照失败：{e!r}")
        return None


def refresh_step(conn, today: date, universe: dict[str, str], judge, *, budget: float | None = None,
                 log: Callable[[str], None] = print, zygc=fetch_zygc, intro=fetch_intro) -> dict | None:
    """报告之后的成员刷新（慢，有预算）：提议新板块 + 刷新成员。整体不抛异常；关闭或失败返回 None。"""
    if not config.OWN_SECTORS_ENABLED:
        return None
    try:
        load_seed(conn, today)
        art = article_sectors(conn, today)
        proposed = propose_from_articles(conn, today, art)
        stats = refresh_members(conn, today, universe, judge, art_sectors=art, budget=budget, log=log,
                                zygc=zygc, intro=intro)
        return {**stats, "proposed": proposed}
    except Exception as e:      # noqa: BLE001
        conn.rollback()
        log(f"[own_sectors] 成员刷新失败：{e!r}")
        return None


def record_day(conn: sqlite3.Connection, today: date, universe: dict[str, str], quotes: dict,
               ret5: dict[str, float], judge, *, budget: float | None = None, log: Callable[[str], None] = print,
               zygc: Callable[[str], list[dict]] = fetch_zygc, intro: Callable[[str], dict] = fetch_intro) -> dict:
    """成员刷新 + 日快照一起跑（测试和手动补跑用）。关闭时返回 {"enabled": False}。"""
    if not config.OWN_SECTORS_ENABLED:
        return {"enabled": False}
    members = refresh_step(conn, today, universe, judge, budget=budget, log=log, zygc=zygc, intro=intro)
    snaps = snapshot_step(conn, today, universe, quotes, ret5, judge, log=log)
    out: dict = {"enabled": True, "members": members, "snapshots": snaps}
    if members is None or snaps is None:
        out["error"] = "见日志"
    return out


def report_lines(conn: sqlite3.Connection, day: date, names: dict[str, str] | None = None) -> list[str]:
    """复盘报告里的「自建板块（只记录）」一节：当天 Jev 判为火热/正常的板块和龙头前三。"""
    rows = conn.execute("SELECT s.name, d.data FROM own_sector_daily d JOIN own_sectors s ON s.id=d.sector_id "
                        "WHERE d.date=? ORDER BY s.rowid", (day.isoformat(),)).fetchall()
    lines = ["", "## 自建板块（只记录，不参与交易）", ""]
    if not rows:
        return lines + ["今天没有记录。"]
    shown = False
    for name, raw in rows:
        d = json.loads(raw)
        if d.get("jev_strength") not in ("火热", "正常"):
            continue
        top = "、".join(f"{l['name'] or l['code']}" for l in (d.get("leaders") or [])[:3]) or "无成员行情"
        lines.append(f"- {name}：Jev {d['jev_strength']}｜成员 {d['members']}，强势 {d['strong']}，涨停 {d['limit_up']}｜龙头 {top}")
        shown = True
    return lines if shown else lines + ["没有 Jev 判为火热/正常的板块。"]
