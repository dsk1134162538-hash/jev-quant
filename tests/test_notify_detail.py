import json
from datetime import date, datetime

from jevq import db
from jevq.ledger import Ledger
from jevq.models import Fill
from jevq.notify_content import buy_push_content, close_summary_content, sell_push_content
import run


def insert_decision(conn, code, strategy, ts, trace):
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, trace, strategy) VALUES (?, ?, ?, ?, ?, ?)",
                 (ts, code, "pass", "detail", json.dumps(trace, ensure_ascii=False), strategy))
    conn.commit()


BOARD_TRACE = {
    "strategy": "board",
    "candidate": {"name": "甲股份", "price": 11.0, "pct": 9.5, "ret5": 3.2,
                 "concept": "题材A", "heat": 4, "score": 6,
                 "ranks": {"pct": 1, "amount": 2, "ret5": 1}},
    "notices": [
        {"date": "2026-09-20", "title": "关于为子公司提供担保进展公告超长标题用于测试截断效果",
         "label": "其他", "confidence": 0.95, "source": "jev",
         "probabilities": {"其他": 0.95, "新增质押": 0.03, "回购": 0.02}, "mine": False, "unsure": False},
        {"date": "2026-09-21", "title": "解除股份质押公告", "label": "解除质押或减持完成",
         "confidence": 1.0, "source": "keyword", "probabilities": None, "mine": False, "unsure": False},
    ],
    "news": {"label": "偏多", "news": ["新闻标题一带来一些积极影响正面消息超长内容测试截断效果哦",
                                     "新闻标题二"],
             "probabilities": {"偏多": 0.6, "中性": 0.3, "偏空": 0.1}, "confidence": 0.82},
    "sealed_at_snapshot": True,
    "outcome": {"bought": True, "shares": 1000, "price": 11.0},
}

REBOUND_TRACE = {
    "strategy": "rebound",
    "candidate": {"name": "乙股份", "price": 9.8, "pct": -1.5, "ret5": -12.4, "rank": 1, "of": 3},
    "notices": [],
    "news": {"label": "中性", "news": [], "probabilities": None, "confidence": None},
    "market": {"mode": "intraday", "sentiment": "正常",
               "sentiment_trace": {"inputs": {}, "label": "正常",
                                   "probabilities": {"正常": 0.5, "火热": 0.3, "偏弱": 0.15, "冰点": 0.05},
                                   "confidence": 0.7}},
    "outcome": {"bought": True, "shares": 500, "price": 9.8},
}


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, title, content):
        self.sent.append((title, content))


def test_board_buy_push_content_includes_full_basic_line_and_jev_detail():
    conn = db.connect(":memory:")
    ts = datetime(2026, 9, 24, 9, 31)
    insert_decision(conn, "600001", "board", ts.isoformat(timespec="seconds"), BOARD_TRACE)
    f = Fill("board_main", "600001", "buy", 11.0, 1000, 6.6, ts, "扫板9.5%")

    content = buy_push_content(conn, f, "甲股份")

    assert "09:31 600001 甲股份 1000股 @11.0｜扫板9.5%｜金额 11,000" in content
    assert "▍为什么选它" in content
    assert "题材A" in content and "龙头分 6" in content
    assert "第1/成交额第2/5日第1" in content
    assert "扫板触发 +9.5%，快照时已封死" in content
    assert "▍Jev 公告排雷（近 90 天 2 条，全部放行）" in content
    assert "→ 标题关键词判定，未调用 Jev" in content
    # 标题截断到 20 字：完整标题（26 字）不该原样出现，截到的前 20 字应该在
    full_title = BOARD_TRACE["notices"][0]["title"]
    assert full_title not in content
    assert full_title[:20] in content
    assert "其他0.95" in content
    assert "▍Jev 消息面：偏多（偏多0.60/中性0.30/偏空0.10，置信度 0.82）" in content
    assert "（共 2 条）" in content
    assert "新闻标题二" in content
    assert "当时市场" not in content   # 扫首板不带这一段


