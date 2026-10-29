"""W5-S1 (sentinel): an approval token minted for a non-operator issuer can never be verified.

The signature covers `issued_to`, but `verify_approval`/`consume_approval` hardcode the operator
scope — so accepting a custom issuer at mint time would hand out a bearer nobody can use. The
honest behaviour is a named refusal; this pins it (and that the default path still works).
"""

import pytest

from app.agents import policy_firewall
from app.core.errors import E_VALIDATION, PolsiaError

SECRET = b"w5-s1-secret"


def test_issue_approval_refuses_a_non_operator_issuer_by_name() -> None:
    with pytest.raises(PolsiaError) as excinfo:
        policy_firewall.issue_approval(
            SECRET, action_fingerprint="f" * 64, issued_to="sentinel-drill"
        )
    assert excinfo.value.code == E_VALIDATION
    assert "operator" in excinfo.value.message
    assert "sentinel-drill" in excinfo.value.message
    assert excinfo.value.how_to_fix


def test_the_default_issuer_still_mints_a_verifiable_token() -> None:
    token = policy_firewall.issue_approval(SECRET, action_fingerprint="a" * 64, ttl_s=60)
    assert token.issued_to == "operator"
    assert policy_firewall.verify_approval(
        SECRET, token.token, action_fingerprint="a" * 64
    )
    # scope is still one fingerprint: a different action never verifies
    assert not policy_firewall.verify_approval(
        SECRET, token.token, action_fingerprint="b" * 64
    )
