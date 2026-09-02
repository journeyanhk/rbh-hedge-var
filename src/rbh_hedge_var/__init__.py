"""rbh-hedge-var — XAU funding-rate hedge (Variational x RHC Lighter).

Phase 1 is a shadow / read-only engine: it fetches public market data,
computes the hedge economics with Decimal precision, drives a persisted
state machine and NEVER sends a real order. Live execution (Phase 2) is
intentionally absent from this package and gated off in config.
"""

__version__ = "0.1.0-phase1"
