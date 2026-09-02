# Phase 2 plan — live execution

Phase 1 built every non-executing layer and left one clean seam for live orders.
This is the map for closing it. Nothing here is implemented yet.

## The one branch to flip

`engine._do_entry` already computes `live = cfg.live_trading and snap.live_allowed_by_units`.
Today `live` can be True but there is no executor, so it always resolves to
shadow. Phase 2 injects a `LiveExecutor` and routes to it when `live` is True.

## New modules

### `lighter_signer.py` (~300–400 lines)
Wrap `lighter-python` `SignerClient` directly (no JS bridge — we're already in
Python). RHC params, taken from `grid-bot-wg` signer_worker.py and confirmed by
this repo's live probe:
- url `https://api.rh.lighter.xyz`, chainId `466324`
- `account_index` = hedge subaccount, `api_key_index` in 4..254
- methods: `place_ioc_limit(market, side, base_amount_int, price_int, reduce_only)`,
  `cancel_all(market)`, `set_leverage(market, x, isolated=True)`
- integer scaling via `numeric.to_int_scaled(value, decimals)` using the
  market's `supported_size_decimals` / `supported_price_decimals`.
- **serial nonce** manager (one in-flight signed tx at a time for the subaccount).

### `live_executor.py`
Mirror `shadow_executor` interface so `engine` is unchanged apart from the
injection point. Responsibilities:
- open: submit both legs concurrently, then **confirm fills** on both by polling
  client-order-id (Lighter) and private positions (Variational). `rfq_id` is
  *accepted, not filled*.
- rollback: if only one leg confirms within timeout → reduce-only flatten it and
  raise (state machine goes ENTERING→IDLE aborted, watchdog escalates).
- close: Phase 1 taker; illiquid Variational leg first, then Lighter; repost
  limit on timeout (reuse VO's repost-seconds pattern).

### `reconciler.py`
On startup (recovery): "new trading disabled until reconciled". Read both
venues' positions/orders, compare to `state.json` legs, resolve orphans before
allowing IDLE→ENTERING. rbh's `EXECUTION_STATE_MACHINE.md` is the spec.

### `variational_mutation.py`
Variational RFQ request → quote → accept flow (authenticated). This is the
riskiest surface; validate semantics against `rbh-hedge-v2`'s
`variational_frontend_mutation.py` before trusting it.

## Phase 2b — maker optimization
Add passive-limit entry on the Lighter leg (post-only), fill-then-hedge the
Variational leg, with an unfilled-order timeout/cancel. Halves round-trip cost.
Reuse the repost framework.

## Arming
Live orders require, in this order: all `GO_LIVE_CHECKLIST.md` boxes checked →
`live_trading=true` → `funding_verified=true` at runtime → the live executor
calling `net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")`. No other code path may
disarm the guard.
