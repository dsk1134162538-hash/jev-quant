"""9% 触线快照（dj 2026-11-02）：全市场主板票第一次涨到 9% 的那一分钟，记下盘口和当时的市场状态，攒买点样本。"""
import json
import sqlite3
from datetime import datetime

from jevq.data.touch_snaps import TouchRecorder
from jevq.models import Quote

T = datetime(2026, 11, 3, 9, 41)


def _q(code, price, ts=T, limit_up=11.0, vol=5000):
    return Quote(code=code, name=f"N{code}", price=price, pre_close=10.0, open=10.2, high=max(price, 10.2),
                 low=10.0, volume=vol, amount=vol * 100 * price, limit_up=limit_up, limit_down=9.0, ts=ts,
                 turnover=3.2, float_mcap=45.0, outer_vol=3000, inner_vol=2000,
                 bid_vols=(100, 200, 300, 400, 500), ask_vols=(50, 60, 70, 80, 90), bid1=price - 0.01)


def test_first_touch_of_main_board_stock_is_recorded_once_with_book_and_market(tmp_path):
    db = tmp_path / "m.sqlite"
    rec = TouchRecorder(db)
    quotes = {"600001": _q("600001", 10.92), "000002": _q("000002", 11.0),     # 一个 9.2%，一个已封
              "300003": _q("300003", 11.0), "600004": _q("600004", 10.5),     # 创业板不记；5% 不记
              "600005": _q("600005", 11.0, ts=datetime(2026, 10, 31, 15, 0))}  # 旧行情不记
    assert rec.step(quotes, T) == 2
    assert rec.step(quotes, datetime(2026, 11, 3, 9, 42)) == 0                 # 同一天不重复
    conn = sqlite3.connect(db)
    rows = {r[0]: r for r in conn.execute(
        "SELECT code, hhmm, pct, sealed, ask_vols, market_sealed, market_touched, turnover FROM touch_snaps")}
    conn.close()
    assert set(rows) == {"600001", "000002"}
    code, hhmm, pct, sealed, asks, m_sealed, m_touched, turnover = rows["600001"]
    assert (hhmm, pct, sealed, json.loads(asks), turnover) == ("09:41", 9.2, 0, [50, 60, 70, 80, 90], 3.2)
    assert (m_sealed, m_touched) == (2, 3)          # 这一刻全市场（含创业板）封住 2 只、≥9% 3 只
    assert rows["000002"][3] == 1


def test_new_day_starts_fresh_and_existing_rows_survive_restart(tmp_path):
    db = tmp_path / "m.sqlite"
    TouchRecorder(db).step({"600001": _q("600001", 10.95)}, T)
    again = TouchRecorder(db)                                                   # 重启：当天已记的不再记
    assert again.step({"600001": _q("600001", 10.95)}, T) == 0
    nxt = datetime(2026, 11, 4, 9, 35)
    assert again.step({"600001": _q("600001", 10.95, ts=nxt)}, nxt) == 1
