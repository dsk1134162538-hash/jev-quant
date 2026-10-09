"""自建板块（只记录）：占比判定、产品映射缓存、候选、龙头、日快照、Jev 出错、隔离。"""
import json
from datetime import date, datetime

from factories import quote
from jevq import db
from jevq.data import own_sectors as os_
from jevq.data.hi189 import Sector
from jevq.judge.jev import JevError
from jevq.judge.own_sectors import NO_SECTOR, UNUSABLE_LABEL, OwnSectorJudge

TODAY = date(2026, 10, 8)
UNIVERSE = {"600001": "甲布", "600002": "乙显", "600003": "丙茅", "600004": "丁药", "600005": "戊泛"}


class FakeJev:
    """按问题类型回答；answers[产品名/键] 可预设。"""
    def __init__(self, products=None, fit="是", level="火热", fail=False):
        self.products, self.fit, self.level, self.fail, self.calls = products or {}, fit, level, fail, []

    def ask(self, state, questions, **kw):
        self.calls.append((state, questions))
        if self.fail:
            raise JevError("down")
        (key, q), = questions.items()
        if key == "sector":
            return {key: {"type": "choice", "choice": self.products.get(state["主营产品行"], NO_SECTOR),
                          "confidence": 0.9}}
        if key == "fit":
            return {key: {"type": "choice", "choice": self.fit, "confidence": 0.8}}
        legend = {str(i): v for i, v in enumerate(q["criteria"])}
        idx = q["criteria"].index(self.level)
        probs = {k: (0.9 if int(k) == idx else 0.1 / 3) for k in legend}
        return {key: {"type": "score", "score": idx, "legend": legend, "probabilities": probs, "confidence": 0.7}}


def zrow(name, ratio, typ="按产品分类", date_="2026-06-30"):
    return {"date": date_, "type": typ, "name": name, "income": 1.0, "ratio": ratio}


def make(conn=None):
    conn = conn or db.connect(":memory:")
    os_.load_seed(conn, TODAY)
    return conn


def test_seed_loads_once_and_keeps_user_changes():
    conn = make()
    assert os_.load_seed(conn, TODAY) == 0
    conn.execute("UPDATE own_sectors SET status='rejected' WHERE id='dairy'")
    os_.load_seed(conn, TODAY)
    assert "dairy" not in {s["id"] for s in os_.sectors(conn)}
    assert {"electronic_cloth", "glass_substrate"} <= {s["id"] for s in os_.sectors(conn)}


def test_product_rows_drops_sub_items_and_other_types():
    rows = [zrow("电子级玻璃纤维布", 0.9856), zrow("其中:特种电子布", 0.34), zrow("电子纱", 0.0105),
            zrow("华东", 0.5, typ="按地区分类"), zrow("旧产品", 1.0, date_="2025-12-31")]
    period, prods = os_.product_rows(rows)
    assert period == "2026-06-30" and [p["name"] for p in prods] == ["电子级玻璃纤维布", "电子纱"]
    assert os_.product_rows([zrow("x", None)]) == (None, [])


def test_shares_sum_same_sector_and_tiers_at_boundaries():
    prods = [zrow("A", 0.3), zrow("B", 0.2), zrow("C", 0.4), zrow("D", 0.1)]
    shares, unusable = os_.shares_by_sector(prods, {"A": "s1", "B": "s1", "C": "s2", "D": os_.UNUSABLE})
    assert shares == {"s1": 0.5, "s2": 0.4} and abs(unusable - 0.1) < 1e-9
    assert os_.tier_of(0.5) == "core" and os_.tier_of(0.4999) == "related"
    assert os_.tier_of(0.3) == "related" and os_.tier_of(0.2999) is None


def test_refresh_members_core_related_and_product_map_cached():
    conn = make()
    conn.execute("INSERT INTO concepts VALUES ('600001', '电子布概念', 'x'), ('600002', '玻璃基板', 'x')")
    zygc = {"600001": [zrow("电子级玻璃纤维布", 0.986), zrow("其中:特种电子布", 0.34)],
            "600002": [zrow("LED显示屏", 0.7), zrow("LED照明", 0.3)]}
    jev = FakeJev({"电子级玻璃纤维布": "电子布"})
    stats = os_.refresh_members(conn, TODAY, UNIVERSE, OwnSectorJudge(jev), art_sectors=[],
                                zygc=lambda c: zygc[c], intro=lambda c: {})
    assert stats["candidates"] == 2 and stats["members"] == 1
    assert conn.execute("SELECT sector_id, tier, share, source, status FROM own_members").fetchall() == [
        ("electronic_cloth", "core", 0.986, "revenue", "auto")]            # 雷曼式（LED 显示）不进玻璃基板
    n = len(jev.calls)
    os_.refresh_members(conn, TODAY, UNIVERSE, OwnSectorJudge(jev), art_sectors=[],
                        zygc=lambda c: zygc[c], intro=lambda c: {})
    assert len(jev.calls) == n                                            # 产品映射已缓存，不重复问 Jev


