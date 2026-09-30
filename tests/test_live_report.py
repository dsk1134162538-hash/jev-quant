from datetime import date, datetime

from jevq import config, db
from jevq.live import store as live_store
from jevq.report import live_report_lines


def conn():
    return db.connect(":memory:")


def test_no_section_when_never_run_and_mode_off():
    c = conn()
    assert live_report_lines(c, date(2026, 9, 29)) == []


def test_section_appears_during_dryrun_even_with_zero_orders_today():
    c = conn()
    live_store.set_mode(c, "dryrun")
    lines = live_report_lines(c, date(2026, 9, 29))
    text = "\n".join(lines)
    assert "## 实盘跟单（演练）" in text
    assert "成交 0 笔 → 实盘成交 0 笔、跳过 0 笔" in text


def _add_intent(c, ts, side, code, sim_price, status, note):
    c.execute("INSERT INTO live_intents(ts, source, side, code, name, sim_price, sim_shares, reason, "
              "status, note) VALUES (?, 'sim', ?, ?, ?, ?, 1, 'r', ?, ?)",
              (ts, side, code, code, sim_price, status, note))
    return c.execute("SELECT id FROM live_intents WHERE ts=? AND code=? AND side=?",
                     (ts, code, side)).fetchone()[0]


def _add_filled_order(c, intent_id, ts, side, code, avg_price, shares):
    oid = live_store.add_order(c, intent_id, datetime.fromisoformat(ts), side, code, avg_price, shares, "sending")
    live_store.update_order(c, oid, datetime.fromisoformat(ts), filled=shares, avg_price=avg_price, status="filled")
    return oid


def _seed(c, day1: str, day2: str):
    """day1：第一次实盘成交（买入 600001）。day2（报告日）：卖出 600001 平仓、买入 600002 建仓、
    600004 被跳过、600005 下单失败无成交。"""
    live_store.set_mode(c, "live")

    i_buy1 = _add_intent(c, f"{day1}T09:31:00", "buy", "600001", 9.98, "done", "已跟单")
    _add_filled_order(c, i_buy1, f"{day1}T09:31:30", "buy", "600001", 10.0, 500)

    i_sell = _add_intent(c, f"{day2}T09:35:00", "sell", "600001", 10.0, "done", "已跟单")
    _add_filled_order(c, i_sell, f"{day2}T09:35:30", "sell", "600001", 10.3, 500)

    i_buy2 = _add_intent(c, f"{day2}T09:20:00", "buy", "600002", 8.0, "done", "已跟单")
    _add_filled_order(c, i_buy2, f"{day2}T09:20:30", "buy", "600002", 8.04, 300)

    _add_intent(c, f"{day2}T09:15:00", "buy", "600004", 7.0, "skipped", "余额不足")

    live_store.add_order(c, None, datetime.fromisoformat(f"{day2}T09:10:00"), "buy", "600005", 6.0, 100,
                         "failed", "下单失败")

    for code, side, price, shares, ts in [
        ("600001", "sell", 10.0, 500, f"{day2}T09:35:00"),
        ("600002", "buy", 8.0, 300, f"{day2}T09:20:00"),
        ("600004", "buy", 7.0, 100, f"{day2}T09:15:00"),
    ]:
        c.execute("INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
                  "VALUES (?, ?, ?, ?, ?, 5.0, ?, 'r')",
                  (config.LIVE_ACCOUNT, code, side, price, shares, ts))

    c.execute("INSERT INTO equity VALUES (?, ?, ?)", (day1, config.LIVE_ACCOUNT, 100000.0))
    c.execute("INSERT INTO equity VALUES (?, ?, ?)", (day2, config.LIVE_ACCOUNT, 102000.0))
    c.commit()


def test_live_report_counts_slippage_and_return():
    c = conn()
    _seed(c, "2026-09-28", "2026-09-29")
    lines = live_report_lines(c, date(2026, 9, 29))
    text = "\n".join(lines)

    assert "## 实盘跟单" in text and "演练" not in text
    assert "今天模拟 rebound_main 成交 3 笔 → 实盘成交 2 笔、跳过 1 笔" in text
    assert "跳过原因：余额不足 1 笔" in text
    assert "买入滑点：600002 +0.50%" in text
    assert "卖出滑点：600001 +3.00%" in text
    assert "累计收益率：实盘 +3.00%｜模拟 +2.00%" in text

    # 只写百分比和笔数，不出现成交股数、金额
    for forbidden in ("500", "300", "10.0", "10.3", "8.04", "8.0", "7.0", "6.0", "100000", "102000"):
        assert forbidden not in text, f"不该出现：{forbidden}"
