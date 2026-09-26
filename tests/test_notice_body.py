from datetime import date

from jevq import db
from jevq.data import notice_body as nb

TODAY = date(2026, 9, 26)


class FakeResp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc, self.urls = payload, exc, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.urls.append((url, params))
        if self.exc:
            raise self.exc
        return FakeResp(self.payload)


def test_fetch_body_cleans_and_truncates():
    s = FakeSession({"data": {"notice_content": "  第一段\n\n 第二段\r\n" + "字" * 3000}})
    text = nb.fetch_body("AN1", session=s)
    assert text.startswith("第一段 第二段")
    assert len(text) == 1500
    assert s.urls[0][1]["art_code"] == "AN1"


def test_fetch_body_failure_returns_none():
    assert nb.fetch_body("AN1", session=FakeSession(exc=OSError("down"))) is None
    assert nb.fetch_body("AN1", session=FakeSession({"data": None})) is None
    assert nb.fetch_body("AN1", session=FakeSession({"data": {"notice_content": "   "}})) is None


def test_get_body_caches_success_only():
    conn = db.connect(":memory:")
    calls = []

    def ok(aid):
        calls.append(aid)
        return "正文"

    assert nb.get_body(conn, "AN1", fetch=ok) == "正文"
    assert nb.get_body(conn, "AN1", fetch=ok) == "正文"
    assert calls == ["AN1"]

    fails = []
    assert nb.get_body(conn, "AN2", fetch=lambda a: fails.append(a)) is None
    assert nb.get_body(conn, "AN2", fetch=lambda a: fails.append(a)) is None
    assert fails == ["AN2", "AN2"]          # 失败不写缓存，下次重试


def _seed(conn):
    conn.executemany("INSERT INTO notices VALUES (?, ?, ?, ?, ?)", [
        ("AN1", "600001", "甲:关于股东减持计划的公告", "", "2026-09-20"),
        ("AN2", "600001", "甲:第五届董事会决议公告", "", "2026-09-20"),     # 非疑似：不抓
        ("AN3", "600002", "乙:关于回购进展的公告", "", "2026-09-10"),
        ("AN4", "600009", "丙:关于减持计划的公告", "", "2026-09-10"),       # 不在股票池：不抓
        ("AN5", "600001", "甲:关于减持的公告", "", "2026-05-01"),           # 超出 90 天：不抓
    ])
    conn.commit()


def test_prefetch_only_suspects_in_universe_within_lookback_and_uncached():
    conn = db.connect(":memory:")
    _seed(conn)
    conn.execute("INSERT INTO notice_bodies VALUES ('AN3', '已缓存', '2026-09-25')")
    got = []
    n = nb.prefetch_bodies(conn, TODAY, ["600001", "600002"], fetch=lambda a: got.append(a) or f"正文{a}")
    assert sorted(got) == ["AN1"] and n == 1
    assert nb.get_body(conn, "AN1", fetch=lambda a: None) == "正文AN1"


def test_prefetch_respects_cap_and_counts_failures_as_not_fetched():
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO notices VALUES (?, ?, ?, ?, ?)",
                     [(f"AN{i}", "600001", "甲:减持公告", "", "2026-09-20") for i in range(10)])
    conn.commit()
    got = []
    n = nb.prefetch_bodies(conn, TODAY, ["600001"], fetch=lambda a: got.append(a) or None, cap=3)
    assert len(got) == 3 and n == 0


# ---- review fixes ----


def test_prefetch_stops_waiting_at_budget_and_keeps_finished_results():
    """正文接口卡死时，预抓不能把 13:00 的交易 tick 堵住：到预算就返回，已抓到的照写。"""
    import threading
    import time
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO notices VALUES (?, ?, ?, ?, ?)",
                     [(f"AN{i}", "600001", "甲:减持公告", "", "2026-09-20") for i in range(6)])
    conn.commit()
    release = threading.Event()

    def fetch(aid):
        if aid in ("AN0", "AN1"):
            return f"正文{aid}"
        release.wait(5)
        return None

    t0 = time.monotonic()
    try:
        n = nb.prefetch_bodies(conn, TODAY, ["600001"], fetch=fetch, workers=4, budget=0.3)
    finally:
        release.set()
    assert time.monotonic() - t0 < 2
    assert n == 2
    assert {r[0] for r in conn.execute("SELECT id FROM notice_bodies")} == {"AN0", "AN1"}


def test_prefetch_one_raising_fetch_does_not_lose_the_others():
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO notices VALUES (?, ?, ?, ?, ?)",
                     [(f"AN{i}", "600001", "甲:减持公告", "", "2026-09-20") for i in range(3)])
    conn.commit()

    def fetch(aid):
        if aid == "AN1":
            raise RuntimeError("boom")
        return f"正文{aid}"

    assert nb.prefetch_bodies(conn, TODAY, ["600001"], fetch=fetch) == 2
