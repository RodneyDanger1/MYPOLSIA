"""V12/F2 — D1: `suppressed` is a list/tuple; ANY other shape refuses BY NAME — never a silent send.

RED-first (measured on the pre-fix tree; lane raw/red-d1.txt): `email_compliance._session_free_send`
iterated WHATEVER `preflight["suppressed"]` was.  A bare STRING iterates its CHARACTERS, so the
membership test could never match and the guarded sender put the message on the wire for an
address the list named; a mapping WITHOUT the address behaved the same way (silently unsuppressed).

Contract pinned here (post-fix):
  * list/tuple are the legal shapes — the shipped list path is byte-for-byte unchanged
    (a listed address refuses `E_POLICY_DENIED`; an empty list still sends);
  * a value that DEMONSTRABLY names the address in any reasonable representation (e.g. a mapping
    whose KEYS include it — W-D's hostile-shape floor) still refuses `E_POLICY_DENIED`;
  * every other shape — bare string, int, mapping without the address, absent/None — raises the
    named `E_CONFIG` refusal (same code family the malformed-COMPANY shape refuses with) BEFORE
    the provider is reachable.  The producer (`outreach_service.compliance_preflight`) always
    writes a sorted list, so this floor is only reachable by a malformed/hostile preflight.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.agents import integrations
from app.agents.email_outreach import compliance_send
from app.core.errors import E_CONFIG, E_POLICY_DENIED, PolsiaError
from app.services.email_compliance import send_compliant_email

SUPPRESSED = "optedout@example.com"
OTHER = "keep@example.com"
COMPANY: dict[str, str] = {
    "sender_legal_name": "Acme Robotics LLC",
    "sender_postal_address": "123 Main St, Minneapolis, MN 55401, USA",
    "website_url": "https://acme.example",
}


def _provider(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Recorder standing in for the SendGrid adapter; any captured message is a REAL leak."""
    sent: list[dict[str, Any]] = []

    def fake_send_email(*, to: str, subject: str, body: str) -> dict[str, Any]:
        sent.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "result": {"message_id": "sg-f2-d1"}}

    monkeypatch.setattr(integrations, "send_email", fake_send_email)
    return sent


def _preflight(suppressed: Any) -> dict[str, Any]:
    return {"company": dict(COMPANY), "suppressed": suppressed}


def _preflight_without_suppressed() -> dict[str, Any]:
    return {"company": dict(COMPANY)}


async def _refusal(monkeypatch: pytest.MonkeyPatch, preflight: dict[str, Any], *, to: str = SUPPRESSED) -> PolsiaError:
    sent = _provider(monkeypatch)
    with pytest.raises(PolsiaError) as refused:
        await send_compliant_email(
            None, to=to, subject="s", body="b", preflight=preflight
        )
    assert sent == [], "a refused send reached the provider"
    return refused.value


# ── the defect: bare-string (and other non-list) shapes must not disable suppression ──────────


async def test_a_bare_string_suppressed_refuses_by_name_and_never_sends(monkeypatch):
    """The sharp shape: one plain string — pre-fix it iterated characters and the mail went out."""
    refusal = await _refusal(monkeypatch, _preflight(SUPPRESSED))
    assert refusal.code == E_CONFIG
    assert "suppressed" in refusal.message
    assert refusal.how_to_fix, "a named refusal carries its operator fix"


async def test_non_iterable_shapes_refuse_by_name(monkeypatch):
    refusal = await _refusal(monkeypatch, _preflight(42))
    assert refusal.code == E_CONFIG


async def test_a_mapping_without_the_address_refuses_instead_of_silently_sending(monkeypatch):
    """Pre-fix this mapping iterated its keys, missed, and the send went through silently."""
    refusal = await _refusal(monkeypatch, _preflight({OTHER: True}))
    assert refusal.code == E_CONFIG


async def test_an_absent_suppressed_block_refuses_by_name(monkeypatch):
    """No `suppressed` key at all is NOT 'nothing suppressed' — it is a preflight that cannot
    be trusted, and the producer always writes the list."""
    refusal = await _refusal(monkeypatch, _preflight_without_suppressed())
    assert refusal.code == E_CONFIG


async def test_a_mapping_that_names_the_address_still_suppresses(monkeypatch):
    """The hostile-shape floor (W-D red-team): a mapping whose KEYS include the address is a
    reasonable representation — it must refuse E_POLICY_DENIED, not E_CONFIG, and not send."""
    refusal = await _refusal(monkeypatch, _preflight({SUPPRESSED: True}))
    assert refusal.code == E_POLICY_DENIED


# ── the shipped list path is untouched ────────────────────────────────────────────────────────


async def test_the_shipped_list_path_refuses_a_listed_address(monkeypatch):
    refusal = await _refusal(monkeypatch, _preflight(["  OPTEDOUT@example.com "]))
    assert refusal.code == E_POLICY_DENIED
    assert "suppressed" in refusal.message


async def test_a_tuple_is_a_legal_shape(monkeypatch):
    refusal = await _refusal(monkeypatch, _preflight((SUPPRESSED,)))
    assert refusal.code == E_POLICY_DENIED


async def test_an_empty_list_still_sends(monkeypatch):
    sent = _provider(monkeypatch)
    result = await send_compliant_email(
        None, to=OTHER, subject="s", body="b", preflight=_preflight([])
    )
    assert result.get("ok") is True
    (call,) = sent
    assert call["to"] == OTHER


# ── the agent seam propagates the named refusal (never a plausible-looking ok:False) ──────────


def test_the_agent_seam_propagates_the_named_refusal(monkeypatch):
    sent = _provider(monkeypatch)
    with pytest.raises(PolsiaError) as refused:
        compliance_send.send_outreach_email(
            to=SUPPRESSED, subject="s", body="b", context={"compliance": _preflight(SUPPRESSED)}
        )
    assert refused.value.code == E_CONFIG
    assert sent == []
