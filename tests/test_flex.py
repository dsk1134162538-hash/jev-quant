"""AI 灵活交易账户（dj 2026-11-03「要灵活交易，有时候就得突破一下」）：DeepSeek 自己判断买什么、买多少、什么时候卖，
可以突破任何选股规则（标「模式外」），只留保命的护栏：单票 ≤30% 净值、买入必须写止损位和「错了的信号」、
当日亏 3% 当天不再开新仓、连亏 3 笔当天不再开新仓、亏损持仓不加仓、持仓 ≤5 只。止损由 daemon 每分钟盯着执行。
订单由 ai_loop 写进 dashboard/runtime/flex_orders.json，daemon 的执行器按实时行情成交。"""
import json
from datetime import date, datetime, timedelta

import pytest
from factories import quote

from jevq import config, db
from jevq.ledger import Ledger
from jevq.strategies.flex import AiflexStrategy

ACCOUNT = "aiflex_main"
NOW = datetime(2026, 11, 3, 10, 0, 30)
CAL = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]


def _q(code, price=10.0, pre=10.0, ts=NOW, **kw):
    return quote(code, pct=round((price / pre - 1) * 100, 2), pre_close=pre, ts=ts, **kw)


def _order(i=1, **kw):
    base = {"id": f"FLEX-20261103-1000-{i}", "action": "买入", "code": "600001", "pct": 0.2, "stop": 9.5, "ref_price": 10.0,
            "invalid": "跌破 9.5 说明分歧没被接住", "mode": "模式内", "pattern": "补涨", "reason": "龙头 6 板，补涨第一只"}
    base.update(kw)
    return base


def _setup(tmp_path, orders, ts=NOW, can_buy=True):
    path = tmp_path / "flex_orders.json"
    path.write_text(json.dumps({"cycleId": "FLEX-20261103-1000", "ts": ts.isoformat(timespec="seconds"), "orders": orders},
                               ensure_ascii=False), encoding="utf-8")
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    return AiflexStrategy(conn, ledger, CAL, orders_path=path, can_buy=can_buy), ledger, conn


def _decisions(conn):
    return conn.execute("SELECT verdict, code, detail, trace FROM decisions WHERE strategy='aiflex' ORDER BY rowid").fetchall()


def test_buy_is_sized_by_percent_of_equity_and_logs_the_stop_and_mode(tmp_path):
    s, ledger, conn = _setup(tmp_path, [_order()])
    fills = s.buy_step({"600001": _q("600001")}, NOW)
    assert len(fills) == 1 and fills[0].account == ACCOUNT and fills[0].side == "buy"
    assert fills[0].shares == 2300 and "补涨" in fills[0].reason and "模式内" in fills[0].reason      # 12 万 × 20% ÷ 10 元，手续费含在预算里、整手向下取
    rows = {v: json.loads(t) for v, c, d, t in _decisions(conn) if t}
    assert rows["stop"]["stop"] == 9.5 and rows["stop"]["invalid"].startswith("跌破") and rows["stop"]["mode"] == "模式内"
    assert s.buy_step({"600001": _q("600001")}, NOW + timedelta(minutes=1)) == []                    # 同一张订单只成交一次


def test_single_stock_is_capped_at_forty_percent_of_equity(tmp_path):
    s, ledger, _ = _setup(tmp_path, [_order(pct=0.9)])
    fills = s.buy_step({"600001": _q("600001")}, NOW)
    assert fills[0].shares == 4700                                                                    # 12 万 × 40% ÷ 10 元（含手续费、整手）；dj 2026-11-11 票数少、仓位大


@pytest.mark.parametrize("change,why", [
    ({"stop": None}, "止损"), ({"stop": 10.5}, "止损"), ({"stop": 8.0}, "止损线太远"), ({"invalid": ""}, "错了的信号"),
    ({"mode": "随便"}, "模式"), ({"ref_price": 9.0}, "偏离"), ({"code": "300001"}, "主板"), ({"pct": 0}, "仓位")])
