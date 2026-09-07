"""Tests for the read-only monitor's server-side round aggregation.

The dashboard is server-rendered (no framework) so the logic worth testing is
aggregate_rounds: it must tolerate missing/legacy fields, split price vs funding
PnL, bucket verbose exit reasons, and cap the detail table at 20 rows.
"""
import json

from rbh_hedge_var import monitor


def _write_rounds(tmp_path, records):
    state_file = tmp_path / "state.json"
    state_file.write_text("{}")
    ledger = tmp_path / "shadow_rounds.jsonl"
    ledger.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(state_file)


def test_aggregate_empty_ledger(tmp_path):
    sf = tmp_path / "state.json"
    sf.write_text("{}")
    a = monitor.aggregate_rounds(str(sf))
    assert a["total"] == 0
    assert a["win_rate"] is None
    assert a["series"] == []
    assert a["last20"] == []


def test_aggregate_totals_and_splits(tmp_path):
    sf = _write_rounds(tmp_path, [
        {"round_id": 1, "opened_at": 1000, "closed_at": 4600, "reason": "take_profit 1.2 >= 1.0",
         "price_pnl": 0.8, "funding_pnl": 0.4, "pnl": 1.2, "direction": "short_var_long_lighter"},
        {"round_id": 2, "opened_at": 5000, "closed_at": 6800, "reason": "round_stop_loss -2.1 <= -2.0",
         "price_pnl": -2.3, "funding_pnl": 0.2, "pnl": -2.1, "direction": "short_lighter_long_var"},
    ])
    a = monitor.aggregate_rounds(sf)
    assert a["total"] == 2
    assert a["wins"] == 1 and a["losses"] == 1
    assert abs(a["win_rate"] - 0.5) < 1e-9
    assert abs(a["cum_pnl"] - (-0.9)) < 1e-9
    assert abs(a["cum_price_pnl"] - (-1.5)) < 1e-9
    assert abs(a["cum_funding_pnl"] - 0.6) < 1e-9
    # avg hold = (3600 + 1800) / 2 = 2700
    assert abs(a["avg_hold_s"] - 2700) < 1e-9
    # cumulative series
    assert a["series"] == [1.2, -0.9]
    # reason buckets
    assert a["reasons"] == {"take_profit": 1, "round_stop_loss": 1}


def test_aggregate_tolerates_missing_fields(tmp_path):
    # legacy/partial rows (no funding_pnl, no times) must not crash aggregation
    sf = _write_rounds(tmp_path, [
        {"round_id": 1, "pnl": 0.5, "reason": None},
        {"round_id": 2, "price_pnl": 0.3},  # no pnl, no reason
    ])
    a = monitor.aggregate_rounds(sf)
    assert a["total"] == 2
    assert a["avg_hold_s"] is None          # no valid open/close pairs
    assert a["reasons"].get("unknown") == 2  # both bucket to unknown


def test_aggregate_caps_detail_at_20(tmp_path):
    recs = [{"round_id": i, "pnl": 0.1, "opened_at": i, "closed_at": i + 1,
             "reason": "take_profit"} for i in range(30)]
    sf = _write_rounds(tmp_path, recs)
    a = monitor.aggregate_rounds(sf)
    assert a["total"] == 30
    assert len(a["last20"]) == 20
    # newest first
    assert a["last20"][0]["round_id"] == 29
    assert len(a["series"]) == 30


def test_reason_bucket_splits_on_space_and_colon():
    assert monitor._reason_bucket("take_profit 1.2 >= 1.0") == "take_profit"
    assert monitor._reason_bucket("watchdog_naked:something") == "watchdog_naked"
    assert monitor._reason_bucket(None) == "unknown"
    assert monitor._reason_bucket("") == "unknown"


def test_venue_realized_override_wins_over_model_pnl(tmp_path):
    # desgin8: the #5 NFP phantom recorded +4.24 (model); the true venue number
    # is -1.34. Once venue_realized is backfilled, totals must prefer it.
    sf = _write_rounds(tmp_path, [
        {"round_id": 5, "opened_at": 1000, "closed_at": 4600, "reason": "take_profit",
         "price_pnl": 4.24, "funding_pnl": 0.0, "pnl": 4.24, "shadow": False,
         "price_pnl_source": "model", "venue_realized": -1.34},
        {"round_id": 6, "opened_at": 5000, "closed_at": 5900, "reason": "take_profit",
         "price_pnl": -0.51, "funding_pnl": 0.0, "pnl": -0.51, "shadow": False,
         "price_pnl_source": "venue_order"},
    ])
    a = monitor.aggregate_rounds(sf)
    # cum uses -1.34 (override) + -0.51 = -1.85, NOT +4.24 - 0.51
    assert abs(a["cum_pnl"] - (-1.85)) < 1e-9
    assert a["wins"] == 0 and a["losses"] == 2
    # round 5 is now verified (override present) -> not estimated
    assert a["estimated_rounds"] == 0
    r5 = next(r for r in a["last20"] if r["round_id"] == 5)
    assert r5["pnl"] == -1.34 and r5["venue_realized"] == -1.34
    assert r5["estimated"] is False


def test_estimated_live_model_round_flagged_and_bucketed(tmp_path):
    # a LIVE model-priced round with no override is an unreconciled estimate
    sf = _write_rounds(tmp_path, [
        {"round_id": 7, "opened_at": 1, "closed_at": 2, "reason": "take_profit",
         "price_pnl": 0.3, "funding_pnl": 0.0, "pnl": 0.3, "shadow": False,
         "price_pnl_source": "model"},
    ])
    a = monitor.aggregate_rounds(sf)
    assert a["estimated_rounds"] == 1
    assert abs(a["cum_estimated_pnl"] - 0.3) < 1e-9
    assert a["last20"][0]["estimated"] is True


def test_shadow_round_never_estimated(tmp_path):
    sf = _write_rounds(tmp_path, [
        {"round_id": 1, "opened_at": 1, "closed_at": 2, "reason": "take_profit",
         "price_pnl": 0.2, "funding_pnl": 0.0, "pnl": 0.2, "shadow": True},
    ])
    a = monitor.aggregate_rounds(sf)
    assert a["estimated_rounds"] == 0


def test_legacy_live_round_without_provenance_is_estimated(tmp_path):
    # review20 tail (1): a pre-review18 LIVE round has no price_pnl_source at all.
    # It must default to ESTIMATED (suspicious-first), NOT be shown as verified.
    sf = _write_rounds(tmp_path, [
        {"round_id": 3, "opened_at": 1, "closed_at": 2, "reason": "max_hold_elapsed",
         "price_pnl": -0.28, "funding_pnl": 0.0, "pnl": -0.28, "shadow": False},
    ])
    a = monitor.aggregate_rounds(sf)
    assert a["estimated_rounds"] == 1
    assert a["last20"][0]["estimated"] is True


def test_venue_order_source_is_verified(tmp_path):
    # a LIVE round booked on the real fill price is NOT an estimate
    sf = _write_rounds(tmp_path, [
        {"round_id": 7, "opened_at": 1, "closed_at": 2, "reason": "take_profit",
         "price_pnl": 0.3, "funding_pnl": 0.0, "pnl": 0.3, "shadow": False,
         "price_pnl_source": "venue_order"},
    ])
    a = monitor.aggregate_rounds(sf)
    assert a["estimated_rounds"] == 0
    assert a["last20"][0]["estimated"] is False
