"""R32 — multi-`v1=` signature acceptance in the stdlib fallback (Stripe secret rotation).

R28 proved F-7: `_stdlib_verify` kept only the LAST `v1=` value while the SDK accepts ANY
matching candidate (`stripe/_webhook.py:81`, `any(secure_compare(expected, s) for s in
signatures)`, stripe 11.3.0 installed here).  This file pins the R32 fix at the internal
boundary: every candidate is tried, each with `hmac.compare_digest`; the timestamp tolerance and
every malformed class are unchanged.  A frozen-clock section pins the tolerance boundaries
exactly (not loosened), and a parity section runs the SAME headers through the real SDK and the
fallback — the two verifiers must agree on accept/refuse for every shape tested here.

Declared residuals (NOT tested, out of scope): the SDK takes the FIRST `t=` while the fallback
keeps the R28 last-wins rule; the SDK's tolerance is one-sided and checked after the signature,
the fallback's is `abs()` and checked first.  Only the v1 multiplicity is in R32's scope.
"""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from fastapi import HTTPException

from app.api.v1.finance import (
    DEFAULT_SIGNATURE_TOLERANCE,
    _parse_signature_header,
    _stdlib_verify,
)

SECRET = "whsec_r32_rotation"
OLD_SECRET = "whsec_r32_rotation_old"  # the pre-rotation secret


