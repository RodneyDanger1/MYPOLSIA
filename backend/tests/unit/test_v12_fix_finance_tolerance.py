"""V12/F2 — V1: the tolerance=0 semantics, reconciled BY EVIDENCE (shipped contract wins).

The two disclosed red suites —
`test_r28_finance_tasks_sweep.py::test_stdlib_verify_tolerance_zero_disables_the_window` and
`test_r32_signature_rotation.py::test_tolerance_boundaries_are_not_loosened` — pin
`_stdlib_verify`'s direct `tolerance=0` argument as the documented escape hatch that skips the
age check.  Evidence that this IS the shipped contract (full write-up in the V12/F2 receipt):

  1. both suites are SHIPPED (R28/R32 waves) — R32 explicitly re-affirms the escape hatch right
     after its own accept-any fix ("the accept-any fix must not have touched the escape hatch
     either");
  2. the installed stripe SDK (11.3.0) does the same: `stripe/_webhook.py:89`
     `if tolerance and timestamp < time.time() - tolerance` — a falsy tolerance performs NO age
     check, so the fallback's falsy-skip was deliberate SDK parity;
  3. the pre-W-C code implemented exactly `if tolerance and abs(int(time.time()) - signed_at)
     > tolerance` (lane pre/finance.py.pre-v12, line 177).

The audit F21 concern ("configured 0 ⇒ any-age signature accepted") is about the CONFIGURED
path; it is KEPT, normalized at the ENTRY POINTS (`_signature_tolerance` + the
`construct_stripe_event` window), which no shipped test contradicts.  The residual seam-level
concern is pinned as the xfail REQUEST below — never by breaking a shipped pin.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from types import SimpleNamespace

import pytest

from app.api.v1 import finance as finance_api

SECRET = "whsec_v1_f2"
DEFAULT = finance_api.DEFAULT_SIGNATURE_TOLERANCE


def _sign(body: bytes, *, ts: int, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def test_the_direct_seam_keeps_its_shipped_escape_hatch() -> None:
    """Mirror of the shipped R28 pin: `tolerance=0` on the low-level verifier skips the age
    check (the clock-skewed-host escape hatch), while a positive tolerance still refuses."""
    body = b'{"id": "evt_old_but_allowed"}'
    old = _sign(body, ts=int(time.time()) - 100_000)
    assert finance_api._stdlib_verify(body, old, SECRET, 0)["id"] == "evt_old_but_allowed"
    with pytest.raises(finance_api.HTTPException):
        finance_api._stdlib_verify(body, old, SECRET, DEFAULT)


def test_the_entry_points_still_normalize_a_zero_to_the_default(monkeypatch) -> None:
    """The F21 floor, kept where the audit meant it: a CONFIGURED / caller-supplied 0 at the
    entry points is the default window — the guard can never be disabled there."""
    monkeypatch.setattr(finance_api, "settings", SimpleNamespace(stripe_signature_tolerance=0))
    assert finance_api._signature_tolerance() == DEFAULT

    body = b'{"id": "evt"}'
    old = _sign(body, ts=int(time.time()) - 4000)
    with pytest.raises(finance_api.HTTPException) as refused:
        finance_api.construct_stripe_event(body, old, SECRET, tolerance=0)
    assert "tolerance" in str(refused.value.detail)


def test_the_boundary_semantics_the_r32_suite_pins_are_unchanged(monkeypatch) -> None:
    """Exactly `tolerance` away is INSIDE; one second more is outside — both directions, and
    the falsy seam is not a hole in the boundary (frozen clock, the R32 pin's own shape)."""
    class _FrozenTime:
        @staticmethod
        def time() -> float:
            return 10_000.0

    monkeypatch.setattr(finance_api, "time", _FrozenTime)
    body = b'{"id": "evt_edge"}'
    assert finance_api._stdlib_verify(body, _sign(body, ts=9_700), SECRET, 300)["id"] == "evt_edge"
    assert finance_api._stdlib_verify(body, _sign(body, ts=10_300), SECRET, 300)["id"] == "evt_edge"
    with pytest.raises(finance_api.HTTPException):
        finance_api._stdlib_verify(body, _sign(body, ts=9_699), SECRET, 300)
    with pytest.raises(finance_api.HTTPException):
        finance_api._stdlib_verify(body, _sign(body, ts=10_301), SECRET, 300)


@pytest.mark.xfail(
    reason=(
        "SECURITY PIN/REQUEST (F21 residual, V12/F2) — the direct `_stdlib_verify(..., 0)` seam "
        "skips the age check BY DESIGN: shipped R28/R32 contract + stripe-SDK parity (see the "
        "module docstring).  REQUEST: flipping this seam to refuse a 0 is a deliberate contract "
        "change that must re-pin BOTH shipped suites in the same commit — it must never land as "
        "a silent floor (that is exactly what broke R28/R32 the first time)."
    ),
    strict=False,
)
def test_pin_request_the_direct_seam_tolerance_zero_would_refuse() -> None:
    body = b'{"id": "evt_pin"}'
    old = _sign(body, ts=int(time.time()) - 100_000)
    with pytest.raises(finance_api.HTTPException):
        finance_api._stdlib_verify(body, old, SECRET, 0)
