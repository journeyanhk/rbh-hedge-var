# Architecture

## Data flow (one tick)

```
Variational supported_assets ─┐
 (annualized rate, 4h)        ├→ strategy.market_snapshot ─→ funding_guard (unit gate)
RHC Lighter orderBookDetails ─┤        │                         │
 + funding-rates + book       ┘        ├→ economics (VWAP, break-even)
                                       ↓
                              state_machine (IDLE/ENTERING/HOLDING/EXITING/COOLDOWN)
                                       ↓
                              shadow_executor (accept→confirm, NO real order)
                                       ↓
                        watchdog (single-leg / drawdown)  →  state.json
                                       ↓
                     monitor (127.0.0.1:8012, read-only)
```

Every outbound request passes `net_guard.check`, which raises on any non-GET
while armed. The guard is armed for the entire Phase 1 lifetime.

## Provenance

| Concern | Source | How used |
|---|---|---|
| Strategy engine, entry/exit shape, state fields | variational-ondo | ported to Python package, corrected |
| RHC Lighter接入 (url, chainId 466324, api_key_index 4..254, integer scaling, endpoints) | grid-bot-wg@dev004-dy signer_worker.py / market.js | knowledge only, rewritten as read-only client |
| `rfq_id`≠fill, VWAP exit, Decimal discipline, go-live gates, state-machine spec | rbh-hedge-v2 | contracts adopted, not code copied |

## Key invariants

1. **Read-only in Phase 1.** `net_guard` blocks writes at the transport layer.
2. **Fail closed on unknown funding unit.** Missing `funding_interval_s` ⇒
   `funding_verified=false` ⇒ live refused.
3. **None ≠ 0.** Uncomputable economics return `None`; callers never coerce to 0.
4. **Deterministic restart.** State transitions are persisted atomically; a
   crash resumes in a well-defined mode.
5. **Auto direction.** Side follows the funding-spread sign each tick.