def test_changed_sector_definitions_invalidate_product_map():
    conn = make()
    conn.execute("INSERT INTO concepts VALUES ('600001', '电子布', 'x')")
    jev = FakeJev({"布": "电子布"})
    args = dict(art_sectors=[], zygc=lambda c: [zrow("布", 0.9)], intro=lambda c: {})
    os_.refresh_members(conn, TODAY, UNIVERSE, OwnSectorJudge(jev), **args)
    n = len(jev.calls)
    conn.execute("UPDATE own_sectors SET product_def='改了定义' WHERE id='dairy'")
    os_.refresh_members(conn, TODAY, UNIVERSE, OwnSectorJudge(jev), **args)
    assert len(jev.calls) == n + 1


def test_unusable_products_fall_back_to_intro_and_stay_pending():
    conn = make()
    conn.execute("INSERT INTO concepts VALUES ('600004', '创新药', 'x')")
    jev = FakeJev({"技术授权": UNUSABLE_LABEL, "销售商品": UNUSABLE_LABEL}, fit="是")
    stats = os_.refresh_members(conn, TODAY, UNIVERSE, OwnSectorJudge(jev), art_sectors=[],
                                zygc=lambda c: [zrow("技术授权", 0.76), zrow("销售商品", 0.23)],
                                intro=lambda c: {"主营业务": "研发生产销售医药产品"})
    assert stats["pending"] == 1 and stats["members"] == 0
    row = conn.execute("SELECT tier, status, source, share FROM own_members").fetchone()
    assert row == ("core", "pending", "jev", None)
    assert os_.members_of(conn, "innovative_drug", TODAY) == []           # pending 不算板块成员
    # 否 → 不记
    conn2 = make()
    conn2.execute("INSERT INTO concepts VALUES ('600004', '创新药', 'x')")
    os_.refresh_members(conn2, TODAY, UNIVERSE, OwnSectorJudge(FakeJev({"技术授权": UNUSABLE_LABEL}, fit="否")),
                        art_sectors=[], zygc=lambda c: [zrow("技术授权", 1.0)], intro=lambda c: {})
    assert conn2.execute("SELECT COUNT(*) FROM own_members").fetchone()[0] == 0


def test_rejected_and_confirmed_members_are_respected():
    conn = make()
    conn.execute("INSERT INTO concepts VALUES ('600001', '电子布', 'x')")
    args = dict(art_sectors=[], zygc=lambda c: [zrow("布", 0.9)], intro=lambda c: {})
    jev = OwnSectorJudge(FakeJev({"布": "电子布"}))
    conn.execute("INSERT INTO own_members VALUES ('electronic_cloth', '600001', 'core', 0.1, '{}', 'manual', "
                 "'rejected', '2026-01-01', '2027-01-01')")
    os_.refresh_members(conn, TODAY, UNIVERSE, jev, **args)
    assert conn.execute("SELECT status, share FROM own_members").fetchone() == ("rejected", 0.1)
    conn.execute("UPDATE own_members SET status='confirmed'")
    os_.refresh_members(conn, TODAY, UNIVERSE, jev, **args)
    assert conn.execute("SELECT status, share FROM own_members").fetchone() == ("confirmed", 0.9)


def test_jev_down_records_nothing_and_does_not_crash():
    conn = make()
    conn.execute("INSERT INTO concepts VALUES ('600001', '电子布', 'x')")
    stats = os_.refresh_members(conn, TODAY, UNIVERSE, OwnSectorJudge(FakeJev(fail=True)), art_sectors=[],
                                zygc=lambda c: [zrow("布", 0.9)], intro=lambda c: {})
    assert stats["members"] == 0
    assert conn.execute("SELECT COUNT(*) FROM own_product_map").fetchone()[0] == 0    # Jev 失败不缓存


