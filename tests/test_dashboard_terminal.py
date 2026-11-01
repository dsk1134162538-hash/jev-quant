"""行情终端页数据（dj 2026-11-01）：只读，账户按秒级行情估值，休市时宽度退回最近收盘日。"""
from datetime import datetime

from factories import quote

from jevq import db
from dashboard.terminal import TerminalData


class NoGateway:
    trust_env = True

    def get(self, *a, **k):
        raise OSError("连不上")


def _db(tmp_path):
    path = tmp_path / "jevq.sqlite"
    conn = db.connect(str(path))
    conn.execute("INSERT INTO cash VALUES ('regime_main', 1000.0)")
    conn.execute("INSERT INTO positions VALUES ('regime_main', '600152', 1000, 7450, 7.45, '2026-10-30')")
    conn.execute("INSERT INTO equity VALUES ('2026-10-30', 'regime_main', 8450.0)")
    conn.executemany("INSERT INTO closes(date, code, close) VALUES (?, ?, ?)",
                     [("2026-10-29", "600152", 7.0), ("2026-10-30", "600152", 7.45), ("2026-10-29", "600001", 10.0),
                      ("2026-10-30", "600001", 9.0)])
    conn.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) VALUES "
                 "('regime_main', '600152', 'buy', 7.45, 1000, 5, ?, '轮动[偏弱]跟超跌反弹｜普跌抄底')",
                 (datetime.now().isoformat(timespec='seconds'),))
    conn.execute("INSERT INTO live_state VALUES ('mode', 'live'), ('paused', '1'), ('pause_reason', '连续读取失败')")
    conn.commit()
    conn.close()
    return path


def test_payload_values_holdings_with_live_quotes_and_reports_status(tmp_path):
    t = TerminalData(_db(tmp_path), tmp_path, session=NoGateway())
    q = quote("600152", pct=2.0, ts=datetime.now())
    d = t.payload({"600152": q}, {"available": False}, None, [])
    acct = next(a for a in d["accounts"] if a["id"] == "regime_main")
    assert acct["live"] and acct["equity"] == round(1000 + q.price * 1000, 2)
    assert acct["positions"][0]["pct"] == 2.0 and acct["positions"][0]["live"]
    assert d["market"]["live"] is False and d["market"]["asOf"] == "2026-10-30"
    assert d["market"]["up"] == 1 and d["market"]["down"] == 1 and d["market"]["limitDown"] == 1
    assert d["fills"][0]["side"] == "buy" and d["fills"][0]["reason"] == "轮动[偏弱]跟超跌反弹" and d["fills"][0]["live"]
    assert d["live"]["paused"] and d["gateway"] == {"ok": False, "text": "连不上"}
    assert t.held_codes() == ["600152"]


def test_missing_db_is_reported_not_raised(tmp_path):
    d = TerminalData(tmp_path / "nope.sqlite", tmp_path, session=NoGateway()).payload({}, {}, None, [])
    assert d["error"] == "找不到 jevq.sqlite" and d["accounts"] == []
