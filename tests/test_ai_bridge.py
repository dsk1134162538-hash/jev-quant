"""AI 桥接无真实委托测试；可用 unittest 或 pytest 运行。"""
import unittest
from datetime import date, datetime

from jevq import config, db
from jevq.live import store as live_store
from jevq.live.ai_bridge import CURSOR_KEY, sync_ai_fills
from jevq.live.broker import BrokerError
from jevq.live.executor import Executor
from jevq.live.store import LivePosition
from tests.factories import quote
from tests.live_fakes import FakeBroker, FakeClock, FakeNotifier


class AIBridgeTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.clock = FakeClock(datetime(2026, 10, 26, 9, 35))
        self.broker = FakeBroker(self.clock, cash=50_000)
        self.executor = Executor(self.conn, self.broker, FakeNotifier(),
                                 clock=self.clock.now, sleep=self.clock.sleep,
                                 quotes=lambda codes: {code: quote(code=code) for code in codes})
        self.executor._last_reconcile = self.clock.now()

    def tearDown(self):
        self.conn.close()

    def ready(self, mode="dryrun"):
        live_store.set_mode(self.conn, mode)
        live_store.set_state(self.conn, "trading_day", self.clock.now().date().isoformat())
        live_store.set_state(self.conn, "self_check_day", self.clock.now().date().isoformat())

    def fill(self, side="buy", code="600001", price=10.0, shares=500, ts=None):
        cur = self.conn.execute(
            "INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
            "VALUES (?, ?, ?, ?, ?, 5.0, ?, 'AI测试')",
            (config.AI_ACCOUNT, code, side, price, shares, (ts or self.clock.now()).isoformat()))
        self.conn.commit()
        return cur.lastrowid

    def test_sync_is_idempotent_and_only_reads_ai_account(self):
        self.ready()
        fill_id = self.fill()
        self.conn.execute(
            "INSERT INTO fills(account, code, side, price, shares, fee, ts, reason) "
            "VALUES (?, '600002', 'buy', 8, 100, 5, ?, '旧策略')",
            (config.LIVE_ACCOUNT, self.clock.now().isoformat()))
        self.conn.commit()

        self.assertEqual(sync_ai_fills(self.conn, {"600001": "甲公司"}), 1)
        self.assertEqual(sync_ai_fills(self.conn), 0)
        rows = self.conn.execute("SELECT source, code, name, status FROM live_intents").fetchall()
        self.assertEqual(rows, [("ai", "600001", "甲公司", "new")])
        self.assertEqual(live_store.get_state(self.conn, CURSOR_KEY), str(fill_id))

    def test_sync_refuses_non_dryrun_without_writing_cursor_or_intent(self):
        self.ready("live")
        self.fill()
        with self.assertRaisesRegex(RuntimeError, "仅允许 dryrun"):
            sync_ai_fills(self.conn)
        self.assertIsNone(live_store.get_state(self.conn, CURSOR_KEY))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM live_intents").fetchone()[0], 0)

    def test_ai_buy_is_dryrun_capped_and_never_places(self):
        self.ready()
        self.fill(price=10.0, shares=500)
        self.executor.step()

        self.assertFalse([call for call in self.broker.calls if call[0] == "place"])
        order = self.conn.execute("SELECT shares, status FROM live_orders").fetchone()
        self.assertEqual(order[1], "dryrun")
        self.assertLessEqual(order[0], 500)
        self.assertLessEqual(order[0] * 10.05 + 5.05, config.AI_TOTAL_BUDGET)
        position = live_store.positions(self.conn)["600001"]
        self.assertEqual(position.sim_account, config.AI_ACCOUNT)
        self.assertLess(order[0], 500)  # 单笔含费预算和限价滑点共同约束，不能沿用旧账户全仓口径

    def test_ai_intent_in_live_is_rejected_before_any_broker_read_or_place(self):
        self.ready("live")
        self.conn.execute(
            "INSERT INTO live_intents(ts, source, side, code, name, sim_price, sim_shares, reason, status) "
            "VALUES (?, 'ai', 'buy', '600001', '甲', 10, 100, '人工注入', 'new')",
            (self.clock.now().isoformat(),))
        self.conn.commit()
        self.executor.step()

        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.conn.execute("SELECT status, note FROM live_intents").fetchone(),
                         ("skipped", "AI 桥接尚未启用实盘"))

    def test_ai_position_in_live_is_not_sold(self):
        self.ready("live")
        live_store.put_position(self.conn, LivePosition(
            "600001", 100, 1005, 10, date(2026, 10, 23), 10, config.AI_ACCOUNT))
        self.broker.holdings = {"600001": [100, 100]}
        self.executor.step()

        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0], 0)
        self.assertIn("600001", live_store.positions(self.conn))

    def test_ai_sell_alignment_does_not_confuse_old_account_position(self):
        self.ready()
        buy_date = self.clock.now().date()
        self.fill("buy", shares=200)  # 为限价滑点预留预算，演练成交 100 股
        self.conn.execute(
            "INSERT INTO positions(account, code, shares, cost, buy_price, buy_date) "
            "VALUES (?, '600001', 100, 1005, 10, ?)", (config.AI_ACCOUNT, buy_date.isoformat()))
        self.conn.commit()
        self.executor.step()
        self.assertEqual(live_store.positions(self.conn)["600001"].sim_account, config.AI_ACCOUNT)

        self.clock.sleep(24 * 60 * 60)
        live_store.set_state(self.conn, "trading_day", self.clock.now().date().isoformat())
        live_store.set_state(self.conn, "self_check_day", self.clock.now().date().isoformat())
        self.executor._last_reconcile = self.clock.now()
        self.conn.execute("DELETE FROM positions WHERE account=?", (config.AI_ACCOUNT,))
        self.conn.execute(
            "INSERT INTO positions(account, code, shares, cost, buy_price, buy_date) "
            "VALUES (?, '600001', 100, 1005, 10, ?)",
            (config.LIVE_ACCOUNT, buy_date.isoformat()))  # 旧账户仍持有，不应阻止 AI 自己的卖出对齐
        self.conn.commit()
        self.fill("sell", shares=100)

        self.executor.step()
        self.assertFalse([call for call in self.broker.calls if call[0] == "place"])
        sell = self.conn.execute("SELECT status FROM live_orders WHERE side='sell'").fetchone()
        self.assertEqual(sell, ("dryrun",))
        self.assertEqual(live_store.positions(self.conn), {})
        note = self.conn.execute(
            "SELECT note FROM live_intents WHERE side='sell' AND source='ai'").fetchone()
        self.assertEqual(note, ("按持仓对齐卖出",))

    def test_old_position_null_account_falls_back_to_old_account(self):
        live_store.put_position(self.conn, LivePosition(
            "600001", 100, 1005, 10, date(2026, 10, 23), 10))
        self.conn.execute("UPDATE live_positions SET sim_account=NULL")
        self.conn.commit()
        self.assertEqual(live_store.positions(self.conn)["600001"].sim_account, config.LIVE_ACCOUNT)

    def test_put_position_refuses_to_overwrite_other_accounts_holding(self):
        live_store.put_position(self.conn, LivePosition("600001", 100, 1005, 10, date(2026, 10, 20), 10))
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            live_store.put_position(self.conn, LivePosition(
                "600001", 200, 2005, 10, date(2026, 10, 21), 10, config.AI_ACCOUNT))
        held = live_store.positions(self.conn)["600001"]
        self.assertEqual((held.shares, held.sim_account), (100, config.LIVE_ACCOUNT))
        # 同账户加仓/改写仍然允许
        live_store.put_position(self.conn, LivePosition("600001", 300, 3005, 10, date(2026, 10, 21), 10))
        self.assertEqual(live_store.positions(self.conn)["600001"].shares, 300)

    def test_ai_buy_skips_code_held_by_other_account(self):
        self.ready()
        old = config.LIVE_MAX_POSITIONS
        config.LIVE_MAX_POSITIONS = 2
        self.addCleanup(setattr, config, "LIVE_MAX_POSITIONS", old)
        live_store.put_position(self.conn, LivePosition("600001", 100, 1005, 10, date(2026, 10, 20), 10))
        self.conn.execute("INSERT INTO positions(account, code, shares, cost, buy_price, buy_date) "
                          "VALUES (?, '600001', 100, 1005, 10, '2026-10-20')", (config.LIVE_ACCOUNT,))
        self.conn.commit()   # 旧策略模拟账户仍持有，否则执行器会把这条演练持仓当成待卖
        self.fill(code="600001", shares=100)
        self.executor.step()
        row = self.conn.execute("SELECT status, note FROM live_intents WHERE source='ai'").fetchone()
        self.assertEqual(row, ("skipped", "这只已被另一个账户持有"))
        self.assertEqual(live_store.positions(self.conn)["600001"].sim_account, config.LIVE_ACCOUNT)

    def test_ai_intent_gets_name_from_quote(self):
        self.ready()
        self.fill(code="600001", shares=100)
        self.executor.quotes = lambda codes: {c: quote(code=c, name="甲公司") for c in codes}
        self.executor.step()
        self.assertEqual(self.conn.execute("SELECT name FROM live_intents WHERE source='ai'").fetchone()[0],
                         "甲公司")

    def test_ocr_read_failures_auto_pause_without_place(self):
        self.ready()
        self.fill(shares=100)

        def ocr_failure():
            self.broker.calls.append(("positions",))
            raise BrokerError("验证码识别失败", "OCR_FAILED")

        self.broker.positions = ocr_failure
        for _ in range(config.LIVE_READ_FAIL_PAUSE):
            self.executor.step()
            self.clock.sleep(1)

        paused = live_store.paused(self.conn)
        self.assertIsNotNone(paused)
        self.assertEqual(paused["kind"], "auto")
        self.assertIn("连续读取失败", paused["reason"])
        self.assertFalse([call for call in self.broker.calls if call[0] == "place"])


if __name__ == "__main__":
    unittest.main()
