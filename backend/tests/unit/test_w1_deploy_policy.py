"""W1 / AC-03 + AC-04 — a LIVE Render deploy now runs the shipped policy gate.

THE DEFECT (BUGS.json AC-03, P1): `deploy_service.deploy_site(dry_run=False)` posted to
`api.render.com` with NO `SANDBOX_MODE` check, NO kill-switch read, NO rate cap and NO approval —
even though CLAUDE.md:52 names Render among the APIs the sandbox prevents and `RateCaps` configures
a 3/day render cap.  `api/v1/sites.py` exposed it behind the owner API key only, and the existing
tests encoded "submitted without policy" (they were moved, with an OLD -> NEW note, in
`test_v25_deploy.py`).

THE DETECTED SEQUENCE THESE TESTS PIN, against a labelled MOCKED transport that fails the test if
it is ever asked for a request:
  * `SANDBOX_MODE=true` (the shipped default) -> `E_SANDBOX_BLOCKED`, nothing sent, `meta.json`
    byte-identical (AC-03);
  * operator kill switch `freeze`/`throttle` -> `E_POLICY_DENIED` naming the switch (AC-04), and
    it wins even with the sandbox off, so the two controls are independent;
  * the render day cap is consulted from a MEASURED count — 3 recorded deploys today refuse the
    4th with `E_RATE_LIMITED`, and the count comes from real `channel_send:render` rows;
  * an irreversible deploy needs a single-use approval token bound to
    `deploy_payload(slug, service_id)`; without it `E_POLICY_DENIED` carries the fingerprint and
    the way to mint the token, and a token minted for ANOTHER site does not verify;
  * only a valid token lets the single POST happen, and that real deploy is then counted.

The route-level tests use the real FastAPI app: the refusals are `403` with the code in the body
AND `X-Meridian-Error-Code`, and a refusal leaves the site's `meta.json` untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.agents import policy_firewall
from app.api.v1 import sites as sites_module
from app.config import settings
from app.core.errors import (
    E_CONFIG,
    E_POLICY_DENIED,
    E_RATE_LIMITED,
    E_SANDBOX_BLOCKED,
    PolsiaError,
)
from app.services import channel_service, deploy_service, site_service

API_KEY = "rnd_w1_deploy_test_key_do_not_leak"
SERVICE_ID = "srv-w1test"
APPROVAL_SECRET = b"w1-ac03-deploy-approval-secret-not-real"
META = {
    "slug": "landing-page",
    "title": "Landing page",
    "created_at": "2026-10-28T12:00:00+00:00",
    "bytes": 4321,
}


# ── fixtures / helpers ──────────────────────────────────────────────────────────────────────
def _write_site(root: Path, slug: str = "landing-page") -> Path:
    directory = root / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (directory / "styles.css").write_text("body{}", encoding="utf-8")
    meta = {**META, "slug": slug}
    (directory / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return directory


class Recorder:
    """A labelled MOCKED Render transport: records requests; default responder FAILS the test."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responder = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responder is None:
            raise AssertionError(f"unexpected Render call: {request.method} {request.url}")
        return self.responder(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def _deploy_ok(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/deploys"):
        return httpx.Response(201, json={"id": "dep-w1abc", "status": "created"})
    return httpx.Response(200, json={"id": SERVICE_ID, "serviceDetails": {"url": "shop.onrender.com"}})


def _token(slug: str = "landing-page") -> str:
    fingerprint = policy_firewall.fingerprint(
        "external_irreversible",
        deploy_service.RENDER_CHANNEL,
        deploy_service.deploy_payload(slug, SERVICE_ID),
    )
    return policy_firewall.issue_approval(APPROVAL_SECRET, action_fingerprint=fingerprint).token


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(deploy_service.RENDER_API_KEY_ENV, API_KEY)
    monkeypatch.setenv(deploy_service.RENDER_SERVICE_ID_ENV, SERVICE_ID)
    monkeypatch.delenv("POLSIA_KILL_SWITCH", raising=False)
    monkeypatch.setattr(settings, "kill_switch", "")
    return API_KEY


@pytest.fixture
def live_policy(monkeypatch):
    """The ALLOWING policy state: sandbox off, an approval channel configured, kill switch at run."""
    monkeypatch.setattr(settings, "sandbox_mode", False)
    monkeypatch.setattr(settings, "approval_secret", APPROVAL_SECRET.decode())
    monkeypatch.delenv("POLSIA_KILL_SWITCH", raising=False)
    monkeypatch.setattr(settings, "kill_switch", "")
    return _token()


# ── AC-03: the sandbox ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_live_deploy_is_refused_while_the_sandbox_is_on(
    configured, async_db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", True)
    recorder = Recorder()
    site_dir = _write_site(tmp_path)
    before = (site_dir / "meta.json").read_bytes()

    with pytest.raises(PolsiaError) as caught:
        await deploy_service.deploy_site(
            "landing-page",
            dry_run=False,
            root=tmp_path,
            transport=recorder.transport,
            db=async_db_session,
        )

    assert caught.value.code == E_SANDBOX_BLOCKED
    assert caught.value.context.get("sandbox") is True
    assert caught.value.how_to_fix
    assert recorder.requests == [], "a sandboxed deploy must never touch the network"
    assert (site_dir / "meta.json").read_bytes() == before, (
        "a refused deploy must not write a last_deploy record (a labelled refusal, never success)"
    )
    assert deploy_service.deploy_state("landing-page", root=tmp_path)["last_deploy"] is None


@pytest.mark.asyncio
async def test_a_dry_run_stays_available_while_the_sandbox_is_on(
    configured, async_db_session, tmp_path: Path, monkeypatch
) -> None:
    """The sandbox bounds SIDE EFFECTS: previewing the exact call is still a read-only answer."""
    monkeypatch.setattr(settings, "sandbox_mode", True)
    recorder = Recorder()
    _write_site(tmp_path)

    result = await deploy_service.deploy_site(
        "landing-page", root=tmp_path, transport=recorder.transport
    )
    assert result["dry_run"] is True and result["state"] == "dry_run"
    assert recorder.requests == []


# ── AC-04: the operator kill switch ────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("grade", ["freeze", "throttle"])
async def test_a_live_deploy_is_refused_under_the_kill_switch(
    configured, async_db_session, tmp_path: Path, monkeypatch, grade: str
) -> None:
    # Sandbox OFF on purpose: the kill switch must be a control in its own right, not a side
    # effect of SANDBOX_MODE.
    monkeypatch.setattr(settings, "sandbox_mode", False)
    monkeypatch.setattr(settings, "approval_secret", APPROVAL_SECRET.decode())
    monkeypatch.setenv("POLSIA_KILL_SWITCH", grade)
    recorder = Recorder()
    site_dir = _write_site(tmp_path)
    before = (site_dir / "meta.json").read_bytes()

    with pytest.raises(PolsiaError) as caught:
        await deploy_service.deploy_site(
            "landing-page",
            dry_run=False,
            root=tmp_path,
            transport=recorder.transport,
            db=async_db_session,
            approval_token=_token(),
        )

    assert caught.value.code == E_POLICY_DENIED
    assert caught.value.context["kill_switch"] == grade
    assert "kill switch" in caught.value.message
    assert "POLSIA_KILL_SWITCH" in caught.value.how_to_fix
    assert recorder.requests == []
    assert (site_dir / "meta.json").read_bytes() == before


# ── AC-03: the render day cap is consulted from a MEASURED count ───────────────────────────
@pytest.mark.asyncio
async def test_the_render_day_cap_refuses_the_fourth_deploy_of_the_day(
    configured, live_policy, async_db_session, mock_redis, tmp_path: Path
) -> None:
    for index in range(3):
        await channel_service.record_channel_send(
            async_db_session,
            deploy_service.RENDER_CHANNEL,
            summary=f"seed render deploy {index}",
            detail={"seed": index},
            agent_type=deploy_service.OWNER_DEPLOY_SLOT,
        )
    assert await deploy_service.render_deploys_today(async_db_session) == 3, (
        "the count must come from the real channel_send:render rows"
    )

    recorder = Recorder()
    site_dir = _write_site(tmp_path)
    before = (site_dir / "meta.json").read_bytes()

    with pytest.raises(PolsiaError) as caught:
        await deploy_service.deploy_site(
            "landing-page",
            dry_run=False,
            root=tmp_path,
            transport=recorder.transport,
            db=async_db_session,
            approval_token=live_policy,
        )

    assert caught.value.code == E_RATE_LIMITED
    assert "daily cap for render" in caught.value.message and "3/3" in caught.value.message
    assert recorder.requests == []
    assert (site_dir / "meta.json").read_bytes() == before


# ── AC-03: the approval token ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_live_deploy_without_an_approval_token_names_the_fingerprint_to_approve(
    configured, live_policy, async_db_session, tmp_path: Path
) -> None:
    recorder = Recorder()
    site_dir = _write_site(tmp_path)
    before = (site_dir / "meta.json").read_bytes()

    with pytest.raises(PolsiaError) as caught:
        await deploy_service.deploy_site(
            "landing-page",
            dry_run=False,
            root=tmp_path,
            transport=recorder.transport,
            db=async_db_session,
        )

    expected = policy_firewall.fingerprint(
        "external_irreversible",
        deploy_service.RENDER_CHANNEL,
        deploy_service.deploy_payload("landing-page", SERVICE_ID),
    )
    assert caught.value.code == E_POLICY_DENIED
    assert caught.value.context["fingerprint"] == expected
    assert caught.value.context["decision"] == "require_approval"
    assert "approval_token" in caught.value.how_to_fix
    assert expected in caught.value.how_to_fix
    assert recorder.requests == []
    assert (site_dir / "meta.json").read_bytes() == before


@pytest.mark.asyncio
async def test_a_token_minted_for_another_site_does_not_verify(
    configured, live_policy, async_db_session, tmp_path: Path
) -> None:
    other = _token(slug="another-site")
    recorder = Recorder()
    _write_site(tmp_path)
    _write_site(tmp_path, slug="another-site")

    with pytest.raises(PolsiaError) as caught:
        await deploy_service.deploy_site(
            "landing-page",
            dry_run=False,
            root=tmp_path,
            transport=recorder.transport,
            db=async_db_session,
            approval_token=other,
        )

    assert caught.value.code == E_POLICY_DENIED
    assert caught.value.context["decision"] == "require_approval"
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_a_valid_approval_lets_the_one_post_through_and_the_deploy_is_counted(
    configured, live_policy, async_db_session, mock_redis, tmp_path: Path
) -> None:
    """MOCKED TRANSPORT — a fake Render, not a live network call."""
    recorder = Recorder()
    recorder.responder = _deploy_ok
    _write_site(tmp_path)
    assert await deploy_service.render_deploys_today(async_db_session) == 0, "measured zero first"

    result = await deploy_service.deploy_site(
        "landing-page",
        dry_run=False,
        root=tmp_path,
        transport=recorder.transport,
        db=async_db_session,
        approval_token=live_policy,
        at="2026-10-28T13:00:00+00:00",
    )

    assert result["deploy_id"] == "dep-w1abc" and result["state"] == "submitted"
    assert len([r for r in recorder.requests if r.method == "POST"]) == 1
    assert await deploy_service.render_deploys_today(async_db_session) == 1, (
        "the real deploy is recorded for the cap that will bound the next one"
    )
    assert API_KEY not in json.dumps(result)


@pytest.mark.asyncio
async def test_a_live_deploy_without_a_session_is_the_named_config_refusal(
    configured, live_policy, tmp_path: Path
) -> None:
    recorder = Recorder()
    _write_site(tmp_path)
    with pytest.raises(PolsiaError) as caught:
        await deploy_service.deploy_site(
            "landing-page",
            dry_run=False,
            root=tmp_path,
            transport=recorder.transport,
            approval_token=live_policy,
        )
    assert caught.value.code == E_CONFIG
    assert "session" in caught.value.message.lower()
    assert "db=" in caught.value.how_to_fix
    assert recorder.requests == []


# ── the HTTP surface ───────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_deploy_route_answers_e_sandbox_blocked_with_403(
    api_client, auth_headers, configured, tmp_path: Path, monkeypatch
) -> None:
    audit: list[dict] = []

    async def recording_audit(db, agent_type, action, summary, **kwargs):  # noqa: ARG001
        audit.append({"agent": agent_type, "action": action, "summary": summary, **kwargs})
        return True

    monkeypatch.setattr(sites_module, "log_activity_isolated", recording_audit)
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    monkeypatch.setattr(settings, "sandbox_mode", True)
    site_dir = _write_site(tmp_path)
    before = (site_dir / "meta.json").read_bytes()

    resp = await api_client.post(
        "/api/v1/sites/landing-page/deploy?dry_run=false", headers=auth_headers
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == E_SANDBOX_BLOCKED
    assert resp.headers["X-Meridian-Error-Code"] == E_SANDBOX_BLOCKED
    assert resp.json()["error"]["how_to_fix"]
    assert (site_dir / "meta.json").read_bytes() == before
    # "a receipt either way": exactly ONE isolated audit row for the refused live attempt.
    assert len(audit) == 1
    assert audit[0]["action"] == sites_module.DEPLOY_REFUSED_ACTION
    assert audit[0]["detail"]["code"] == E_SANDBOX_BLOCKED
    assert audit[0]["detail"]["slug"] == "landing-page"
    assert "approval_token" not in json.dumps(audit)


@pytest.mark.asyncio
async def test_the_deploy_route_answers_e_policy_denied_under_the_kill_switch(
    api_client, auth_headers, configured, tmp_path: Path, monkeypatch
) -> None:
    audit: list[dict] = []

    async def recording_audit(db, agent_type, action, summary, **kwargs):  # noqa: ARG001
        audit.append({"agent": agent_type, "action": action, **kwargs})
        return True

    monkeypatch.setattr(sites_module, "log_activity_isolated", recording_audit)
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    monkeypatch.setattr(settings, "sandbox_mode", False)
    monkeypatch.setenv("POLSIA_KILL_SWITCH", "freeze")
    _write_site(tmp_path)

    resp = await api_client.post(
        "/api/v1/sites/landing-page/deploy?dry_run=false", headers=auth_headers
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == E_POLICY_DENIED
    assert "kill switch" in resp.json()["error"]["message"]
    assert resp.headers["X-Meridian-Error-Code"] == E_POLICY_DENIED
    assert [row for row in audit if row["detail"].get("code") == E_POLICY_DENIED], (
        "the refused live deploy must leave its audit receipt"
    )


# ── the capability wiring itself ──────────────────────────────────────────────────────────
def test_no_model_agent_may_touch_the_render_channel() -> None:
    """The owner console has its own slot; the fix must not widen a model agent's reach."""
    offenders = [
        agent_type
        for agent_type, capability in policy_firewall.AGENT_CAPABILITIES.items()
        if deploy_service.RENDER_CHANNEL in capability.channels
    ]
    assert offenders == []


def test_an_unknown_direct_caller_is_denied() -> None:
    decision = policy_firewall.decide(
        policy_firewall.ActionRequest(
            agent_type="whatever",
            action_class=policy_firewall.ActionClass.READ,
            channel="",
        ),
        direct_caller="made_up_slot",
        sandbox_mode=False,
    )
    assert decision.decision is policy_firewall.Decision.DENY
    assert "unknown direct caller" in decision.reason


def test_the_owner_deploy_slot_fails_closed_without_an_approval_channel() -> None:
    decision = policy_firewall.decide(
        policy_firewall.ActionRequest(
            agent_type=deploy_service.OWNER_DEPLOY_SLOT,
            action_class=policy_firewall.ActionClass.EXTERNAL_IRREVERSIBLE,
            channel=deploy_service.RENDER_CHANNEL,
            payload=deploy_service.deploy_payload("landing-page", SERVICE_ID),
            sends_today=0,
        ),
        direct_caller=deploy_service.OWNER_DEPLOY_SLOT,
        sandbox_mode=False,
    )
    assert decision.decision is policy_firewall.Decision.DENY
    assert "approval" in decision.reason
