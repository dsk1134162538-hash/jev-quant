"""低吸下线后 Engine 只做各策略共用的市场服务：每分钟取行情、记成交额；买入阶段按 SENTIMENT_REFRESH_MIN
刷新市场情绪（超跌反弹、自定义策略读它）；行情连续失败置 halted。自己不买不卖。"""
import json
from datetime import date, datetime

from factories import quote
from jevq import db
from jevq.config import SENTIMENT_REFRESH_MIN
from jevq.engine import Engine
from jevq.ledger import Ledger

DAY = datetime(2026, 9, 23)
UNIVERSE = {c: "x" for c in ["600001", "600002"]}


def at(h, m, d=DAY):
    return d.replace(hour=h, minute=m)


class Market:
    def __init__(self, label="正常"):
        self.label, self.calls = label, []

    def sentiment(self, stats):
        self.calls.append(stats)
        return self.label


def make(quotes, market=None, conn=None):
    conn = conn or db.connect(":memory:")
    ledger = Ledger(conn)
    feed = {"quotes": quotes, "asked": []}

    def fetch(codes):
        feed["asked"].append(sorted(codes))
        q = feed["quotes"]
        if isinstance(q, Exception):
            raise q
        return {c: x for c, x in q.items() if c in codes}

    eng = Engine(conn, ledger, market or Market(), fetch, lambda d: {"涨停家数": 50}, UNIVERSE,
                 date(2026, 9, 22), "中性")
    return eng, ledger, feed


def qs(**pcts):
    return {c: quote(c, pct=p) for c, p in pcts.items()}


def test_sell_step_fetches_universe_and_held_codes_and_records_turnover():
    eng, ledger, feed = make(qs(**{"600001": -1.0, "600002": 1.0, "700001": 2.0}))
    ledger.buy("rebound_main", quote("700001", pct=-1.0), at(9, 40), "x")   # 不在股票池的持仓也要拉行情
    assert eng.sell_step(at(10, 0)) == []
    assert feed["asked"] == [["600001", "600002", "700001"]]
    assert set(eng.last_quotes) == {"600001", "600002", "700001"}
    assert eng.last_fetch_minute == "2026-09-23 10:00"
    assert eng.conn.execute("SELECT ts, amount FROM turnover").fetchall() == [("2026-09-23 10:00", 2e7)]  # 只算股票池


def test_engine_never_trades_and_leftover_lowbuy_positions_stay_frozen():
    """低吸下线：库里 main 遗留的持仓原样冻结——不拉它的行情、不卖；引擎也不买、不留判断记录。"""
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO cash VALUES ('main', 1000.0)")
    conn.execute("INSERT INTO positions VALUES ('main', '700002', 1000, 10005.0, 10.0, '2026-09-21')")
    conn.commit()
    eng, ledger, feed = make({**qs(**{"600001": -1.0, "600002": -2.0}), "700002": quote("700002", pct=-5.0)},
                             conn=conn)
    for t in (at(10, 0), at(14, 50), at(9, 31, datetime(2026, 9, 24)), at(14, 55, datetime(2026, 9, 24))):
        assert eng.tick(t) == []
    assert all("700002" not in codes for codes in feed["asked"])
    assert [p.code for p in ledger.positions("main")] == ["700002"] and ledger.cash("main") == 1000.0
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert {v for (v,) in conn.execute("SELECT verdict FROM decisions")} == {"sentiment"}


def test_sentiment_refreshed_in_buy_phase_once_every_refresh_minutes():
    market = Market("火热")
    eng, _, _ = make(qs(**{"600001": -1.0}), market=market)
    asked = []
    for m in range(2 * SENTIMENT_REFRESH_MIN + 1):
        eng.sell_step(at(10, m))
        before = len(market.calls)
        assert eng.buy_step(at(10, m)) == []
        asked.append(len(market.calls) - before)
    idle = [0] * (SENTIMENT_REFRESH_MIN - 1)
    assert asked == [1, *idle, 1, *idle, 1]
    assert eng.sentiment_label == "火热"
    rows = eng.conn.execute("SELECT substr(ts, 12, 5), code, strategy, detail FROM decisions "
                            "WHERE verdict='sentiment' ORDER BY id").fetchall()
    assert [(t, c, s) for t, c, s, _ in rows] == [("10:00", "", "market"), ("10:10", "", "market"),
                                                  ("10:20", "", "market")]
    detail = json.loads(rows[0][3])
    assert detail["label"] == "火热" and detail["overseas"] == "中性" and detail["stats"]["涨停家数"] == 50


def test_sell_step_never_asks_jev_sentiment_waits_for_buy_step():
    """慢 Jev 永远拖不住卖出：卖出阶段只取行情，情绪到买入阶段才问。"""
    market = Market()
    eng, _, _ = make(qs(**{"600001": -1.0}), market=market)
    eng.sell_step(at(10, 0))
    assert market.calls == [] and eng.sentiment_label is None
    eng.buy_step(at(10, 0))
    assert len(market.calls) == 1 and eng.sentiment_label == "正常"