def test_buy_rejected_without_the_required_guardrail_fields(tmp_path, change, why):
    code = change.get("code", "600001")
    s, ledger, conn = _setup(tmp_path, [_order(**change)])
    assert s.buy_step({code: _q(code)}, NOW) == []
    verdicts = [(v, d) for v, c, d, t in _decisions(conn)]
    assert verdicts and verdicts[0][0] == "order_reject" and why in verdicts[0][1]


def test_cannot_buy_at_limit_up_or_with_stale_quote_or_when_buying_is_off(tmp_path):
    s, ledger, conn = _setup(tmp_path, [_order()])
    assert s.buy_step({"600001": _q("600001", price=11.0)}, NOW) == []                                # 涨停价买不到
    s2, _, _ = _setup(tmp_path, [_order()])
    assert s2.buy_step({"600001": _q("600001", ts=NOW - timedelta(minutes=10))}, NOW) == []
    s3, _, _ = _setup(tmp_path, [_order()], can_buy=False)
    assert s3.buy_step({"600001": _q("600001")}, NOW) == []


def test_at_most_three_positions_and_no_adding_to_a_losing_position(tmp_path):
    s, ledger, conn = _setup(tmp_path, [_order(code="600004")])
    for i in range(1, 4):
        ledger.buy(ACCOUNT, _q(f"60000{i}"), NOW - timedelta(days=1), "x", budget=10000)
    qs = {f"60000{i}": _q(f"60000{i}") for i in range(1, 5)}
    assert s.buy_step(qs, NOW) == []                                                                  # 第 4 只
    assert "持仓已满（3 只）" in _decisions(conn)[0][2]
    s2, ledger2, conn2 = _setup(tmp_path, [_order(ref_price=9.7)])
    ledger2.buy(ACCOUNT, _q("600001"), NOW - timedelta(days=1), "x", budget=10000)
    assert s2.buy_step({"600001": _q("600001", price=9.7)}, NOW) == []                                # 亏损持仓不加仓
    assert "亏损" in _decisions(conn2)[0][2]


def test_no_new_buys_after_losing_three_percent_of_equity_today(tmp_path):
    s, ledger, conn = _setup(tmp_path, [_order(code="600002")])
    ledger.buy(ACCOUNT, _q("600001"), NOW - timedelta(days=1), "x", budget=70000)                    # 6900 股，跌到 9.4：亏 ≈4100 > 3%
    qs = {"600001": _q("600001", price=9.4), "600002": _q("600002")}
    assert s.buy_step(qs, NOW) == []
    assert "当日亏损" in _decisions(conn)[0][2]


def test_no_new_buys_after_three_losing_trades_in_a_row_today(tmp_path):
    s, ledger, conn = _setup(tmp_path, [_order(code="600009")])
    for i in range(1, 4):
        buy_q, sell_q = _q(f"60000{i}"), _q(f"60000{i}", price=9.8)
        ledger.buy(ACCOUNT, buy_q, NOW - timedelta(days=1), "x", budget=10000)
        pos = ledger.positions(ACCOUNT)[-1]
        ledger.sell(ACCOUNT, pos, sell_q, NOW - timedelta(minutes=30 - i), "亏")
    assert s.buy_step({"600009": _q("600009")}, NOW) == []
    assert "连亏" in _decisions(conn)[0][2]


def test_declared_stop_is_enforced_every_minute_but_not_on_the_buy_day(tmp_path):
    s, ledger, conn = _setup(tmp_path, [_order()])
    s.buy_step({"600001": _q("600001")}, NOW)
    # 买入当天（T+1 卖不了）：跌破止损也卖不掉
    assert s.sell_step({"600001": _q("600001", price=9.4)}, NOW + timedelta(minutes=30)) == []
    # 第二天跌破 9.5 → 止损
    t2 = datetime(2026, 11, 4, 9, 45)
    sold = s.sell_step({"600001": _q("600001", price=9.4, ts=t2)}, t2)
    assert len(sold) == 1 and sold[0].side == "sell" and "止损" in sold[0].reason and "9.5" in sold[0].reason
    # 没破止损：不动
    s2, ledger2, _ = _setup(tmp_path, [_order()])
    s2.buy_step({"600001": _q("600001")}, NOW)
    assert s2.sell_step({"600001": _q("600001", price=9.6, ts=t2)}, t2) == []


