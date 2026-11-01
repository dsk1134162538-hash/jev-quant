"""AI 打首板（dj 2026-11-01）：老扫首板的买点 + DeepSeek 开闸 + 单票不亏损卖出。"""
import json
from datetime import date, datetime

from factories import position, quote

from jevq import db
from jevq.ledger import Ledger
from jevq.rules import ai_board
from jevq.rules.board import Candidate
from jevq.strategies.board import BoardStrategy

DAY = datetime(2026, 11, 3)
PREV = date(2026, 11, 2)
AI = ("aiboard_main", "aiboard_shadow")


def at(h, m, d=DAY):
    return d.replace(hour=h, minute=m)


class Judge:
    def check(self, code, *args):
        return "pass", "ok"


def make(gate, legacy_buy=False, q=None):
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    strat = BoardStrategy(conn, ledger, Judge(), Judge(), lambda codes: {"600001": q}, {"600001": "甲"},
                          {"600001": 5.0}, {"600001": ["AI"]}, {"600001": 9.5}, PREV,
                          legacy_buy=legacy_buy, ai_accounts=AI, ai_gate=gate, ai_budget_pct=0.5)
    strat.watch = {"600001": Candidate("600001", "AI", 5, 3, {})}
    strat.verdicts = {"600001": ("pass", "ok", {"outcome": {"bought": False}})}
    return strat, conn, ledger


# ---- 卖出：单票不亏损 ----

def test_loss_is_never_sold_even_when_board_rule_says_stop():
    pos = position(buy_price=10.0, buy_date=PREV)                    # 次日低开 -5%，老规则会「止损」
    q = quote(pct=-6.5, open_pct=-5.0, ts=at(9, 40))
    assert not ai_board.decide_noloss_sell(pos, q, at(9, 40), PREV).sell


def test_board_exit_used_when_not_losing():
    pos = position(buy_price=10.0, buy_date=PREV)
    q = quote(pct=1.0, open_pct=1.0, pre_close=10.5, ts=at(9, 31))   # 竞价不及预期，但 10.6 > 成本
    d = ai_board.decide_noloss_sell(pos, q, at(9, 31), PREV)
    assert d.sell and d.reason == "竞价不及预期（不亏）"


def test_fees_count_selling_at_buy_price_is_still_a_loss():
    pos = position(buy_price=10.0, buy_date=PREV)
    q = quote(pct=0.0, open_pct=0.0, pre_close=10.0, ts=at(9, 31))
    assert ai_board.net_pnl(pos, 10.0) < 0
    assert not ai_board.decide_noloss_sell(pos, q, at(9, 31), PREV).sell
    assert ai_board.breakeven_price(pos) > 10.0 and ai_board.net_pnl(pos, ai_board.breakeven_price(pos)) >= 0


def test_underwater_position_exits_as_soon_as_it_breaks_even():
    pos = position(buy_price=10.0, buy_date=date(2026, 10, 30))      # 拿了好几天，被套
    be = ai_board.breakeven_price(pos)
    q = quote(pct=round((be / 9.8 - 1) * 100, 2), open_pct=-1.0, pre_close=9.8, ts=at(13, 30))
    if q.price < be:                                                  # 百分比四舍五入后差一分就往上补
        q = quote(pct=round((be / 9.8 - 1) * 100, 2) + 0.11, open_pct=-1.0, pre_close=9.8, ts=at(13, 30))
    d = ai_board.decide_noloss_sell(pos, q, at(13, 30), PREV)
    assert d.sell


def test_sealed_limit_up_is_held():
    pos = position(buy_price=10.0, buy_date=PREV)
    q = quote(pct=10.0, open_pct=4.0, pre_close=10.5, ts=at(10, 0))
    assert not ai_board.decide_noloss_sell(pos, q, at(10, 0), PREV).sell


def test_bought_today_is_not_sold():
    pos = position(buy_price=10.0, buy_date=DAY.date())
    assert not ai_board.decide_noloss_sell(pos, quote(pct=20.0, ts=at(14, 0)), at(14, 0), PREV).sell


# ---- 闸门 ----

def _rec(ts, action="允许模拟小仓评估"):
    return {"ts": ts.isoformat(timespec="seconds"), "regime": "进攻观察", "action": action}


def test_gate_closed_without_judgment_or_when_stale_or_defensive():
    assert ai_board.gate(None, "600001", at(9, 40))[0] is False
    assert ai_board.gate(_rec(at(9, 0)), "600001", at(9, 40))[0] is False          # 超过 15 分钟
    assert ai_board.gate(_rec(at(9, 35), "空仓等待"), "600001", at(9, 40))[0] is False
    assert ai_board.gate(_rec(at(9, 35)), "600001", at(9, 40))[0] is True


def test_gate_respects_deepseek_veto_on_that_stock():
    orders = {"600001": {"action": "观察", "reason": "高位换手"}}
    ok, why = ai_board.gate(_rec(at(9, 35)), "600001", at(9, 40), orders)
    assert not ok and "观察" in why
    assert ai_board.gate(_rec(at(9, 35)), "600002", at(9, 40), orders)[0] is True