def test_fetch_failure_isolated_per_stock():
    conn = make()
    conn.execute("INSERT INTO concepts VALUES ('600001', '电子布', 'x'), ('600002', '电子布', 'x')")

    def zygc(code):
        if code == "600001":
            raise OSError("down")
        return [zrow("布", 0.9)]

    stats = os_.refresh_members(conn, TODAY, UNIVERSE, OwnSectorJudge(FakeJev({"布": "电子布"})), art_sectors=[],
                                zygc=zygc, intro=lambda c: {}, log=lambda m: None)
    assert stats["failed"] == 1 and stats["members"] == 1


def test_article_candidates_by_names():
    art = [Sector("人形机器人", ["甲布"], ["乙显"]), Sector("无关", ["丙茅"], [])]
    s = {"id": "humanoid_robot", "name": "人形机器人", "aliases": ["机器人"], "product_def": ""}
    assert os_.article_candidates(art, s, UNIVERSE) == ({"600001", "600002"}, "人形机器人")
    assert os_.article_candidates([], s, UNIVERSE) == (set(), "")


def test_propose_new_sector_from_article_does_not_activate():
    conn = make()
    art = [Sector("光伏", [], []), Sector("商业航天", ["x"], [])]
    assert os_.propose_from_articles(conn, TODAY, art) == 1                 # 光伏已有，商业航天是新的
    assert os_.propose_from_articles(conn, TODAY, art) == 0                 # 不重复提议
    assert [s["name"] for s in os_.sectors(conn, "proposed")] == ["商业航天"]
    assert "商业航天" not in [s["name"] for s in os_.sectors(conn)]


def _quotes():
    ts = datetime(2026, 10, 8, 15, 0)
    return {"600001": quote("600001", pct=10.0, amount=9e8, ts=ts), "600002": quote("600002", pct=6.0, amount=5e8, ts=ts),
            "600003": quote("600003", pct=-2.0, amount=1e8, ts=ts)}


def _core(conn, sid, code, share):
    conn.execute("INSERT INTO own_members VALUES (?, ?, 'core', ?, '{}', 'revenue', 'auto', '2026-10-01', '2027-01-01')",
                 (sid, code, share))


def test_rank_leaders_and_snapshot_with_jev_strength():
    conn = make()
    for c, sh in (("600001", 0.9), ("600002", 0.6), ("600003", 0.55)):
        _core(conn, "electronic_cloth", c, sh)
    conn.execute("INSERT INTO closes(date, code, close) VALUES ('2026-10-07', '600001', 11.0), "
                 "('2026-10-06', '600001', 10.0)")
    ret5 = {"600001": 20.0, "600002": 5.0, "600003": -3.0}
    qs = _quotes()
    leaders = os_.rank_leaders(conn, os_.members_of(conn, "electronic_cloth", TODAY), qs, ret5, TODAY, UNIVERSE)
    assert [l["code"] for l in leaders] == ["600001", "600002", "600003"] and leaders[0]["board"] == 2
    assert leaders[0]["ranks"]["pct"] == 1 and leaders[0]["name"] == "甲布"
    jev = FakeJev(level="火热")
    n = os_.snapshot_day(conn, TODAY, qs, ret5, UNIVERSE, OwnSectorJudge(jev), art_sectors=[Sector("电子布", [], [])])
    assert n == len(os_.sectors(conn))
    d = json.loads(conn.execute("SELECT data FROM own_sector_daily WHERE sector_id='electronic_cloth'").fetchone()[0])
    assert d["members"] == 3 and d["strong"] == 2 and d["limit_up"] == 1 and d["jev_strength"] == "火热"
    assert d["jev_probs"]["火热"] > 0.5 and d["leaders"][0]["code"] == "600001" and d["article_mentioned"] == "电子布"
    quiet = json.loads(conn.execute("SELECT data FROM own_sector_daily WHERE sector_id='dairy'").fetchone()[0])
    assert quiet["members"] == 0 and quiet["jev_strength"] is None          # 没动静的板块不问 Jev
    assert sum(1 for s, _ in jev.calls if "板块" in s) == 1


def test_snapshot_survives_jev_failure():
    conn = make()
    _core(conn, "electronic_cloth", "600001", 0.9)
    os_.snapshot_day(conn, TODAY, _quotes(), {}, UNIVERSE, OwnSectorJudge(FakeJev(fail=True)),
                     art_sectors=[Sector("电子布", [], [])])
    d = json.loads(conn.execute("SELECT data FROM own_sector_daily WHERE sector_id='electronic_cloth'").fetchone()[0])
    assert d["jev_strength"] is None and d["strong"] == 1