def test_sell_order_needs_a_position_held_since_a_previous_day(tmp_path):
    sell = {"id": "FLEX-20261103-1000-2", "action": "卖出", "code": "600001", "reason": "预期消失：龙头断板"}
    s, ledger, conn = _setup(tmp_path, [sell])
    ledger.buy(ACCOUNT, _q("600001"), NOW, "x", budget=10000)                                        # 今天买的
    assert s.sell_step({"600001": _q("600001")}, NOW) == []
    assert "T+1" in _decisions(conn)[0][2]
    s2, ledger2, _ = _setup(tmp_path, [sell])
    ledger2.buy(ACCOUNT, _q("600001"), NOW - timedelta(days=1), "x", budget=10000)
    sold = s2.sell_step({"600001": _q("600001")}, NOW)
    assert len(sold) == 1 and "预期消失" in sold[0].reason


def test_orders_older_than_ten_minutes_are_expired_and_a_restart_does_not_repeat_them(tmp_path):
    s, ledger, conn = _setup(tmp_path, [_order()], ts=NOW - timedelta(minutes=11))
    assert s.buy_step({"600001": _q("600001")}, NOW) == []
    assert "过期" in _decisions(conn)[0][2]
    s2, ledger2, conn2 = _setup(tmp_path, [_order()])
    assert len(s2.buy_step({"600001": _q("600001")}, NOW)) == 1
    again = AiflexStrategy(conn2, ledger2, CAL, orders_path=tmp_path / "flex_orders.json")            # 重启
    assert again.buy_step({"600001": _q("600001")}, NOW + timedelta(minutes=1)) == []


def test_second_trader_has_its_own_account_orders_file_and_decision_trail(tmp_path):
    # 二号交易员（趋势风格）共用执行器和护栏，但账户、订单文件、decisions 里的 strategy 和止损记录都各自独立
    path1, path2 = tmp_path / "flex_orders.json", tmp_path / "flex2_orders.json"
    for p, oid in ((path1, "A-1"), (path2, "B-1")):
        p.write_text(json.dumps({"cycleId": "c", "ts": NOW.isoformat(timespec="seconds"),
                                 "orders": [_order(id=oid, code="600001" if p is path1 else "600002")]}, ensure_ascii=False), encoding="utf-8")
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    one = AiflexStrategy(conn, ledger, CAL, orders_path=path1)
    two = AiflexStrategy(conn, ledger, CAL, orders_path=path2, sid="aiflex2")
    assert two.account == "aiflex2_main" and one.account == "aiflex_main"
    qs = {"600001": _q("600001"), "600002": _q("600002")}
    f1, f2 = one.buy_step(qs, NOW), two.buy_step(qs, NOW)
    assert [f.account for f in f1] == ["aiflex_main"] and [f.account for f in f2] == ["aiflex2_main"]
    rows = conn.execute("SELECT strategy, code FROM decisions WHERE verdict='stop' ORDER BY rowid").fetchall()
    assert rows == [("aiflex", "600001"), ("aiflex2", "600002")]
    assert one._stop_of("600002") is None and two._stop_of("600002") == 9.5          # 止损各看各的
    assert ledger.cash("aiflex2_main") < config.FLEX_CAPITAL and ledger.free_slots("aiflex2_main") == config.FLEX_MAX_POSITIONS - 1


def test_missing_or_broken_orders_file_is_harmless(tmp_path):
    conn = db.connect(":memory:")
    s = AiflexStrategy(conn, Ledger(conn), CAL, orders_path=tmp_path / "nope.json")
    assert s.buy_step({"600001": _q("600001")}, NOW) == [] and s.sell_step({"600001": _q("600001")}, NOW) == []
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    s2 = AiflexStrategy(conn, Ledger(conn), CAL, orders_path=tmp_path / "bad.json")
    assert s2.buy_step({"600001": _q("600001")}, NOW) == []


