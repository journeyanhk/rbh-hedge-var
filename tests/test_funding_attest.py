"""review4 P0-D: funding-settlement attestation validation + gate acceptance."""
from rbh_hedge_var import funding_attest, funding_guard
from rbh_hedge_var.numeric import D


def _hourly_rows(n, start=1_700_000_000, step=3600):
    return [{"timestamp": start + i * step, "amount": "0.5"} for i in range(n)]


def test_validate_settlements_proves_hourly_cadence():
    out = funding_attest.validate_settlements(_hourly_rows(6), expected_interval_s=3600)
    assert out["ok"] is True
    assert out["observed_interval_s"] == 3600
    assert out["samples"] == 6


def test_validate_settlements_rejects_wrong_cadence():
    # 4h gaps but we expect 1h -> outside tolerance
    rows = _hourly_rows(6, step=14400)
    out = funding_attest.validate_settlements(rows, expected_interval_s=3600)
    assert out["ok"] is False
    assert out["observed_interval_s"] == 14400


def test_validate_settlements_needs_min_samples():
    out = funding_attest.validate_settlements(_hourly_rows(2), expected_interval_s=3600)
    assert out["ok"] is False


def test_build_and_validate_attestation_roundtrip():
    att = funding_attest.build_attestation("lighter", 3600, samples=6, detail="ok",
                                           validity_s=100, now=1000)
    assert funding_attest.valid_attestation(att, "lighter", now=1050) is att
    # expired
    assert funding_attest.valid_attestation(att, "lighter", now=2000) is None
    # wrong venue
    assert funding_attest.valid_attestation(att, "variational", now=1050) is None


def test_verify_units_accepts_attested_lighter_interval():
    # Lighter never publishes an interval; the attestation supplies it.
    res = funding_guard.verify_units(
        14400, None, expected_var_s=14400, expected_lighter_s=3600,
        lighter_attested_interval_s=3600)
    assert res.verified is True
    assert res.live_allowed is True
    assert "attestation" in res.reason


def test_verify_units_still_fails_closed_without_attestation():
    res = funding_guard.verify_units(
        14400, None, expected_var_s=14400, expected_lighter_s=3600,
        lighter_attested_interval_s=None)
    assert res.verified is False
    assert res.live_allowed is False


def test_verify_units_attested_mismatch_blocks():
    # attested cadence disagrees with the operator's expected config -> MISMATCH
    res = funding_guard.verify_units(
        14400, None, expected_var_s=14400, expected_lighter_s=3600,
        lighter_attested_interval_s=7200)
    assert res.status == "mismatch"
    assert res.live_allowed is False


def test_amount_plausibility_note():
    rows = [{"timestamp": 1, "amount": "0.5"}, {"timestamp": 2, "amount": "0.6"}]
    note = funding_attest.amount_plausibility(rows, rate=D("0.0001"), notional=D("12000"),
                                              interval_s=3600)
    assert "ratio" in note
