"""开盘首笔快照（dj 2026-11-03，知识库「集合竞价」）：每天第一次拿到今天行情的那一分钟，把全部主板票的开盘价、成交量/额、五档存下来。
竞价量占昨日成交额（知识库：小票 10~20%、十亿到二十亿 5~8%、二十亿以上 4%）、开盘幅度，都要靠它事后回测。"""
import json
import sqlite3
from datetime import datetime

from jevq.data.open_snaps import OpenSnapper
from jevq.models import Quote

T = datetime(2026, 11, 3, 9, 30, 20)


def _q(code, open_, ts=T, vol=8000):
    return Quote(code=code, name=f"N{code}", price=open_ + 0.01, pre_close=10.0, open=open_, high=open_ + 0.05,
                 low=open_, volume=vol, amount=vol * 100 * open_, limit_up=11.0, limit_down=9.0, ts=ts,
                 bid_vols=(100, 200, 300, 400, 500), ask_vols=(50, 60, 70, 80, 90), bid1=open_)


def test_first_tick_of_the_day_is_stored_once_for_main_board_with_todays_quotes(tmp_path):
    db = tmp_path / "m.sqlite"
    snap = OpenSnapper(db)
    quotes = {"600001": _q("600001", 10.4), "000002": _q("000002", 9.9), "300003": _q("300003", 10.4),
              "600004": _q("600004", 10.4, ts=datetime(2026, 11, 2, 15))}
    assert snap.step(quotes, T) == 2
    assert snap.step(quotes, datetime(2026, 11, 3, 9, 31)) == 0                   # 同一天只记第一次
    assert OpenSnapper(db).step(quotes, datetime(2026, 11, 3, 9, 32)) == 0         # 重启也不重复
    conn = sqlite3.connect(db)
    rows = {r[0]: r for r in conn.execute("SELECT code, ts, open, pre_close, volume, amount, bid_vols, ask_vols FROM open_snaps")}
    conn.close()
    assert set(rows) == {"600001", "000002"}
    code, ts, open_, pre, vol, amount, bids, asks = rows["600001"]
    assert (ts, open_, pre, vol) == ("2026-11-03T09:30:20", 10.4, 10.0, 8000)
    assert json.loads(asks) == [50, 60, 70, 80, 90] and amount == 8000 * 100 * 10.4


def test_a_new_day_is_recorded_again_and_nothing_is_stored_without_fresh_quotes(tmp_path):
    snap = OpenSnapper(tmp_path / "m.sqlite")
    assert snap.step({"600001": _q("600001", 10.4, ts=datetime(2026, 11, 2, 15))}, T) == 0
    assert snap.step({"600001": _q("600001", 10.4)}, T) == 1
    nxt = datetime(2026, 11, 4, 9, 30, 5)
    assert snap.step({"600001": _q("600001", 10.5, ts=nxt)}, nxt) == 1