# ---------- 经理的一万元账户（dj 2026-11-11：实盘只有一万块、单票全仓，让经理自己操作，也是对经理的考核） ----------
def _mgr(tmp_path, orders, ts=NOW):
    path = tmp_path / "manager_orders.json"
    path.write_text(json.dumps({"ts": ts.isoformat(timespec="seconds"), "orders": orders}, ensure_ascii=False), encoding="utf-8")
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    return AiflexStrategy(conn, ledger, CAL, orders_path=path, sid="mgr"), ledger, conn


def test_manager_account_is_ten_thousand_all_in_on_one_stock(tmp_path):
    s, ledger, conn = _mgr(tmp_path, [_order(id="MGR-1", pct=1.0), _order(id="MGR-2", code="600002", pct=1.0)])
    assert s.account == "mgr_main" and ledger.cash("mgr_main") == 10000
    fills = s.buy_step({"600001": _q("600001"), "600002": _q("600002")}, NOW)
    assert [(f.code, f.shares) for f in fills] == [("600001", 900)]                # 一万 ÷ 10 元：1000 股加手续费超预算，整手向下取 900
    assert fills[0].reason.startswith("经理｜")
    why = [d for v, c, d, t in conn.execute("SELECT verdict, code, detail, trace FROM decisions WHERE strategy='mgr'") if v == "order_reject"]
    assert len(why) == 1 and "持仓已满（1 只）" in why[0]


def test_manager_account_cannot_afford_a_lot_of_a_stock_above_a_hundred_yuan(tmp_path):
    s, ledger, conn = _mgr(tmp_path, [_order(id="MGR-1", pct=1.0, stop=140.0, ref_price=150.0)])
    assert s.buy_step({"600001": _q("600001", price=150.0, pre=150.0)}, NOW) == []
    assert "资金不足一手" in conn.execute("SELECT detail FROM decisions WHERE strategy='mgr' AND verdict='order_reject'").fetchone()[0]


def test_trader_accounts_keep_their_own_limits(tmp_path):
    s, _, _ = _setup(tmp_path, [_order(pct=0.9)])
    assert (s.max_pct, s.max_positions) == (config.FLEX_MAX_PCT, config.FLEX_MAX_POSITIONS)


# ---------- 越涨越卖（dj 2026-11-11：「本来盈利 20%，一轮下跌硬拿着，最后 -3% 割肉，白白浪费几天。正确做法是越涨越卖」） ----------
D2 = datetime(2026, 11, 4, 9, 45)


def _held(tmp_path, shares_budget=10100, sid="aiflex"):
    """前一天 10 元买入、AI 自设止损 9.5 的一笔持仓（默认 1000 股）。"""
    s, ledger, conn = (_setup(tmp_path, []) if sid == "aiflex" else _mgr(tmp_path, []))
    ledger.buy(s.account, _q("600001", ts=NOW), NOW, "x", budget=shares_budget)
    s._log(NOW, "stop", "600001", "止损 9.5", {"code": "600001", "stop": 9.5, "invalid": "跌破 9.5"})
    return s, ledger, conn


def _px(price, pre, minute=0):
    t = D2 + timedelta(minutes=minute)
    return {"600001": _q("600001", price=price, pre=pre, ts=t)}, t


def test_ledger_can_sell_part_of_a_position_and_keeps_the_rest_at_the_same_buy_price(tmp_path):
    s, ledger, _ = _held(tmp_path)
    pos = ledger.positions(ACCOUNT)[0]
    assert pos.shares == 1000
    f = ledger.sell_part(ACCOUNT, pos, _q("600001", price=11.0, pre=10.5, ts=D2), D2, "分批", 300)
    left = ledger.positions(ACCOUNT)[0]
    assert f.shares == 300 and f.side == "sell" and left.shares == 700 and left.buy_price == 10.0
    assert left.cost == pytest.approx(pos.cost * 0.7, abs=0.01)
    whole = ledger.sell_part(ACCOUNT, left, _q("600001", price=11.0, pre=10.5, ts=D2), D2, "清掉", 9999)
    assert whole.shares == 700 and ledger.positions(ACCOUNT) == []