def test_overview_hides_rejected_and_shows_pending_and_basis():
    conn = make()
    _core(conn, "electronic_cloth", "600001", 0.9)
    conn.execute("INSERT INTO own_members VALUES ('dairy', '600003', 'core', NULL, '{\"source\": \"主营介绍\"}', 'jev', "
                 "'pending', '2026-10-08', '2027-01-01')")
    conn.execute("INSERT INTO own_members VALUES ('dairy', '600005', 'core', 0.9, '{}', 'revenue', 'rejected', "
                 "'2026-10-08', '2027-01-01')")
    ov = os_.overview(conn)
    dairy = next(s for s in ov["sectors"] if s["id"] == "dairy")
    assert [m["code"] for m in dairy["members"]] == ["600003"] and dairy["members"][0]["basis"]["source"] == "主营介绍"


def test_record_day_disabled_and_isolated_from_trading_tables(monkeypatch):
    conn = make()
    conn.execute("INSERT INTO concepts VALUES ('600001', '电子布', 'x')")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES ('t', '600001', 'pass', 'd', 'board')")
    before = [conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("decisions", "fills", "positions", "cash")]
    res = os_.record_day(conn, TODAY, UNIVERSE, _quotes(), {}, OwnSectorJudge(FakeJev({"布": "电子布"})),
                         zygc=lambda c: [zrow("布", 0.9)], intro=lambda c: {})
    assert res["members"]["members"] == 1 and res["snapshots"] > 0
    assert [conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("decisions", "fills", "positions", "cash")] == before
    monkeypatch.setattr(os_.config, "OWN_SECTORS_ENABLED", False)
    assert os_.record_day(conn, TODAY, UNIVERSE, {}, {}, None) == {"enabled": False}


def test_record_day_never_raises(monkeypatch):
    conn = make()
    monkeypatch.setattr(os_, "refresh_members", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    logs = []
    res = os_.record_day(conn, TODAY, UNIVERSE, {}, {}, None, log=logs.append)
    assert res["error"] and res["members"] is None and any("boom" in m for m in logs)


def test_budget_stops_processing(monkeypatch):
    conn = make()
    conn.execute("INSERT INTO concepts VALUES ('600001', '电子布', 'x'), ('600002', '电子布', 'x')")
    stats = os_.refresh_members(conn, TODAY, UNIVERSE, OwnSectorJudge(FakeJev()), art_sectors=[], budget=-1,
                                zygc=lambda c: pytest_fail(), intro=lambda c: {})
    assert stats["skipped_budget"] == 2 and stats["checked"] == 0


def pytest_fail():
    raise AssertionError("预算用完不该再取数")


# ---- 面板接口与页面 ----
def _api(tmp_path, build):
    from jevq.app.api import Api
    path = str(tmp_path / "t.sqlite")
    conn = db.connect(path)
    build(conn)
    conn.commit()
    conn.close()
    api = Api.__new__(Api)
    api._db_path = path
    api._log = lambda m: None
    return api, path


def test_api_review_and_decide_only_touch_own_tables(tmp_path):
    def build(conn):
        os_.load_seed(conn, TODAY)
        conn.execute("INSERT INTO own_members VALUES ('dairy', '600003', 'core', NULL, '{}', 'jev', 'pending', "
                     "'2026-10-08', '2027-01-01')")
        os_.propose_from_articles(conn, TODAY, [Sector("商业航天", [], [])])
        conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES ('t', 'c', 'pass', 'd', 'board')")
    api, path = _api(tmp_path, build)
    assert api.own_sector_review("dairy", "600003", "confirm")["ok"]
    assert not api.own_sector_review("dairy", "999999", "confirm")["ok"]
    assert not api.own_sector_review("dairy", "600003", "删除")["ok"]
    conn = db.connect(path)
    assert conn.execute("SELECT status FROM own_members").fetchone()[0] == "confirmed"
    pid = os_.sectors(conn, "proposed")[0]["id"]
    assert not api.own_sector_decide(pid, "activate", "")["ok"]                 # 必须写产品定义
    assert api.own_sector_decide(pid, "activate", "商业航天火箭与卫星整机及配套")["ok"]
    assert os_.sectors(conn, "proposed") == [] and "商业航天" in [s["name"] for s in os_.sectors(conn)]
    assert not api.own_sector_decide("dairy", "reject")["ok"]                  # 只能处理 proposed
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    assert api.own_sectors()["sectors"][0]["name"]


def test_api_own_sectors_empty_when_tables_missing(tmp_path):
    import sqlite3
    from jevq.app.api import Api
    path = str(tmp_path / "old.sqlite")
    sqlite3.connect(path).execute("CREATE TABLE x(a)").connection.commit()
    api = Api.__new__(Api)
    api._db_path, api._log = path, (lambda m: None)
    assert api.own_sectors() == {"date": None, "sectors": []}


def test_home_view_blocks_own_sector_writes():
    from jevq.app.home_view import ReadOnlyApi
    assert not ReadOnlyApi.own_sector_review(None, "a", "b", "confirm")["ok"]
    assert not ReadOnlyApi.own_sector_decide(None, "a", "activate", "x")["ok"]


def test_report_section_lists_only_hot_or_normal(tmp_path):
    conn = make()
    for sid, label in (("dairy", "火热"), ("liquor", "偏弱"), ("solar", "正常")):
        conn.execute("INSERT INTO own_sector_daily VALUES (?, ?, ?)", (TODAY.isoformat(), sid, json.dumps(
            {"jev_strength": label, "members": 3, "strong": 2, "limit_up": 1,
             "leaders": [{"name": "甲", "code": "1"}, {"name": "乙", "code": "2"}]}, ensure_ascii=False)))
    text = "\n".join(os_.report_lines(conn, TODAY))
    assert "乳业：Jev 火热" in text and "光伏：Jev 正常" in text and "白酒" not in text and "龙头 甲、乙" in text
    assert "今天没有记录" in "\n".join(os_.report_lines(conn, date(2026, 1, 1)))


import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

UI = Path(__file__).resolve().parent.parent / "jevq" / "app" / "ui.html"
_HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const noop = () => {};
const ctx = {console, document: {addEventListener: noop, getElementById: () => null,
                                 querySelectorAll: () => [], querySelector: () => null},
             window: {addEventListener: noop}, CSS: {escape: (s) => s}, setInterval: noop, setTimeout: noop};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + "\n;globalThis.f = ownSectorsHtml;", ctx);