def test_rebound_buy_push_content_includes_market_section_and_empty_news():
    conn = db.connect(":memory:")
    ts = datetime(2026, 9, 24, 10, 0)
    insert_decision(conn, "600002", "rebound", ts.isoformat(timespec="seconds"), REBOUND_TRACE)
    f = Fill("rebound_main", "600002", "buy", 9.8, 500, 5.1, ts, "超跌-1.5%｜5日-12.4%｜公告无雷")

    content = buy_push_content(conn, f, "乙股份")

    assert "10:00 600002 乙股份 500股 @9.8｜超跌-1.5%｜5日-12.4%｜公告无雷｜金额 4,900" in content
    assert "当前 -1.5%｜近 5 日 -12.40%｜候选排名 1/3（5 日跌得多的优先）" in content
    assert "▍Jev 公告排雷（近 90 天 0 条，全部放行）" in content
    assert "无新闻，按中性" in content
    assert "盘中可买" in content and "情绪 正常（正常0.50" in content   # 情绪概率有格式化
    assert "外围" not in content


def test_buy_push_content_missing_trace_still_has_full_basic_line():
    conn = db.connect(":memory:")
    ts = datetime(2026, 9, 24, 10, 5)
    f = Fill("rebound_main", "600003", "buy", 10.0, 200, 5.0, ts, "超跌-2%")

    content = buy_push_content(conn, f, "丁股份")

    assert content == "10:05 600003 丁股份 200股 @10.0｜超跌-2%｜金额 2,000"


def test_sell_push_content_computes_pnl_from_buy_fill_history():
    conn = db.connect(":memory:")
    buy_ts = datetime(2026, 9, 22, 10, 0)
    sell_ts = datetime(2026, 9, 24, 14, 0)
    conn.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
                 "VALUES ('rebound_main', '600004', 'buy', 10.0, 1000, 5.15, ?, 'x')",
                 (buy_ts.isoformat(),))
    conn.commit()
    f = Fill("rebound_main", "600004", "sell", 10.5, 1000, 12.65, sell_ts, "止盈")

    content = sell_push_content(conn, f, "戊股份")

    cost = 10.0 * 1000 + 5.15
    proceeds = 10.5 * 1000 - 12.65
    pct = round((proceeds / cost - 1) * 100, 2)
    amount = round(proceeds - cost, 2)
    assert content == (f"14:00 600004 戊股份 1000股 @10.5｜止盈｜持有 2 天｜"
                       f"盈亏 {pct:+.2f}%（{amount:+,.0f} 元）")


def test_sell_push_content_missing_buy_history_gives_basic_line_only():
    conn = db.connect(":memory:")
    f = Fill("rebound_main", "600005", "sell", 10.5, 1000, 12.65, datetime(2026, 9, 24, 14, 0), "止盈")

    content = sell_push_content(conn, f, "己股份")

    assert content == "14:00 600005 己股份 1000股 @10.5｜止盈"


def test_notify_fills_uses_bracket_titles_for_buy_and_sell():
    conn = db.connect(":memory:")
    notifier = FakeNotifier()
    buy_f = Fill("rebound_main", "600006", "buy", 10.0, 100, 5.0, datetime(2026, 9, 24, 10, 0), "超跌")
    sell_f = Fill("rebound_main", "600006", "sell", 11.0, 100, 5.5, datetime(2026, 9, 24, 14, 0), "止盈")
    run._notify_fills(notifier, conn, [buy_f, sell_f], {"600006": "丙股份"}, print)

    assert notifier.sent[0][0] == "【超跌·主(Jev) 买入】丙股份 @10.0"
    assert notifier.sent[1][0] == "【超跌·主(Jev) 卖出】丙股份 @11.0"


