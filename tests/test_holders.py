"""股东户数太多、股性不活跃的票主账户不买（用户 2026-10-19）。"""
from datetime import date, datetime

from factories import quote

from jevq import config, db
from jevq.data import holders
from jevq.ledger import Ledger
from jevq.strategies.lowbuy import LowbuyStrategy
from jevq.strategies.rebound import ReboundStrategy

TODAY = date(2026, 9, 24)
PREV_DAY = date(2026, 9, 23)


def at(h, m):
    return datetime(2026, 9, 24, h, m)


def test_refresh_keeps_old_data_when_too_few_rows():
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO holders VALUES ('600001', 50000, '2026-06-30', '2026-09-01')")
    conn.commit()
    assert holders.refresh(conn, TODAY, fetch=lambda: [("600002", 1, "x")], log=lambda *_: None) == 0
    assert holders.load(conn) == {"600001": 50000}
    rows = [(f"60{i:04d}", 10000 + i, "2026-06-30") for i in range(1200)]
    assert holders.refresh(conn, TODAY, fetch=lambda: rows, log=lambda *_: None) == 1200
    assert len(holders.load(conn)) == 1200 and not holders.needs_refresh(conn, TODAY)


def test_mean_abs_change_uses_days_before_today():
    conn = db.connect(":memory:")
    closes = [10.0, 10.1, 10.0, 10.2, 10.0]                 # 每天 1%、-0.99%、2%、-1.96%
    for k, c in enumerate(closes):
        conn.execute("INSERT INTO closes(date, code, close) VALUES (?, '600001', ?)", (f"2026-09-{18 + k:02d}", c))
    conn.execute("INSERT INTO closes(date, code, close) VALUES ('2026-09-24', '600001', 20.0)")   # 今天的不算
    conn.commit()
    got = holders.mean_abs_change(conn, ["600001"], TODAY, days=4)
    assert abs(got["600001"] - 1.49) < 0.01


def test_stock_filter_reasons_and_unknown_data_passes():
    f = holders.StockFilter({"600001": 254702, "600002": 40000}, {"600002": 0.8, "600003": 2.0})
    assert f.blocked("600001").startswith("股东户数 25.5 万")
    assert f.blocked("600002").startswith("股性不活跃")
    assert f.blocked("600003") is None and f.blocked("600009") is None


class Judge:
    def __init__(self):
        self.calls, self.last_trace = [], None

    def check(self, code, *args):
        self.calls.append(code)
        return "pass", "ok"


UNIVERSE = {"600001": "甲", "600002": "乙", "600003": "丙"}
SHRINK4 = {"vols": [100.0, 400.0, 300.0, 200.0, 150.0], "vols_until": PREV_DAY.isoformat()}


def _quotes(t, **pcts):
    return {c: quote(c, pct=p, amount=2e8, ts=t, volume=10) for c, p in pcts.items()}


def test_rebound_main_skips_filtered_stock_shadow_buys_it():
    conn = db.connect(":memory:")
    strat = ReboundStrategy(conn, Ledger(conn), Judge(), Judge(), UNIVERSE,
                            {"600001": -5.0, "600002": -15.0, "600003": -8.0}, set(), PREV_DAY, lambda: "正常",
                            features={c: SHRINK4 for c in UNIVERSE})
    strat.stock_filter = holders.StockFilter({"600002": 254702}, {})
    fills = strat.on_minute(_quotes(at(10, 0), **{c: -1.0 for c in UNIVERSE}), at(10, 0))
    assert {(f.account, f.code) for f in fills} == {("rebound_main", "600003"), ("rebound_shadow", "600002")}
    strat.on_minute(_quotes(at(10, 1), **{c: -1.0 for c in UNIVERSE}), at(10, 1))
    rows = conn.execute("SELECT code, detail FROM decisions WHERE verdict='skip'").fetchall()
    assert len(rows) == 1 and rows[0][0] == "600002" and "股东户数" in rows[0][1]


def test_lowbuy_main_skips_filtered_stock_and_logs_once():
    conn = db.connect(":memory:")
    uni = {"600001": "甲", "600002": "乙"}
    strat = LowbuyStrategy(conn, Ledger(conn), Judge(), Judge(), uni, {"600001": 15.0, "600002": 10.0}, PREV_DAY,
                           lambda: "正常", lambda: "中性")
    strat.stock_filter = holders.StockFilter({}, {"600001": 0.9})
    fills = strat.buy_step(_quotes(at(10, 40), **{"600001": -1.0, "600002": -1.0}), at(10, 40))
    assert {(f.account, f.code) for f in fills} == {("lowbuy_main", "600002"), ("lowbuy_shadow", "600001")}
    assert conn.execute("SELECT code FROM decisions WHERE verdict='skip' AND strategy='lowbuy'").fetchall() == [
        ("600001",)]


def test_run_stock_filter_loads_and_survives_missing_table():
    import run
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO holders VALUES ('600001', 90000, '2026-06-30', '2026-09-20')")
    conn.commit()
    logs = []
    f = run._stock_filter(conn, UNIVERSE, TODAY, logs.append)
    assert f is not None and f.blocked("600001") and "8 万户 1 只" in logs[0]
    conn.execute("DROP TABLE holders")
    assert run._stock_filter(conn, UNIVERSE, TODAY, logs.append) is None and "个股过滤数据失败" in logs[-1]