def test_last_judgment_and_orders_read_files(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text(json.dumps(_rec(at(9, 30), "空仓等待")) + "\n" + json.dumps(_rec(at(9, 35))) + "\n", encoding="utf-8")
    assert ai_board.last_judgment(p)["action"] == "允许模拟小仓评估"
    assert ai_board.last_judgment(tmp_path / "none.jsonl") is None
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"snapshotId": at(9, 35).isoformat(), "portfolio": {"orders": {"600001": {"action": "观察"}}}}),
                 encoding="utf-8")
    assert ai_board.last_orders(a, at(9, 40)) == {"600001": {"action": "观察"}}
    assert ai_board.last_orders(a, at(10, 30)) == {}                                 # 过期


# ---- 策略接线 ----

def test_gate_open_ai_main_buys_half_and_legacy_accounts_stay_flat():
    q = quote("600001", pct=9.2, pre_close=10.0, ts=DAY)
    strat, conn, ledger = make(lambda code, now: (True, "开闸"), q=q)
    fills = strat.on_fast(at(9, 40))
    assert {f.account for f in fills} == set(AI)
    assert not ledger.holds("board_main", "600001") and not ledger.holds("board_shadow", "600001")
    spent = 30000 - ledger.cash("aiboard_main")
    assert 14000 < spent <= 15000


def test_gate_closed_only_shadow_buys_and_skip_logged_once():
    q = quote("600001", pct=9.2, pre_close=10.0, ts=DAY)
    strat, conn, ledger = make(lambda code, now: (False, "空仓避险"), q=q)
    strat.on_fast(at(9, 40))
    strat.on_fast(at(9, 41))
    assert not ledger.holds("aiboard_main", "600001") and ledger.holds("aiboard_shadow", "600001")
    rows = conn.execute("SELECT detail FROM decisions WHERE verdict='skip' AND detail LIKE 'AI 闸门关%'").fetchall()
    assert len(rows) == 1


def test_legacy_buy_still_works_when_enabled():
    q = quote("600001", pct=9.2, pre_close=10.0, ts=DAY)
    strat, conn, ledger = make(lambda code, now: (True, "开闸"), legacy_buy=True, q=q)
    fills = strat.on_fast(at(9, 40))
    assert {f.account for f in fills} == {"board_main", "board_shadow", *AI}


def test_ai_account_loss_held_while_board_main_stops_out():
    strat, conn, ledger = make(lambda code, now: (True, "开闸"))
    y = quote("600001", pct=9.2, pre_close=10.0, ts=at(9, 40, datetime(2026, 11, 2)))
    ledger.buy("board_main", y, at(9, 40, datetime(2026, 11, 2)), "x")
    ledger.buy("aiboard_main", y, at(9, 40, datetime(2026, 11, 2)), "x")
    drop = quote("600001", pct=-7.0, open_pct=-4.0, pre_close=10.92, ts=at(9, 45))
    fills = strat.sell_step({"600001": drop}, at(9, 45))
    assert [f.account for f in fills] == ["board_main"]
    assert ledger.holds("aiboard_main", "600001")


# ---- 给 DeepSeek 的上下文 / 面板 ----

def _db_with_watch(tmp_path):
    from jevq.data import candidates
    path = tmp_path / "jevq.sqlite"
    conn = db.connect(str(path))
    ledger = Ledger(conn)
    candidates.save(conn, DAY.date(), at(9, 40), [{"code": "600001", "name": "甲", "concept": "AI", "heat": 6,
                                                   "pct": 8.1, "verdict": "pass", "why": "ok"}], None)
    ledger.buy("aiboard_main", quote(pct=9.2, ts=at(9, 40, datetime(2026, 11, 2))), at(9, 40, datetime(2026, 11, 2)),
               "x", budget=15000)
    conn.close()
    return path


def test_board_context_gives_deepseek_watch_and_breakeven(tmp_path):
    from dashboard.subscription_bridge import _board_context
    ctx = _board_context(_db_with_watch(tmp_path), DAY.date())
    assert [w["code"] for w in ctx["watch"]] == ["600001"]
    pos = ctx["account"]["positions"][0]
    assert pos["breakeven"] > pos["buyPrice"]
    assert _board_context(tmp_path / "none.sqlite", DAY.date())["watch"] == []


def test_terminal_shows_ai_board_separately_from_legacy(tmp_path):
    from dashboard.terminal import TerminalData
    t = TerminalData(_db_with_watch(tmp_path), tmp_path)
    t.gateway = lambda: {"ok": False, "text": "连不上"}
    out = t.payload({}, {}, None, [])
    assert "aiboard_main" not in {a["id"] for a in out["accounts"]}
    main = out["ai"]["board"]["accounts"][0]
    assert main["id"] == "aiboard_main" and main["positions"][0]["breakeven"] > main["positions"][0]["price"] - 1

