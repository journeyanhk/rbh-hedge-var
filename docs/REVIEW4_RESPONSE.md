# var-review4 — response & fixes

All fixes stay **behind the `net_guard` master write-guard**. Nothing here
disarms the guard; live orders remain blocked until the three-gate go-live is
passed explicitly.

## P0 (blocking) — all fixed

### P0-A — "accepted ≠ filled": prove fills by position reconciliation
- `variational_gateway.submit_market_order` now returns an **acceptance**
  (`rfq_id`, `status`, `ref_price`, `terminal_ok`) — never a trusted fill.
  `"accepted"`/`"pending"` are excluded from `_TERMINAL_OK`.
- `live_executor.open_hedge` baselines both venues, submits the Variational leg,
  then **confirms the real signed-position delta** (`_confirm_delta`, half-step
  tolerance). It hedges the **actual** Variational fill qty (partial-fill safe),
  confirms the Lighter leg, and backfills **real average fill prices**
  (`avg_entry_price`), not the mark. Any timeout/mismatch → reduce-only flatten
  + `NakedLegError`.

### P0-B — verified token transport as default
- `auth_scheme="token"` (default) uses a Bearer token against
  `/api/quotes/indicative` + `/api/orders/new/market` (returns `rfq_id`).
- `auth_scheme="hmac"` retained as opt-in. Endpoints overridable via
  `variational.paths`. (⚠️ endpoints still REVIEW-REQUIRED before disarm.)

### P0-C — recovery / IDLE reconcile / loud flatten
- `engine._recover(mode)` routes by the **persisted `shadow` flag**. A live
  ENTERING/EXITING **reconciles first**: flat → roll back / finalize; **any
  residual → HALT + Telegram**. Reconcile failure → HALT.
- `engine._idle_flat_check()` reconciles every `idle_reconcile_every_ticks`
  while live; non-flat → HALT.
- `live_executor._flatten` **raises** on failure (never a silent pass).
- `_do_entry` catches `NakedLegError` → HALT + alert (does not roll back to IDLE
  as if nothing happened).

### P0-D — funding attestation
- `funding_attest` proves hourly cadence from real settlements
  (`validate_settlements`) and writes a time-boxed (7-day) attestation.
- `lighter_client.funding_history` pulls the settlement stream
  (⚠️ endpoint REVIEW-REQUIRED).
- New CLI `verify-funding` runs the proof and persists the attestation to state.
- `funding_guard.verify_units` accepts a valid attested interval in lieu of
  Lighter's never-published interval; still MISMATCH/UNVERIFIED otherwise.

## P1 — fixed
- **P1-1** `_live_positions` failure streak: alert once, keep holding, escalate
  to a sentinel imbalance (forces exit) only after N consecutive failures.
- **P1-3** flat check uses half-a-size-step tolerance.
- **P1-5** close ledger line tagged `LIVE`/`SHADOW` by the persisted flag.
- **P1-6** `run` loop wraps each `tick()` in try/except + Telegram; the loop
  never dies silently.

## Tests
95 passing (`pytest`), `ruff` clean. New suites: `test_funding_attest.py`,
`test_engine_recovery.py`; migrated `test_variational_gateway.py`,
`test_live_executor.py`, `test_engine_live.py` to the position-confirmed APIs.
