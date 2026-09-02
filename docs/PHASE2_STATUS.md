# Phase 2 status — live execution (built, awaiting review)

Phase 2 code is now in the tree. It stays **inert** until an operator explicitly
disarms the write-guard: `config.live_trading=true`, every `preflight` check
green, and `RBH_HEDGE_LIVE_ARM=I_UNDERSTAND_LIVE_TRADING` set for `run`. Absent
any of these the engine runs exactly as Phase 1 (shadow, no orders).

## What was built

| Module | Role |
|---|---|
| `lighter_signer.py` | `LighterSignerClient` — wraps `lighter-python` `SignerClient`; guarded market orders + cancel; signed-position read via the verified read-only account endpoint. |
| `variational_gateway.py` | `VariationalOrderGateway` — authenticated RFQ (request→accept) taker fills + authenticated positions. |
| `live_executor.py` | `LiveExecutor` — same `open_hedge/close_hedge/mark_to_market` interface as `ShadowExecutor`; Variational leg first on entry with automatic reduce-only rollback if the Lighter hedge fails. |
| `pricing.py` | Pure fill-price maths shared by both executors (MTM parity, no guard coupling). |
| `reconcile.py` | `reconcile_positions` → `{venue: signed_qty}`; fails closed if either venue read fails. |
| `http_util.request_json/post_json` | Write transport; passes through `net_guard.check` first. |
| `engine.py` | Executor selection by persisted `state.shadow`; `_live_positions` now reconciles; `preflight()` readiness table. |
| CLI | `reconcile`, `preflight`; `run` env-gated live arming. |

## Safety invariants (unchanged from Phase 1)

- **Master switch = `net_guard`.** Every mutating path (`LighterSignerClient`,
  `VariationalOrderGateway`, `LiveExecutor`, `http_util`) raises
  `WriteBlockedError` while armed. The `lighter` SDK opens its own socket, so the
  signer client asserts the guard *itself* before touching the SDK.
- **Lazy SDK import.** `lighter-python` is imported only when actually going
  live; Phase 1 and the whole test suite run without it installed.
- **Fail-closed sizing.** Missing size/price decimals raise rather than guess.
- **Fail-closed reconciliation.** A venue read error is a flagged imbalance, not
  a silent "flat" — the watchdog forces a protective exit.
- **Live round is sticky.** `state.shadow=False` is persisted at entry so a live
  round is always closed by the live executor even if live gating flickers.

## ⚠️ REVIEW-REQUIRED before disarming (cannot verify from public data)

1. **Variational private API** — `variational_gateway._DEFAULT_PATHS`
   (`/api/rfq/quote`, `/api/rfq/accept`, `/api/account/positions`) and the
   HMAC-SHA256(timestamp+method+path+body) signing in `_sign` are the
   *documented-but-unverified* shape. Both are config-overridable under
   `variational.paths`. Pin them to the real venue docs and adjust `_sign` if the
   scheme differs.
2. **Lighter SDK surface** — `LighterSignerClient` calls
   `signer.create_market_order(market_index, client_order_index, base_amount,
   avg_execution_price, is_ask, reduce_only)` and `cancel_all_orders()`, and
   treats the return as `(tx, tx_hash, err)`. Confirm against the installed
   `lighter-python` version; adjust the call if the method name/signature moved.
3. **Nonce management** — the SDK handles nonces internally per api_key_index;
   confirm a single in-flight tx per subaccount under the serial tick loop.
4. **Fill confirmation** — current `open_hedge` trusts the gateway's returned
   fill. Before scaling notional, add a private position/fill re-poll on both
   legs (the reconcile plumbing is already here) to prove `rfq_id ≠ fill`.

## How to exercise it safely

```bash
python3 run.py preflight     # readiness table; never disarms/trades
python3 run.py reconcile     # real signed positions on both venues (read-only)
# only when preflight is all-green:
RBH_HEDGE_LIVE_ARM=I_UNDERSTAND_LIVE_TRADING python3 run.py run
```
