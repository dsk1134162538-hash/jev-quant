# Task 3 review findings — fix round 1 (all must be fixed; controller rulings included)

Reviewer repro script (outside repo): C:\Users\Administrator\AppData\Local\Temp\claude\D--claude-jev-quant\98fd1077-1933-4b0a-84cc-cdad3982d637\scratchpad\probe_exec.py — turn each scenario into a regression test.

## Critical 1 — submitted order recorded as "cancelled/0" and then never tracked
- `_wait_fill` (executor.py:193-203) swallows read failures; `_cancel_last_checked` return value ignored (199); `_finalize_buy_order` then writes `cancelled`, intent done, pushes「没成交，已撤单」(178-181) even when the order is still open at the broker or cancel was never sent (repro A2: two trailing read failures; repro B: cancel pre-check mismatch).
- Real cancels are async (「已报待撤」is open): one read after cancel (200-204) may be stale; `_reconcile_sending_row` (258-265) same.
- Fix: write a terminal status ONLY when a successful read shows our order `not open`. Otherwise keep the order `submitted`, add a note, `_auto_pause("委托状态查不到：…")`. `_cancel_last_checked` read failure → auto-pause; when it returns False never claim cancelled. After cancel_last, poll (every LIVE_POLL, up to LIVE_BUY_WAIT) until our order is not open. startup (and later resume) reconcile `submitted` rows as well as `sending`. Test B must assert: order not cancelled, no「没成交」push.

## Critical 2 — the same intent can place a second real order
- C: `place` raising a non-BrokerError escapes (`except BrokerError` at 126) → order stuck `sending`, `buys_today` doesn't count sending, intent still `new` → next second places again (places=2).
- D: `orders_today` raising non-BrokerError (e.g. ValueError parsing '--') escapes `_read` (only catches BrokerError, 288) → order `submitted`, intent `new` → second order.
- E: process dies after place, before finish_intent; restart within 60 s → startup reconciles the row but never finishes `row["intent_id"]` → buys again.
- Fix: right after `add_order(sending)` and before `place`, `finish_intent(intent, "done", "下单中")` (later notes may be updated via finish_intent again), AND at the top of `_buy` skip any intent that already has a live_orders row. `place`: `except Exception` — REJECT_CODES → failed (no pause); INFLIGHT_CODES → keep sending + auto-pause; everything else → single-order reconcile. `_read`: count ANY exception as a read failure. startup: after reconciling a sending/submitted row, finish its intent.

## Important 3 — controller ruling (spec wins over brief)
Unknown place error whose single-order reconcile finds nothing → order `failed` AND `_auto_pause("下单层返回认不出的错误：…")`. Startup `sending` row not found in today's orders → `failed` AND auto-pause. Update `test_buy_unknown_place_error_not_found…` accordingly.

## Important 4 — cancel pre-check identity
executor.py:223-226: when we have an entrust_no, match ONLY by entrust_no; fall back to code/side/price/shares only when entrust_no is None. (Otherwise a user's identical manual order can be cancelled.)

## Controller ruling — dryrun positions
Spec: 演练的持仓按**模拟成交价**记入 live_positions (buy_price = intent.sim_price). Change executor.py:118 and its test.

## Minors (fix them too — same file, cheap)
- Past the fail threshold every failure calls `_auto_pause` again (repro A sent 6 pushes and overwrote a more specific reason): if already auto-paused, don't re-pause/re-notify.
- `_wait_fill`: if our order is already `not open` (废单 / user-cancelled), return early instead of waiting 30 s and mis-pausing.
- No-entrust-no matching uses `o.time >= since` from the local clock: allow 5 s tolerance and only reconcile sending/submitted rows whose ts is today.
- Guard `limit_up <= 0` (treat as unknown: use only the sim-price-based limit).
- Wrong notes: single-order reconcile note says「启动核对：…」, intent note says「券商拒单：…」— use accurate text. The「没成交，已撤单」push lacks the stock name.
- Float `==` price comparisons (210, 225) → `abs(a - b) < 0.005`.
