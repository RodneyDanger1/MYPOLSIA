"""W1 / AC-04 — the operator kill switch stops the DIRECT external-effect paths too.

THE DEFECT (BUGS.json AC-04, P2): `phone_send_service.send()` checked only
`settings.sandbox_mode`, so with `SANDBOX_MODE=false` an operator's `MERIDIAN_KILL_SWITCH=freeze`
still let an SMS out.  The switch's own documented semantics are fleet-wide ("freeze denies ALL
side effects; reads may continue"), and agent dispatch / the tool gate / every
`request_side_effect` path honour it — the direct owner endpoints were the exception.

THE FIX: `send()` reads the switch FIRST through the one shared reader
(`policy_firewall.require_external_effects_enabled`), before the sandbox check and before any
driver/bridge work.  These tests pin:
  * `freeze` and `throttle` both refuse with the named `E_POLICY_DENIED` and the recipient never
    reaches a transport;
  * the switch is read at CALL time (settings fallback works when the env var is unset), so it
    needs no restart;
  * it holds even when a caller passes `sandbox=False` — the sandbox flag is a deployment switch,
    never a policy override;
  * with the fleet running, the existing sandbox refusal is unchanged (no regression);
  * the refusal maps to HTTP 403 + `X-Meridian-Error-Code` through the shipped `http_refusal`.
"""
from __future__ import annotations

import pytest

from app.agents import policy_firewall
from app.config import settings
from app.core.errors import E_POLICY_DENIED, E_SANDBOX_BLOCKED, MeridianError
from app.services import phone_send_service as phone

E164 = "15551234567"
BODY = "kill switch probe — must never be sent"


class _Transport:
    """Records any transport attempt; the default is empty and every test asserts it stays so."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def __call__(self, *args, **kwargs):  # pragma: no cover - asserted empty
        self.calls.append(("called", args, kwargs))
        raise AssertionError("a phone transport ran under the operator kill switch")


@pytest.fixture
def running_fleet(monkeypatch):
    monkeypatch.delenv("MERIDIAN_KILL_SWITCH", raising=False)
    monkeypatch.setattr(settings, "kill_switch", "")
    return monkeypatch


@pytest.fixture
def transport(monkeypatch):
    """Poison both real transports: the direct driver and the bridge POST."""
    fake = _Transport()
    monkeypatch.setattr(phone, "_run_driver", fake)
    monkeypatch.setattr(phone, "_send_via_bridge", fake)
    monkeypatch.delenv(phone.BRIDGE_URL_ENV, raising=False)
    return fake


# ── the refusal itself ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("grade", ["freeze", "throttle"])
async def test_phone_send_refuses_under_the_kill_switch(
    monkeypatch, transport, grade: str
) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", False)
    monkeypatch.setenv("MERIDIAN_KILL_SWITCH", grade)

    with pytest.raises(MeridianError) as caught:
        await phone.send(E164, BODY)

    assert caught.value.code == E_POLICY_DENIED
    assert caught.value.context["kill_switch"] == grade
    assert "kill switch" in caught.value.message
    assert "MERIDIAN_KILL_SWITCH" in caught.value.how_to_fix
    assert transport.calls == [], "no driver/bridge transport may run under a kill switch"


@pytest.mark.asyncio
async def test_the_kill_switch_beats_an_explicit_sandbox_false(monkeypatch, transport) -> None:
    """`sandbox=False` (the test seam) is a DEPLOYMENT flag, not a policy override."""
    monkeypatch.setattr(settings, "sandbox_mode", True)
    monkeypatch.setenv("MERIDIAN_KILL_SWITCH", "freeze")

    with pytest.raises(MeridianError) as caught:
        await phone.send(E164, BODY, sandbox=False)

    assert caught.value.code == E_POLICY_DENIED
    assert transport.calls == []


@pytest.mark.asyncio
async def test_the_kill_switch_wins_over_the_sandbox_refusal(monkeypatch, transport) -> None:
    """Both controls are armed: the operator's stop is the one reported (it is read first)."""
    monkeypatch.setattr(settings, "sandbox_mode", True)
    monkeypatch.setenv("MERIDIAN_KILL_SWITCH", "freeze")

    with pytest.raises(MeridianError) as caught:
        await phone.send(E164, BODY)

    assert caught.value.code == E_POLICY_DENIED
    assert caught.value.context["kill_switch"] == "freeze"
    assert transport.calls == []


# ── the switch is read at CALL time (env first, then settings) ─────────────────────────────
def test_the_settings_kill_switch_is_read_when_the_env_is_unset(running_fleet) -> None:
    running_fleet.setattr(settings, "kill_switch", "freeze")

    with pytest.raises(MeridianError) as caught:
        policy_firewall.require_external_effects_enabled("probe action")

    assert caught.value.code == E_POLICY_DENIED
    assert caught.value.context["kill_switch"] == "freeze"


def test_a_running_fleet_passes_the_gate(running_fleet) -> None:
    assert policy_firewall.require_external_effects_enabled("probe action") == "run"


# ── no regression on the existing sandbox refusal ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_sandbox_still_refuses_when_the_fleet_runs(
    running_fleet, transport
) -> None:
    running_fleet.setattr(settings, "sandbox_mode", True)

    with pytest.raises(MeridianError) as caught:
        await phone.send(E164, BODY)

    assert caught.value.code == E_SANDBOX_BLOCKED
    assert transport.calls == []


# ── the HTTP mapping the route consumes ───────────────────────────────────────────────────
def test_the_kill_switch_refusal_is_http_403_with_the_code_header(running_fleet) -> None:
    running_fleet.setattr(settings, "kill_switch", "freeze")
    with pytest.raises(MeridianError) as caught:
        policy_firewall.require_external_effects_enabled("phone send")

    http = phone.http_refusal(caught.value)
    assert http.status_code == 403
    assert http.headers["X-Meridian-Error-Code"] == E_POLICY_DENIED
    assert http.detail["code"] == E_POLICY_DENIED
    assert http.detail["how_to_fix"]
