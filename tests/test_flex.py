"""AI 灵活交易账户（dj 2026-11-03「要灵活交易，有时候就得突破一下」）：DeepSeek 自己判断买什么、买多少、什么时候卖，
可以突破任何选股规则（标「模式外」），只留保命的护栏：单票 ≤30% 净值、买入必须写止损位和「错了的信号」、
当日亏 3% 当天不再开新仓、连亏 3 笔当天不再开新仓、亏损持仓不加仓、持仓 ≤5 只。止损由 daemon 每分钟盯着执行。
订单由 ai_loop 写进 dashboard/runtime/flex_orders.json，daemon 的执行器按实时行情成交。"""
import json
from datetime import date, datetime, timedelta

import pytest
from factories import position, quote

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


def test_single_stock_is_capped_at_thirty_percent_of_equity(tmp_path):
    s, ledger, _ = _setup(tmp_path, [_order(pct=0.9)])
    fills = s.buy_step({"600001": _q("600001")}, NOW)
    assert fills[0].shares == 3500                                                                    # 12 万 × 30% ÷ 10 元（含手续费、整手）


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


def test_at_most_five_positions_and_no_adding_to_a_losing_position(tmp_path):
    s, ledger, conn = _setup(tmp_path, [_order(code="600006")])
    for i in range(1, 6):
        ledger.buy(ACCOUNT, _q(f"60000{i}"), NOW - timedelta(days=1), "x", budget=10000)
    qs = {f"60000{i}": _q(f"60000{i}") for i in range(1, 7)}
    assert s.buy_step(qs, NOW) == []                                                                  # 第 6 只
    assert "持仓已满" in _decisions(conn)[0][2]
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
