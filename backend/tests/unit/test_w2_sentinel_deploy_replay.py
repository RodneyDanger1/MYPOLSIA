"""W2/SENTINEL — the DEPLOY seam spends the approval: one token, one live deploy.

THE WAVE-1 GAP, re-measured here.  `sentinel/REPORT.md` §4.3 recorded the shipped defect against
the real gate: `"deploy gate: REPLAY the same token a second time" -> "ok"`.  Nothing consumed the
token, so one human approval authorised any number of irreversible Render deploys inside its TTL.

THE SEAM.  `deploy_service._authorise_live_deploy` asks the stateless decision question
(`enforce`, which verifies signature/TTL/fingerprint) and then SPENDS the approval through
`policy_firewall.consume_approval_or_refuse` BEFORE the Render POST is opened.  Three properties are
pinned below:
  1. the second deploy with the same token is refused by name (`POLSIA-E121`, `already_used`) and
     makes NO second network call;
  2. an approval is spent by the ATTEMPT, not by success — a transport failure still spends it, so
     an approval that already produced an unknown-outcome request cannot be replayed;
  3. a refusal BEFORE authorisation (sandbox, kill switch, cap) spends nothing — a token is not
     burned by an action that was never authorised.

The last test is the weaver R-UIB02-1 contract from the other direction: the fingerprint the state
read serves is the fingerprint the gate verifies (a token minted from it authorises the deploy), and
it is `null` — never a plausible-looking digest — while no Render service id exists.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.agents import policy_firewall
from app.config import settings
from app.core.errors import E_POLICY_DENIED, E_SANDBOX_BLOCKED, E_UPSTREAM, PolsiaError
from app.services import deploy_service, site_service

APPROVAL_SECRET = b"w2-sentinel-deploy-secret-not-a-real-one"
API_KEY = "rnd_w2sentinel_test_key"
SERVICE_ID = "srv-sentinel-w2"


# ── the site + the policy state ──────────────────────────────────────────────────────────────
def _write_site(root: Path, slug: str = "landing-page") -> Path:
    site_dir = root / slug
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "index.html").write_text("<html><body>w2</body></html>", encoding="utf-8")
    (site_dir / "meta.json").write_text(
        json.dumps({"slug": slug, "title": "W2",
                    "created_at": "2026-10-28T00:00:00+00:00"}),
        encoding="utf-8",
    )
    return site_dir


@pytest.fixture
def sites_root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def render_configured(monkeypatch):
    monkeypatch.setenv(deploy_service.RENDER_API_KEY_ENV, API_KEY)
    monkeypatch.setenv(deploy_service.RENDER_SERVICE_ID_ENV, SERVICE_ID)
    return API_KEY


@pytest.fixture
def live_policy(monkeypatch):
    """The ALLOWING policy state: sandbox OFF, an approval channel, no kill switch."""
    monkeypatch.setattr(settings, "sandbox_mode", False)
    monkeypatch.setattr(settings, "approval_secret", APPROVAL_SECRET.decode())
    monkeypatch.delenv("POLSIA_KILL_SWITCH", raising=False)
    monkeypatch.setattr(settings, "kill_switch", "")


def _token(*, slug: str = "landing-page", ttl_s: int = 3600) -> str:
    fingerprint = deploy_service.deploy_fingerprint(slug, SERVICE_ID)
    return policy_firewall.issue_approval(
        APPROVAL_SECRET, action_fingerprint=fingerprint, ttl_s=ttl_s
    ).token


class Recorder:
    """A labelled MOCKED Render transport — a fake at the network boundary, never a live call."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responder = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responder is None:  # pragma: no cover - a test bug, surfaced loudly
            raise AssertionError(f"unexpected Render call: {request.method} {request.url}")
        return self.responder(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @property
    def posts(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "POST"]


def _deploy_ok(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/deploys"):
        return httpx.Response(201, json={"id": "dep-w2abc", "status": "created"})
    return httpx.Response(200, json={"serviceDetails": {"url": "https://w2.onrender.com"}})


def _boom(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused for test", request=request)


# ── 1. the replay is refused, by name, before any second network call ────────────────────────
@pytest.mark.asyncio
async def test_one_approval_authorises_one_deploy_and_the_replay_is_refused(
    render_configured, live_policy, async_db_session, mock_redis, sites_root: Path
) -> None:
    """THE RED LEG.  Pre-fix, the second deploy below returned `ok` (wave-1 §4.3)."""
    recorder = Recorder()
    recorder.responder = _deploy_ok
    site_dir = _write_site(sites_root)
    token = _token()

    first = await deploy_service.deploy_site(
        "landing-page",
        dry_run=False,
        root=sites_root,
        transport=recorder.transport,
        db=async_db_session,
        approval_token=token,
        at="2026-10-28T20:00:00+00:00",
    )
    assert first["deploy_id"] == "dep-w2abc"
    assert len(recorder.posts) == 1
    after_first = (site_dir / "meta.json").read_bytes()

    with pytest.raises(PolsiaError) as caught:
        await deploy_service.deploy_site(
            "landing-page",
            dry_run=False,
            root=sites_root,
            transport=recorder.transport,
            db=async_db_session,
            approval_token=token,
            at="2026-10-28T20:00:05+00:00",
        )

    replay = caught.value
    assert replay.code == E_POLICY_DENIED
    assert replay.context["reason"] == "already_used"
    assert replay.context["approval_replay"] is True
    assert replay.context["policy_code"] == "POLSIA-E121"
    assert "already used" in replay.message.lower()
    assert len(recorder.posts) == 1, "the replayed approval must not reach Render a second time"
    assert (site_dir / "meta.json").read_bytes() == after_first, (
        "a refused replay must not rewrite the deploy record"
    )


@pytest.mark.asyncio
async def test_a_fresh_token_after_the_replay_is_allowed_again(
    render_configured, live_policy, async_db_session, mock_redis, sites_root: Path
) -> None:
    """Single-use, not single-DEPLOY: a NEW approval authorises the next action."""
    recorder = Recorder()
    recorder.responder = _deploy_ok
    _write_site(sites_root)

    for stamp in ("2026-10-28T20:01:00+00:00", "2026-10-28T20:01:05+00:00"):
        await deploy_service.deploy_site(
            "landing-page",
            dry_run=False,
            root=sites_root,
            transport=recorder.transport,
            db=async_db_session,
            approval_token=_token(),
            at=stamp,
        )
    assert len(recorder.posts) == 2
    assert await deploy_service.render_deploys_today(async_db_session) == 2, (
        "two authorised deploys were really submitted, and the day cap counts both"
    )


# ── 2. an attempt spends the approval, even when the attempt fails ───────────────────────────
@pytest.mark.asyncio
async def test_a_transport_failure_still_spends_the_approval(
    render_configured, live_policy, async_db_session, sites_root: Path
) -> None:
    """A deploy that may have reached Render must not leave the approval replayable."""
    recorder = Recorder()
    recorder.responder = _boom
    _write_site(sites_root)
    token = _token()

    with pytest.raises(PolsiaError) as failed:
        await deploy_service.deploy_site(
            "landing-page",
            dry_run=False,
            root=sites_root,
            transport=recorder.transport,
            db=async_db_session,
            approval_token=token,
        )
    assert failed.value.code == E_UPSTREAM

    with pytest.raises(PolsiaError) as replay:
        await deploy_service.deploy_site(
            "landing-page",
            dry_run=False,
            root=sites_root,
            transport=recorder.transport,
            db=async_db_session,
            approval_token=token,
        )
    assert replay.value.context["reason"] == "already_used"
    assert len(recorder.posts) == 1


# ── 3. a refusal BEFORE authorisation spends nothing ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_sandbox_refusal_does_not_burn_the_token(
    render_configured, live_policy, async_db_session, mock_redis, sites_root: Path, monkeypatch
) -> None:
    """A token presented to an action the gate REFUSED is not an approval that was used."""
    recorder = Recorder()
    recorder.responder = _deploy_ok
    _write_site(sites_root)
    token = _token()

    monkeypatch.setattr(settings, "sandbox_mode", True)
    with pytest.raises(PolsiaError) as sandboxed:
        await deploy_service.deploy_site(
            "landing-page",
            dry_run=False,
            root=sites_root,
            transport=recorder.transport,
            db=async_db_session,
            approval_token=token,
        )
    assert sandboxed.value.code == E_SANDBOX_BLOCKED
    assert recorder.posts == []

    monkeypatch.setattr(settings, "sandbox_mode", False)
    result = await deploy_service.deploy_site(
        "landing-page",
        dry_run=False,
        root=sites_root,
        transport=recorder.transport,
        db=async_db_session,
        approval_token=token,
        at="2026-10-28T20:02:00+00:00",
    )
    assert result["deploy_id"] == "dep-w2abc", "the refused attempt must not have spent the token"


# ── 4. weaver R-UIB02-1: the state read serves the fingerprint the gate verifies ────────────
@pytest.mark.asyncio
async def test_the_fingerprint_the_state_read_serves_authorises_the_deploy(
    render_configured, live_policy, async_db_session, mock_redis, sites_root: Path
) -> None:
    _write_site(sites_root)
    state = deploy_service.deploy_state("landing-page", root=sites_root)
    fingerprint = state["approval_fingerprint"]

    assert isinstance(fingerprint, str) and len(fingerprint) == 24
    assert fingerprint == deploy_service.deploy_fingerprint("landing-page", SERVICE_ID), (
        "the state read must use the SAME function the gate verifies, never a copy"
    )

    recorder = Recorder()
    recorder.responder = _deploy_ok
    token = policy_firewall.issue_approval(
        APPROVAL_SECRET, action_fingerprint=fingerprint
    ).token
    result = await deploy_service.deploy_site(
        "landing-page",
        dry_run=False,
        root=sites_root,
        transport=recorder.transport,
        db=async_db_session,
        approval_token=token,
        at="2026-10-28T20:03:00+00:00",
    )
    assert result["deploy_id"] == "dep-w2abc"
    assert result["approval_fingerprint"] == fingerprint


def test_the_state_read_has_no_fingerprint_without_a_service_id(
    sites_root: Path, monkeypatch
) -> None:
    """No service id = no action to approve yet: null, never a digest for `service_id: ""`."""
    _write_site(sites_root)
    monkeypatch.delenv(deploy_service.RENDER_API_KEY_ENV, raising=False)
    monkeypatch.delenv(deploy_service.RENDER_SERVICE_ID_ENV, raising=False)
    monkeypatch.setattr(settings, "render_api_key", "")
    monkeypatch.setattr(settings, "render_service_id", "")

    state = deploy_service.deploy_state("landing-page", root=sites_root)
    assert state["configured"] is False
    assert state["approval_fingerprint"] is None


def test_deploy_fingerprint_refuses_a_missing_service_id() -> None:
    with pytest.raises(PolsiaError) as caught:
        deploy_service.deploy_fingerprint("landing-page", "")
    assert caught.value.code == "E_CONFIG"
    assert "fingerprint" in caught.value.message


@pytest.mark.asyncio
async def test_a_dry_run_preview_carries_the_same_fingerprint(
    render_configured, sites_root: Path
) -> None:
    """The preview is the exact deploy a live attempt would make, so it names the same fingerprint."""
    _write_site(sites_root)
    preview = await deploy_service.deploy_site("landing-page", root=sites_root, dry_run=True)
    assert preview["approval_fingerprint"] == deploy_service.deploy_fingerprint(
        "landing-page", SERVICE_ID
    )
    assert preview["state"] == "dry_run"


# ── 5. the HTTP seam: the wire serves the fingerprint, and a spent token is refused ──────────
@pytest.mark.asyncio
async def test_get_deploy_state_over_http_serves_the_fingerprint(
    api_client, auth_headers, render_configured, sites_root: Path
) -> None:
    _write_site(sites_root)
    resp = await api_client.get("/api/v1/sites/landing-page/deploy", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approval_fingerprint"] == deploy_service.deploy_fingerprint(
        "landing-page", SERVICE_ID
    )
    assert body["configured"] is True


@pytest.mark.asyncio
async def test_http_deploy_with_an_already_spent_token_is_403_e_policy_denied(
    api_client, auth_headers, render_configured, live_policy, async_db_session,
    mock_redis, sites_root: Path,
) -> None:
    """The HTTP leg of the replay refusal — refused BEFORE any Render request is attempted."""
    _write_site(sites_root)
    token = _token()
    spent = await policy_firewall.consume_approval(
        async_db_session,
        APPROVAL_SECRET,
        token,
        action_fingerprint=deploy_service.deploy_fingerprint("landing-page", SERVICE_ID),
        consumed_by="test:pre-spend",
    )
    assert spent.ok is True

    resp = await api_client.post(
        "/api/v1/sites/landing-page/deploy",
        json={"dry_run": False, "approval_token": token},
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.headers["X-Polsia-Error-Code"] == E_POLICY_DENIED
    error = resp.json()["error"]
    assert error["context"]["reason"] == "already_used"
    assert error["context"]["policy_code"] == "POLSIA-E121"
    assert error["context"]["approval_replay"] is True
    assert token not in resp.text, "a refusal must never echo the token"