def test_buy_step_without_fresh_quotes_this_minute_does_nothing():
    market = Market()
    eng, _, feed = make(qs(**{"600001": -1.0}), market=market)
    assert eng.buy_step(at(10, 0)) == []                        # 这一分钟还没取行情
    feed["quotes"] = RuntimeError("down")
    eng.sell_step(at(10, 1))
    assert eng.buy_step(at(10, 1)) == []                        # 取数失败：不拿旧行情判情绪
    assert market.calls == []


def test_three_fetch_failures_halt_but_sentiment_keeps_refreshing():
    market = Market()
    eng, _, feed = make(RuntimeError("down"), market=market)
    for m in range(3):
        assert eng.tick(at(10, m)) == []
    assert eng.halted and market.calls == []
    feed["quotes"] = qs(**{"600001": -1.0})
    eng.tick(at(10, 5))
    assert len(market.calls) == 1 and eng.sentiment_label == "正常"


def test_turnover_ratio_vs_previous_day_same_minute():
    eng, _, _ = make(qs(**{"600001": -1.0}))
    eng.conn.execute("INSERT INTO turnover VALUES ('2026-09-22 10:00', 5000000)")
    stats = eng._market_stats(at(10, 0), eng._fetch(at(10, 0)))
    assert stats["成交额较昨日同期%"] == 100.0          # 1e7 vs 5e6


def test_last_fetch_minute_only_set_on_successful_fetch():
    eng, _, feed = make(qs(**{"600001": 2.0}))
    assert eng.last_fetch_minute is None
    eng.tick(at(10, 0))
    assert eng.last_fetch_minute == "2026-09-23 10:00"
    feed["quotes"] = RuntimeError("qt down")
    eng.tick(at(10, 1))
    assert eng.last_fetch_minute == "2026-09-23 10:00"     # 失败不更新，last_quotes 仍是 10:00 的旧快照


def test_restart_restores_todays_sentiment_for_sells_but_buy_phase_still_refreshes():
    """卖出阶段不碰 Jev，自定义卖出读到的情绪是上次刷新的值。重启后第一分钟别是 None：
    从当日最后一条 sentiment 决策恢复标签；买入阶段照旧立即重新判断（不沿用恢复值）。"""
    quotes = qs(**{"600001": -1.0})
    eng, _, _ = make(quotes, market=Market("火热"))
    eng.tick(at(10, 0))
    assert eng.sentiment_label == "火热"
    again, _, _ = make(quotes, market=Market("冰点"), conn=eng.conn)
    again.sell_step(at(10, 5))
    assert again.sentiment_label == "火热"
    again.buy_step(at(10, 5))
    assert again.sentiment_label == "冰点"


def test_restart_restores_sentiment_rows_written_before_lowbuy_retired():
    """下线当天重启：早上的情绪行还是旧写法（strategy 为 'lowbuy' 或空），照样恢复。"""
    for strategy in ("lowbuy", None):
        conn = db.connect(":memory:")
        conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES (?, '', 'sentiment', ?, ?)",
                     ("2026-09-23T09:40:00", '{"label": "偏弱"}', strategy))
        conn.commit()
        eng, _, _ = make(qs(**{"600001": -1.0}), conn=conn)
        eng.sell_step(at(10, 0))
        assert eng.sentiment_label == "偏弱"


def test_sentiment_not_restored_from_previous_day():
    quotes = qs(**{"600001": -1.0})
    eng, _, _ = make(quotes, market=Market("火热"))
    eng.tick(at(10, 0))
    nxt, _, _ = make(quotes, market=Market("冰点"), conn=eng.conn)
    nxt.sell_step(at(10, 0, datetime(2026, 9, 24)))
    assert nxt.sentiment_label is None


class ArticleMarket(Market):
    def sentiment(self, stats, articles=None):
        self.calls.append((stats, articles))
        return self.label


def test_engine_passes_articles_and_exposes_sectors_and_survives_provider_failure():
    arts = [{"title": "晚报", "link": "l", "published": "2026-09-22T19:00", "text": "机器人\n涨停板：甲\n其他强势股：乙"}]
    market = ArticleMarket()
    eng, ledger, feed = make(qs(**{"600001": 1.0}), market=market)
    eng.articles_provider = lambda now: arts
    eng.tick(at(10, 0))
    assert market.calls[0][1] == arts and eng.sectors[0].name == "机器人"
    eng.articles_provider = lambda now: (_ for _ in ()).throw(OSError("down"))
    eng.tick(at(10, 20))                                   # 取文章出错：保留旧文章，情绪照常刷新
    assert len(market.calls) == 2 and eng.articles == arts


def test_engine_without_articles_calls_sentiment_with_stats_only():
    market = Market()
    eng, ledger, feed = make(qs(**{"600001": 1.0}), market=market)
    eng.articles_provider = lambda now: []
    eng.tick(at(10, 0))
    assert len(market.calls) == 1