def _digest(body: bytes, *, ts: int, secret: str = SECRET) -> str:
    """The documented scheme: HMAC-SHA256 over `"<t>.<raw body>"` — same construction the code
    verifies, no live Stripe involvement."""
    return hmac.new(secret.encode("utf-8"), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def _sign(body: bytes, *, ts: int | None = None, secret: str = SECRET) -> str:
    stamp = int(time.time()) if ts is None else ts
    return f"t={stamp},v1={_digest(body, ts=stamp, secret=secret)}"


def _refusal(call, needle: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        call()
    assert excinfo.value.status_code == 400, excinfo.value.detail
    assert needle in str(excinfo.value.detail), excinfo.value.detail


# ── 1. the parser returns every candidate, in header order ────────────────────────


def test_parser_returns_every_v1_candidate_in_header_order() -> None:
    assert _parse_signature_header("t=123,v1=abc") == ("123", ["abc"])
    assert _parse_signature_header("t=1, v1=deadbeef") == ("1", ["deadbeef"])
    assert _parse_signature_header("scheme=x,t=2,v1=aa") == ("2", ["aa"])
    # R32: secret rotation — ALL v1 values are kept for the accept-any check below.  The R28
    # pin ("the LAST one wins and is the only one tried") was the divergence being fixed.
    assert _parse_signature_header("t=1,v1=first,v1=second") == ("1", ["first", "second"])
    assert _parse_signature_header("t=1,v1=b,v1=a") == ("1", ["b", "a"])  # order preserved
    assert _parse_signature_header("t=1,t=9,v1=a") == ("9", ["a"])  # duplicate t: last wins (R28)
    assert _parse_signature_header("T=1,V1=abc") == (None, [])  # keys are case-sensitive
    assert _parse_signature_header("v1") == (None, [""])  # a bare part carries no v1 value
    assert _parse_signature_header("") == (None, [])


# ── 2. accept-any over the rotated candidates ─────────────────────────────────────


def _rotation_headers(body: bytes) -> dict[str, str]:
    """The task-mandated shapes (plus the two order covers that the old last-wins code refused)."""
    now = int(time.time())
    fresh = _digest(body, ts=now)
    stale = _digest(body, ts=now - 3600)  # right secret, timestamp outside any window
    old = _digest(body, ts=now, secret=OLD_SECRET)  # pre-rotation secret, current timestamp
    return {
        "stale_invalid_then_valid": f"t={now},v1={stale},v1={fresh}",
        "valid_then_garbage": f"t={now},v1={fresh},v1=deadbeef",
        "garbage_then_valid": f"t={now},v1=deadbeef,v1={fresh}",
        "valid_then_stale": f"t={now},v1={fresh},v1={stale}",
        "old_secret_then_valid": f"t={now},v1={old},v1={fresh}",
        "valid_then_old_secret": f"t={now},v1={fresh},v1={old}",
    }


@pytest.mark.parametrize(
    "shape",
    [
        "stale_invalid_then_valid",
        "valid_then_garbage",
        "garbage_then_valid",
        "valid_then_stale",
        "old_secret_then_valid",
        "valid_then_old_secret",
    ],
)
def test_stdlib_verify_accepts_a_matching_v1_wherever_it_sits(shape: str) -> None:
    body = b'{"id": "evt_rot", "type": "charge.succeeded"}'
    payload = _stdlib_verify(
        body, _rotation_headers(body)[shape], SECRET, DEFAULT_SIGNATURE_TOLERANCE
    )
    assert payload == {"id": "evt_rot", "type": "charge.succeeded"}


def test_stdlib_verify_refuses_when_no_v1_matches() -> None:
    now = int(time.time())
    _refusal(
        lambda: _stdlib_verify(
            b"{}", f"t={now},v1=deadbeef,v1=cafebabe", SECRET, DEFAULT_SIGNATURE_TOLERANCE
        ),
        "Invalid Stripe signature",
    )


def test_single_v1_behavior_is_unchanged() -> None:
    body = b'{"id": "evt_single"}'
    assert _stdlib_verify(body, _sign(body), SECRET, DEFAULT_SIGNATURE_TOLERANCE)["id"] == (
        "evt_single"
    )
    now = int(time.time())
    _refusal(
        lambda: _stdlib_verify(body, f"t={now},v1=deadbeef", SECRET, DEFAULT_SIGNATURE_TOLERANCE),
        "Invalid Stripe signature",
    )


def test_empty_v1_candidates_cannot_match_but_do_not_mask_a_valid_one() -> None:
    body = b'{"id": "evt_empty_candidate"}'
    now = int(time.time())
    fresh = _digest(body, ts=now)
    # empty-only: no non-empty candidate -> the header class refusal, same as R28
    _refusal(
        lambda: _stdlib_verify(body, f"t={now},v1=", SECRET, DEFAULT_SIGNATURE_TOLERANCE),
        "Invalid Stripe signature header",
    )
    # an empty candidate next to a valid one: the empty one cannot match, the valid one can
    for header in (f"t={now},v1=,v1={fresh}", f"t={now},v1={fresh},v1="):
        assert _stdlib_verify(body, header, SECRET, DEFAULT_SIGNATURE_TOLERANCE)["id"] == (
            "evt_empty_candidate"
        )


# ── 3. tolerance boundaries, frozen clock: preserved, never loosened ──────────────


def test_tolerance_boundaries_are_not_loosened(monkeypatch: pytest.MonkeyPatch) -> None:
    """`abs(int(time.time()) - t) > tolerance` refuses: exactly `tolerance` away is INSIDE,
    one second more is outside — both directions.  Pinned against a frozen clock so no test
    run can flake at a second boundary."""
    import app.api.v1.finance as finance_module

    class _FrozenTime:
        @staticmethod
        def time() -> float:
            return 10_000.0

    monkeypatch.setattr(finance_module, "time", _FrozenTime)
    body = b'{"id": "evt_edge"}'

    assert _stdlib_verify(body, _sign(body, ts=9_700), SECRET, 300)["id"] == "evt_edge"
    assert _stdlib_verify(body, _sign(body, ts=10_300), SECRET, 300)["id"] == "evt_edge"
    _refusal(
        lambda: _stdlib_verify(body, _sign(body, ts=9_699), SECRET, 300),
        "outside the 300s tolerance",
    )
    _refusal(
        lambda: _stdlib_verify(body, _sign(body, ts=10_301), SECRET, 300),
        "outside the 300s tolerance",
    )
    # the accept-any fix must not have touched the escape hatch either
    assert _stdlib_verify(body, _sign(body, ts=10_000 - 100_000), SECRET, 0)["id"] == "evt_edge"


# ── 4. SDK parity: the two verifiers agree accept/refuse on every shape here ──────


def test_fallback_and_sdk_agree_on_every_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """stripe 11.3.0's `verify_header` accepts ANY matching v1 — the documented behavior the
    fallback now matches.  Both verifiers are run for real (no mocks): the SDK through its own
    `construct_event`, the fallback through `_stdlib_verify`."""
    import stripe

    body = b'{"id": "evt_parity", "type": "charge.succeeded"}'
    now = int(time.time())
    fresh = _digest(body, ts=now)
    stale = _digest(body, ts=now - 3600)
    old = _digest(body, ts=now, secret=OLD_SECRET)
    cases = {
        "single_valid": (f"t={now},v1={fresh}", True),
        "single_invalid": (f"t={now},v1=deadbeef", False),
        "stale_invalid_then_valid": (f"t={now},v1={stale},v1={fresh}", True),
        "valid_then_garbage": (f"t={now},v1={fresh},v1=deadbeef", True),
        "garbage_then_valid": (f"t={now},v1=deadbeef,v1={fresh}", True),
        "valid_then_stale": (f"t={now},v1={fresh},v1={stale}", True),
        "old_secret_then_valid": (f"t={now},v1={old},v1={fresh}", True),
        "valid_then_old_secret": (f"t={now},v1={fresh},v1={old}", True),
        "all_invalid": (f"t={now},v1=deadbeef,v1=cafebabe", False),
        "valid_then_empty": (f"t={now},v1={fresh},v1=", True),
    }

    def sdk_accepts(header: str) -> bool:
        try:
            stripe.Webhook.construct_event(
                body, header, SECRET, tolerance=DEFAULT_SIGNATURE_TOLERANCE
            )
            return True
        except Exception:  # noqa: BLE001 - SignatureVerificationError is the refusal
            return False

    def fallback_accepts(header: str) -> bool:
        try:
            _stdlib_verify(body, header, SECRET, DEFAULT_SIGNATURE_TOLERANCE)
            return True
        except HTTPException:
            return False

    for shape, (header, expected) in cases.items():
        assert fallback_accepts(header) is expected, shape
        assert sdk_accepts(header) is expected, shape
        assert fallback_accepts(header) == sdk_accepts(header), shape