def test_notify_fills_pushes_only_main_accounts_not_shadows_or_retired_lowbuy():
    conn = db.connect(":memory:")
    notifier = FakeNotifier()
    ts = datetime(2026, 9, 24, 10, 0)
    fills = [Fill(a, "600006", "buy", 10.0, 100, 5.0, ts, "x")
             for a in ("main", "shadow", "board_main", "rebound_shadow", "rebound_main", "c1_main", "c1_shadow")]
    run._notify_fills(notifier, conn, fills, {"600006": "丙股份"}, print)

    assert [t for t, _ in notifier.sent] == ["【首板·主(Jev) 买入】丙股份 @10.0", "【超跌·主(Jev) 买入】丙股份 @10.0",
                                             "【c1_main 买入】丙股份 @10.0"]


def test_close_summary_content_moved_to_notify_content():
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    content = close_summary_content(ledger, date(2026, 9, 24), {})
    assert "净值" in content and "今日成交" in content


def test_close_summary_lists_builtin_accounts_without_retired_lowbuy():
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO cash VALUES ('main', 31000.0)")          # 低吸下线前留下的现金行
    ledger = Ledger(conn)
    for dynamic in (None, conn):
        content = close_summary_content(ledger, date(2026, 9, 24), {}, dynamic)
        assert [line.split(" ")[0] for line in content.splitlines()] == [
            "首板·主(Jev)", "首板·影子", "超跌·主(Jev)", "超跌·影子",
            "低吸·主(Jev)", "低吸·影子", "情绪轮动(实盘跟)"]


def test_push_notices_new_format_top3_by_score():
    from jevq.notify_content import buy_push_notices_line
    trace = {"suspects": [
        {"date": "2026-09-04", "title": "甲:股东减持计划公告很长很长很长很长", "category": "减持", "level": "低",
         "score": 0.18, "body_read": True, "verdict": "pass"},
        {"date": "2026-09-02", "title": "甲:回购进展", "category": "回购", "level": "低",
         "score": 0.12, "body_read": False, "verdict": "pass"},
        {"date": "2026-09-01", "title": "甲:质押", "category": "质押", "level": "无",
         "score": 0.02, "body_read": True, "verdict": "pass"},
        {"date": "2026-08-01", "title": "甲:第四条", "category": "质押", "level": "无",
         "score": 0.01, "body_read": True, "verdict": "pass"}],
        "others": 12, "skipped": []}
    lines = buy_push_notices_line(trace)
    assert "疑似 4 条" in lines[0] and "其余 12 条无关" in lines[0]
    assert len(lines) == 4
    assert "减持·低 0.18" in lines[1] and "放行" in lines[1] and "已读正文" in lines[1]
    assert "仅标题" in lines[2]


def test_push_notices_new_format_no_suspects():
    from jevq.notify_content import buy_push_notices_line
    lines = buy_push_notices_line({"suspects": [], "others": 5, "skipped": []})
    assert lines == ["▍Jev 公告排雷（近 90 天 5 条公告，无疑似雷）"]


def test_push_notices_legacy_list_unchanged():
    from jevq.notify_content import buy_push_notices_line
    lines = buy_push_notices_line([{"date": "2026-09-01", "title": "甲:公告", "source": "keyword"}])
    assert lines[0].startswith("▍Jev 公告排雷") and "标题关键词判定" in lines[1]


def test_rebound_buy_push_content_shows_candidate_and_sentiment_without_overseas():
    conn = db.connect(":memory:")
    ts = datetime(2026, 9, 24, 14, 50)
    trace = {**REBOUND_TRACE,
             "candidate": {"name": "丙股份", "price": 8.0, "pct": -2.0, "ret5": -15.3, "rank": 1, "of": 4},
             "market": {"mode": "tail", "sentiment": "偏弱", "sentiment_trace": None}}
    insert_decision(conn, "600003", "rebound", ts.isoformat(timespec="seconds"), trace)
    f = Fill("rebound_main", "600003", "buy", 8.0, 300, 5.0, ts, "超跌-2.0%｜5日-15.3%｜消息面中性")

    content = buy_push_content(conn, f, "丙股份")

    assert "当前 -2.0%｜近 5 日 -15.30%｜候选排名 1/4（5 日跌得多的优先）" in content
    assert "只在尾盘买" in content and "情绪 偏弱" in content
    assert "外围" not in content