def test_sells_a_third_at_each_profit_tier_then_trails_the_rest_from_the_high(tmp_path):
    s, ledger, _ = _held(tmp_path)
    q, t = _px(10.85, 10.4)                                   # +8.5%：第一档，卖 1/3
    f1 = s.sell_step(q, t)
    assert [(f.shares, f.side) for f in f1] == [(300, "sell")] and "分批止盈" in f1[0].reason
    assert s.sell_step(*_px(10.9, 10.4, 1)) == []             # 同一档不重复卖
    f2 = s.sell_step(*_px(11.6, 10.9, 2))                     # +16%：第二档，再卖 1/3
    assert [f.shares for f in f2] == [300]
    assert s.sell_step(*_px(12.0, 11.2, 3)) == []             # 继续涨：剩下的拿着，最高价 12
    assert s.sell_step(*_px(11.5, 11.2, 4)) == []             # 回撤 4.2%，还没到 5%
    f3 = s.sell_step(*_px(11.35, 11.2, 5))                    # 从 12 回撤超过 5%：剩下的全走，锁住 +13.5%，不等回到 9.5
    assert [f.shares for f in f3] == [400] and "移动止盈" in f3[0].reason and ledger.positions(ACCOUNT) == []


def test_once_five_percent_up_it_never_turns_into_a_loss(tmp_path):
    s, ledger, _ = _held(tmp_path)
    assert s.sell_step(*_px(10.6, 10.3)) == []                # +6%：没到第一档，但止损已经上移到成本
    assert s.effective_stop(ledger.positions(ACCOUNT)[0]) == pytest.approx(10.07, abs=0.01)   # 10.6 回撤 5% = 10.07，高于成本
    sold = s.sell_step(*_px(10.05, 10.3, 1))
    assert len(sold) == 1 and sold[0].shares == 1000 and "移动止盈" in sold[0].reason
    cold, ledger2, _ = _held(tmp_path)                        # 从没涨到 +5% 的：还是按 AI 自己定的 9.5
    assert cold.sell_step(*_px(9.8, 10.0)) == [] and cold.effective_stop(ledger2.positions(ACCOUNT)[0]) == 9.5


def test_the_high_is_tracked_on_the_buy_day_even_though_nothing_can_be_sold_then(tmp_path):
    s, ledger, _ = _held(tmp_path)
    same_day = NOW + timedelta(minutes=30)
    assert s.sell_step({"600001": _q("600001", price=10.9, pre=10.0, ts=same_day)}, same_day) == []      # T+1：当天卖不了
    sold = s.sell_step(*_px(10.3, 10.9))                      # 第二天开在 10.3：已经从 10.9 回撤超过 5%
    assert len(sold) == 1 and "移动止盈" in sold[0].reason


def test_a_single_lot_cannot_be_split_so_the_managers_account_only_trails(tmp_path):
    s, ledger, _ = _held(tmp_path, shares_budget=1100, sid="mgr")
    assert ledger.positions("mgr_main")[0].shares == 100
    assert s.sell_step(*_px(11.6, 10.9)) == []                # 过了两档，但 100 股拆不开
    sold = s.sell_step(*_px(11.0, 10.9, 1))
    assert [f.shares for f in sold] == [100] and "移动止盈" in sold[0].reason


def test_partial_sells_are_not_counted_as_a_fake_loss_by_the_losing_streak_guard(tmp_path):
    s, ledger, _ = _held(tmp_path)
    s.sell_step(*_px(10.85, 10.4))                            # 卖 300 股，赚
    results = s._results()
    assert len(results) == 1 and results[0][1] > 0            # 300 股对 300 股的成本，不是对 1000 股的成本