console.log(JSON.stringify(JSON.parse(fs.readFileSync(process.argv[3], "utf8")).map((c) => ctx.f(c))));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="没有安装 node")
def test_own_sectors_page_renders_escapes_and_empty(tmp_path):
    js = "\n".join(re.findall(r"<script>(.*?)</script>", UI.read_text(encoding="utf-8"), flags=re.S))
    (tmp_path / "ui.js").write_text(js, encoding="utf-8")
    (tmp_path / "h.js").write_text(_HARNESS, encoding="utf-8")
    evil = "<script>x</script>"
    member = {"code": "600001", "tier": "core", "share": 0.986, "source": "revenue", "status": "pending",
              "valid_to": "2027-01-01", "basis": {"source": "主营构成", "period": "2026-06-30", "name": evil,
                                                 "products": [{"name": evil, "ratio": 0.986}]}}
    data = {"date": "2026-10-08", "sectors": [
        {"id": "electronic_cloth", "name": evil, "product_def": evil, "status": "active", "aliases": [],
         "daily": {"jev_strength": "火热", "members": 1, "strong": 1, "limit_up": 1, "article_mentioned": "电子布",
                   "leaders": [{"rank": 1, "code": "600001", "name": evil, "pct": 10.0, "share": 0.9, "board": 2,
                                "score": 5}]},
         "members": [member]},
        {"id": "proposed_x", "name": "商业航天", "product_def": "", "status": "proposed", "members": [], "daily": None}]}
    (tmp_path / "c.json").write_text(json.dumps([None, {"sectors": []}, data], ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(["node", str(tmp_path / "h.js"), str(tmp_path / "ui.js"), str(tmp_path / "c.json")],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    e1, e2, full = json.loads(r.stdout)
    assert "还没有自建板块记录" in e1 and "还没有自建板块记录" in e2
    assert "<script>" not in full and "&lt;script&gt;" in full
    assert "Jev 火热" in full and "晚报点名" in full and "待确认成员 1 个" in full
    assert 'data-own-review="confirm"' in full and 'data-own-decide="activate"' in full and "商业航天" in full
