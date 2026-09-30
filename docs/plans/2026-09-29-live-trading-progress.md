# SDD ledger — plan: D:\claude\jev-quant-live\docs\plans\2026-09-29-live-trading.md

Spec: docs/specs/2026-09-29-live-trading-design.md (reachable)
Base: 4338052 (docs commit on live-trading; master 26e5dec)

## Pre-flight scan

| Pair / task | Produces ↔ consumes | Finding |
|---|---|---|
| T1 ↔ T2 | broker.py types (Holding/OrderRow/BrokerError/REJECT_CODES) ↔ live_fakes.FakeBroker | T1 detail not written yet (waits on gateway/easytrader research); T2 needs the types now |
| T2 ↔ T3/T4/T5 | store API, config LIVE_*, live_fakes ↔ executor | names consistent |
| T3 → T4 → T5 | executor.py sequential | T3 marks sell intents done placeholder; T4 replaces; T5 fixes final step order — consistent |
| T6 ↔ T3 | Executor class ↔ live.py main | T6 in track C would lack executor.py — conflict |
| T7 ↔ T10 | both touch deploy.py CODE_PATHS | double edit → merge conflict |
| T8 ↔ T9 | both touch report.py | separate hunks, resolve at T11 |
| T2 ↔ T9 | both touch config.py, run.py | separate hunks, resolve at T11 |
| T1 self | adapters + probe + setup doc | ok once detail written |
| T2 self | tests vs store signatures | ok (FakeNotifier.once sketch is loose; implementer may simplify) |
| T3 self | buy tests vs rules | ok |
| T4 self | sell tests vs rules | ok |
| T5 self | step order vs T3/T4 | ok |
| T6 self | web tests use loopback server | ok (loopback is not network) |
| T7 self | ok |
| T8 self | ok |
| T9 self | grep-first refactor; keep sentiment refresh | ok |
| T10 self | ok |

## Rulings

- Ruling: broker.py interface types are created in Task 2 (not Task 1) — T2's FakeBroker needs them now while T1 waits on research — cost if wrong: T1 may tweak field names, small follow-up edit.
- Ruling: REJECT_CODES initial set = INSUFFICIENT_BALANCE, INSUFFICIENT_SHARES, SHORT_SELLING_FORBIDDEN, T1_RESTRICTION, PRICE_OUT_OF_RANGE, OUTSIDE_TRADING_HOURS, SERVER_CLEARING, ORDER_SUBMIT_FAILED (gateway README: order definitely not submitted); everything else = unknown → post-check — cost if wrong: a rejected order triggers an unnecessary auto-pause, or an unknown one is treated as clean reject.
- Ruling: parallel implementers only across separate worktrees (A live-trading, B lowbuy-removal, C live-web) — user deadline this afternoon — cost if wrong: merge conflicts in T11.
- Ruling: Task 6 moves to track A after T5 (needs Executor); track C = T7, T8 only, branched after T2 — cost if wrong: less parallelism.
- Ruling: only Task 10 adds "live.py" to deploy CODE_PATHS; Task 7 does not touch deploy.py — avoids double edit.

## Progress
- Task 2: dispatched (track A, sonnet, BASE 4338052)
- Task 9: dispatched (track B worktree D:/claude/jev-quant-lowbuy, opus, BASE 4338052)
- Research done: research-gateway.md (pin 1662adf, usable with caveats), research-easytrader.md (pin def5a8e, PyPI stale)
- Ruling: plan amended after research — Task 1 detail written (runs after Task 2); REJECT_CODES widened (VALIDATION_ERROR, ORDER_PRICE_REQUIRED, INPUT_VERIFY_FAILED); new INFLIGHT_CODES (QUEUE_TIMEOUT/TASK_TIMEOUT/TASK_TIMEOUT_RECOVERY_FAILED/TIMEOUT) keep order 'sending' + auto-pause; resume re-runs sending recovery; OrderRow gains cancelled (撤消数量) and open uses it — gateway review shows orders can execute after timeouts and status lives in 备注 — cost if wrong: an extra auto-pause on a timeout that really failed.
- Ruling: primary broker = xiadan-gateway (HTTP isolation, captcha/popup handling), easytrader = fallback; final choice after home probe — cost if wrong: switch config.LIVE_BROKER.
- Task 2: implementer DONE 77e7f8e (757 passed); review dispatched (sonnet) on review-4338052..77e7f8e.diff
- Plan edits committed 44c3177; track C worktree D:/claude/jev-quant-livec branch live-extras from 44c3177
- Ruling: Task 1 implementer dispatched while Task 2 review runs — time pressure; any Task 2 fix round is queued until Task 1's implementer finishes (same worktree) — cost if wrong: a Task 2 finding in broker.py/live_fakes.py needs a small rebase-style follow-up.
- Task 1: dispatched (opus, BASE 44c3177)
- Task 7: dispatched (track C, sonnet, BASE 44c3177)
- 12:5x all four agents (T9 impl, T2 review, T1 impl, T7 impl) died on the account session limit (reset 13:10).
  State at 13:11: live clean at 44c3177; lowbuy has uncommitted test edits only (T9 mid-TDD, no source changes); livec clean.
- Ruling: after the limit hit, run at most 2 agents at a time and prefer sonnet — user cares about quota; Task 1 (probe for this afternoon) first, then T2 review, then resume T9, then T3–T6, T7, T8, T10 — cost if wrong: slower wall clock.
- Task 1: re-dispatched (sonnet, BASE 44c3177)
- Task 2: complete (commits 4338052..77e7f8e, review clean)
- Task 2: minor (deferred): emit() except around add_intents has no conn.rollback() (fold into T3)
- Task 2: minor (deferred): add_remote_sell return id unasserted; reduce_position no-op path untested; report miscount 28 vs 30 constants; broker.py/fakes written test-alongside not red-first
- Ruling: ⚠️ trading_day is written even for once=True on a non-trading day — real gap (live.py uses it as the trading-day gate) — fold the one-line fix (write only when the calendar says trading) + test + emit rollback into the Task 3 dispatch, reviewed with Task 3 — cost if wrong: none beyond a mixed-scope diff.
- Task 9: resumed original implementer (context intact) after rate limit
- Task 9: implementer DONE_WITH_CONCERNS 5849263 (663 passed); review package review-t9-4338052..5849263.diff
- Ruling: engine.halted is now unread (only low-buy checked it; rebound/board/custom never did) — keep behaviour, reword the 「今日暂停开新仓」 alert to what actually happens — changing live-mirrored rebound behaviour is out of scope — cost if wrong: rebound keeps buying after a 3-minute quote outage, as it always has.
- Task 9: complete (commits 4338052..5849263 on lowbuy-removal, review clean)
- Task 9: minor (deferred→folded into T10): report.py:85 prints None for legacy decision rows with strategy NULL; halted alert wording (ruling above)
- Task 9: minor (deferred): install_shortcut.py NAME still 「Jev低吸模拟盘」 (kept deliberately so re-install doesn't duplicate the shortcut)
- Task 10: dispatched (track B, sonnet, BASE 5849263) with the two folded fixes
- Task 10: implementer DONE 425a244 + folded fixes 7ff2b5d (665 passed); review package review-t10-5849263..7ff2b5d.diff
- Task 10: complete (commits 5849263..7ff2b5d on lowbuy-removal, review clean)
- Task 10: minor (deferred, final review to triage): deploy.py:153 git diff failure → changed=[] → treated as "no code change" → rebase+push without tests (pre-existing pattern, now decides whether tests run)
- Track B done (T9, T10).
- Task 7: re-dispatched (track C livec, sonnet, BASE 44c3177)
- Task 7: implementer DONE_WITH_CONCERNS dd67f40 (766 passed; watchdog_start injected, real starter wired only in __main__; trailer says Sonnet 5 — acceptable)
- Task 1: second attempt stalled (0-byte output for 58 min) — stopped at 14:10; third dispatch (sonnet, BASE 44c3177)
- Task 7: review dispatched (sonnet) on review-t7-44c3177..dd67f40.diff
- Task 7: complete (commits 44c3177..dd67f40 on live-extras, review clean)
- Task 7: minor (deferred): daemon.py:92 watchdog thread's db.connect is outside try → thread could die silently; notify wording invented (心跳缺失); ctypes without argtypes
- Task 8: dispatched (track C, sonnet, BASE dd67f40)
- 09-30: Task 1 committed 3ffa295 (agent died before report? report exists); controller fix 732610e (side validation, setup step 0); pushed live-trading (Tasks 1,2). Task 8 committed 5dd447a on live-extras (no report). Task 1 review pending.
- 09-30 09:10 user: stop company daemon now, one-click home install, keep going. Done: daemon killed (2 procs), startup shortcut renamed .lnk.disabled, DB snapshot (integrity ok, 71 close days) gz 9.9MB at D:/claude/jevq-migrate/; push of DB to GitHub DENIED by permission classifier (sensitive data) → user must carry it or push it themselves.
- Controller-written: scripts/home_install.py + tests (386cc26), pushed live-trading.
- Ruling: Task 1 and Task 8 skip per-task review (Task 1 agent died after commit; controller read probe/gateway and fixed side validation; Task 8 has no report) — both go to the final whole-branch review — quota — cost if wrong: issues found later, before master merge.
- Ruling: fix daily-sync pathspec bug (git commit -- .superpowers fails when nothing under it is tracked → no data pushed since 09-28) in track B: commit exactly the staged data files — blocks home sync after migration — cost if wrong: none.
- migration-data branch pushed (7b011c6) after user chose GitHub route (2nd denial was a transient classifier error). Delete after home install.
- Sync pathspec fix: f8e5a51 on lowbuy-removal (668 passed) — to final review.
- Task 3: dispatched (sonnet, BASE 386cc26)
- Task 3: implementer DONE fd4081d b9aae26 (854 passed); concerns: read-failure policy split, startup reconcile seam left for T5
- Task 6: dispatched in new worktree D:/claude/jev-quant-liveweb branch live-web from b9aae26 (parallel with T3 review; T6 doesn't touch executor.py)
- Task 3: review (opus) Needs fixes — C1 untracked submitted order recorded cancelled; C2 same intent can place twice (non-BrokerError place/read, restart); I3 unknown errors don't pause; I4 cancel pre-check falls back to 4-field match when entrust_no known; minors.
- Ruling: I3 — spec wins: unknown place error not found / startup sending not found → failed + auto-pause — cost if wrong: extra pauses needing manual resume.
- Ruling: dryrun positions use sim price (spec) not limit price (brief) — cost: none (rehearsal only).
- Task 3: fix round 1 sent to original implementer (findings file task-3-findings-r1.md; FIX_BASE b9aae26)
- Task 6: implementer DONE 1455ff9 on live-web (886 passed; added store.intents_on, LIVE_RESTART_CHECK; logs to data/live.log)
- Task 6: complete (commits b9aae26..1455ff9 on live-web, review clean)
- Task 6: minor (deferred): no test for same-host-different-port Origin; web_serve() call outside try in live.py main
- Task 3: fix round 1 done 74e2062 (864 passed) — needs scoped re-review over b9aae26..74e2062 against docs/plans/2026-09-29-live-trading-t3-findings-r1.md
- 09-30: session moved to cloud. Branches on origin: live-trading (T1,T2,T3+fix, installer), lowbuy-removal (T9,T10, sync pathspec fix f8e5a51), live-extras (T7 watchdog, T8 report 5dd447a — T8 has no report and no review), live-web (T6 1455ff9, reviewed clean). migration-data = DB snapshot for home (delete after home install).
- 09-30 cloud: working branch is live-trading-6luqxt (session-designated; started at live-trading afb0dc7). Tests run on Linux with a python3.12 venv (864 passed at afb0dc7).
- Task 3: scoped re-review of b9aae26..74e2062 (controller, opus): C2, I3, I4, dryrun ruling and most minors fixed. Still open → fix round 2 by controller:
  R2-1 no polling after cancel_last (「已报待撤」is open → every unfilled real buy would auto-pause) → poll LIVE_CANCEL_WAIT (new const, 30 s); R2-2 startup reconcile ignored the row's stored entrust_no (could latch onto the user's identical later order and cancel it); R2-3 _auto_pause returned on a timed manual pause (unexpected situation silently auto-resumed) → upgrade to auto; R2-4 "last open order" by list position (gateway row order undocumented) → by time; R2-5 _wait_fill counted polls not elapsed time (~66 s with 6 s gateway reads); R2-6 unclear place error + order found open → cancelled immediately instead of waiting for fill; plus cancel pre-check no longer pauses when our order is already closed.
- Ruling: startup/resume reconcile only today's sending/submitted rows; earlier-day leftovers are Task 5's (mark them, let position reconcile fix the book) — cost if wrong: a stale submitted row lingers.
- Minor (deferred, final review): no-entrust-no field match takes the latest candidate; two identical orders within the 5 s window are ambiguous.
- Task 3: fix round 2 committed 43014d9 (871 passed) — Task 3 complete (controller-reviewed; final review covers it again).
- Task 4: brief docs/plans/2026-09-29-live-trading-t4-brief.md. Rulings in it: sell-intent notes decided before alignment (「程序没有这只 / 当天买的，下个交易日卖 / 按持仓对齐卖出」 replaces 「已卖」); dryrun sells not capped by client available; every non-finishing sell attempt waits LIVE_SELL_RETRY (gateway reads ~6 s); sell rejects → failed + once-a-day notice + retry, no pause; qty rounded down to 100 (gateway only takes lots), odd lot → notify user; after 15:00 one settle read makes today's submitted sells final; startup never cancels an open sell row; remote intents stay new until Task 5 — cost if wrong: small behaviour tweaks in Task 5.
- Task 4: implementer (sonnet) DONE_WITH_CONCERNS 68c8629 (914 passed; 6 safer deviations accepted: retry measured from deferral, quote before client read, window re-check per round, never place while a sending sell exists, row-before-position booking, stop loop on auto-pause; trailer names Sonnet — acceptable).
- Task 4: review (controller, opus): logic sound; fixed by controller: (1) cancel_last() raising escaped step() → submitted order left untracked, no pause (Task 3 path too) → treat as "maybe sent" and poll; (2) cancels during lunch / closing auction are refused → new LIVE_CANCEL_WINDOWS + LIVE_ORDER_MARGIN (20 s): buys skipped when wait+cancel can't finish before 11:30/14:57 (「临近休市，来不及等成交和撤单」), morning sells wait for 13:00, afternoon sells near 14:57 are left for the closing auction, _cancel_and_confirm refuses outside continuous trading and auto-pauses; LIVE_NO_CANCEL_AFTER removed; (3) close settle at LIVE_SELL_SETTLE 15:01 (auction fills may lag).
- Ruling: last ~50 s before 11:30 / 14:57 no new buys — spec deviation for safety — cost if wrong: an occasional late sim signal not followed.
- Minor (deferred, final review): a morning sell left open by a slow gateway is only tracked, never re-priced in the afternoon.
- Task 5: implementer (sonnet) DONE_WITH_CONCERNS 604a6e1 (976 passed). Deviations accepted (all safer): sell-all wrap-up before the buy loop; no already-expired pause after 15:00; _sell_round stops when paused mid-attempt; stale reduce:* keys dropped; sell-all never overwrites an existing pause. Test helpers in test_live_buy/sell pre-mark self-check and last reconcile.
- Task 5: review (controller, opus) — fixed by controller: remote sell-all survived only until the weekend (non-trading-day skip dropped it) → kept; close summary also while paused (DB only); paused 09:05 self-check says 「暂停中…没读客户端」; startup/resume reconcile stops at the first auto-pause (no further cancels/reads).
- Note for final review: store.pause single commit does not isolate readers on the same shared connection (same connection sees uncommitted writes) — web + executor share one sqlite connection.
- Task 11: merged origin/lowbuy-removal (only conflict tests/test_run.py: both sides appended tests, kept both — rerere), origin/live-extras, origin/live-web → 987 passed. Post-merge fixes: ac6e0c2 (web /api/pause 409 while auto-paused — phone could downgrade an auto pause into a self-expiring manual one; deploy: failed `git diff running..origin/master` now counts as changed → tests run first); 9d7e0e2 (report live section: dryrun rows count during rehearsal, live ignores rehearsal rows, sell slippage vs the sim's sell intent since sells carry no intent); heartbeat written before every broker read (a 3-round sell can exceed LIVE_WATCHDOG_STALE → watchdog would kill live.py mid-sell). 990 passed.
- Final review (opus) → docs/plans/2026-09-29-live-trading-final-review.md: 1 Critical, 6 Important, 9 Minor, 4 home checks. Verdict: rehearsal OK from 10-08; live not before fixes + home checks.
- Final fixes, executor part (controller): C-1 polling stops early enough to cancel inside continuous trading (LIVE_READ_EST 10 s) for buys and morning sells (afternoon sells may run into the closing auction), fresh-clock re-check before placing; after-close settle now books open buys too (_settle_after_close, _book_buy_row), resume/startup after close books instead of cancelling; I-2 earlier-day sending/submitted rows → new status `unknown`, auto-pause with a clear list (not silent); I-4 leaving dryrun (to off or live) clears rehearsal positions, entering dryrun with real positions refused; I-5 an open non-queue sell older than LIVE_SELL_WAIT is cancelled and re-priced once it can be cancelled (queue rows noted 「跌停排队」 are left alone); home-check 2 guard: positions bought today are never reduced by reconcile; home-check 3: match tolerance LIVE_MATCH_TOLERANCE 30 s, entrust numbers already owned by other rows excluded, latest by time, learned entrust_no written back; Minor 5 odd-lot text. 1006+ passed.
- Ruling: new live_orders status `unknown` (earlier-day order we can no longer read) — spec list extended — cost if wrong: none (not counted anywhere).
- Final fixes, peripheral part: sonnet in worktree scratchpad/wt-periph, branch final-fixes-periph (I-1, I-3, I-6, Minor 1/2/3/4/6/8/9, broker time/cancel-row parsing, probe two-order cancel test, setup doc).
- Minor 7 (daemon down → live.py silently idle) deferred: no local trading calendar; HANDOFF tells the user to expect the 09:05 self-check push every trading day.
- Final fixes, peripheral part: sonnet DONE 776699f..9aa73f5 on final-fixes-periph (1063 passed); controller review clean; merged. Extras accepted: probe refuses with <120 s left in the session or if B would sit within 2% of the price, prints raw rows (masked) via raw_orders_today(), warns on clock skew; HANDOFF install window 15:30.
- Controller follow-up: resume_requested now carries the reason of the pause the user saw; the page writes it only while paused, the executor clears only that same pause (closes the race the agent flagged). 1084 passed.
