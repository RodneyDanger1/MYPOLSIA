"""WAVE 8 / logic-debugger — JOB 2: the wave-7 R1 SEND-TIME-VERIFICATION contract, under failure.

THE CONTRACT (wave-7 integration notes, R1/R2/R4 closed by the Lead):

  * `outreach_service.compliance_preflight(db)` is a SNAPSHOT and DECLARES that it is one:
    `send_time_verified = False`.
  * The session-free send path (`email_compliance.send_compliant_email(db=None, preflight=…)`) —
    the one the outreach/CS agents use from a worker thread with no DB session — REFUSES unless
    the producer asserted `preflight["send_time_verified"] is True` (fail closed; a stale snapshot
    cannot mail a person who has just unsubscribed).
  * `celery_app.tasks.agent_tasks._with_compliance_preflight` is the ONE producer of that
    assertion, and it adds it at DISPATCH (mode 1).
  * With no preflight, the guarded sender runs on its own session (mode 2) and re-reads the DB —
    fail closed for the same person.
  * Both consumers share ONE key: `email_outreach.compliance_send.PREFLIGHT_KEY` ==
    `customer_support.send_bridge.PREFLIGHT_KEY` == "compliance".

This file proves the contract at every seam and under every injected failure: the producer's own
declaration, the worker's assertion (outreach AND customer support), the strict `is True` check
(truthy strings/ints refuse), the shape floor, the producer-failure fallback (key absent -> mode 2,
fail closed, and the fallback is LOGGED), both modes' behaviour, and the one residual bound that
the wave-8 per-SEND refresh carry still owns (an opt-out that lands AFTER dispatch).

Rows are numbered R01..R13 in FAILURE-MATRIX-W8.md.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.errors import E_CONFIG, E_DB, E_POLICY_DENIED, PolsiaError
from app.models.outreach import Prospect
from app.services import email_compliance as compliance
from app.services import outreach_service
from app.services import support_inbox_service as inbox  # noqa: F401  (registers its models)

ADDRESS = "ada@example.com"
OTHER = "grace@example.com"

COMPANY = SimpleNamespace(
    sender_legal_name="Meridian Labs, Inc.",
    sender_postal_address="1 Example Street, Springfield, IL 62701",
    website_url="https://example.test",
)


@pytest.fixture(autouse=True)
def _configured_channel(monkeypatch):
    """Sandbox off (the send path is what this file measures) + the identity gate satisfied."""
    monkeypatch.setattr(settings, "sandbox_mode", False)
    monkeypatch.setattr("app.services.company_service.get_company", _company)
    return None


async def _company(_db):
    return COMPANY


class _ProviderTripwire:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any):
        self.calls.append(dict(kwargs))
        return {"ok": True, "result": {"message_id": "sg-w8-r1"}}

    @property
    def bodies(self) -> list[str]:
        return [str(call.get("body") or "") for call in self.calls]


@pytest.fixture()
def provider(monkeypatch) -> _ProviderTripwire:
    trip = _ProviderTripwire()
    monkeypatch.setattr("app.agents.integrations.send_email", trip)
    return trip


async def _seed_prospect(db, *, status: str = "new", email: str = ADDRESS) -> Prospect:
    prospect = Prospect(email=email, status=status, source="w8-r1-test")
    db.add(prospect)
    await db.flush()
    return prospect


def _preflight_from_producer(raw: dict[str, Any]) -> dict[str, Any]:
    return raw


# ── R01: the producer DECLARES the snapshot ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_r01_the_producer_declares_the_preflight_is_a_snapshot(async_db_session):
    preflight = await outreach_service.compliance_preflight(async_db_session)

    assert preflight["send_time_verified"] is False, (
        "the producer must never assert a verification it did not perform"
    )
    assert preflight["company"]["sender_legal_name"] == COMPANY.sender_legal_name
    assert isinstance(preflight["suppressed"], list)
    assert compliance_send_key() == outreach_service_key() == "compliance"


def compliance_send_key() -> str:
    from app.agents.email_outreach.compliance_send import PREFLIGHT_KEY

    return PREFLIGHT_KEY


def outreach_service_key() -> str:
    from app.agents.customer_support.send_bridge import PREFLIGHT_KEY

    return PREFLIGHT_KEY


# ── R02: the WORKER adds the dispatch-time assertion (outreach) ────────────────────────────
@pytest.mark.asyncio
async def test_r02_the_worker_asserts_send_time_verification_at_dispatch(async_db_session):
    from celery_app.tasks.agent_tasks import _with_compliance_preflight

    context: dict[str, Any] = {"task": "outreach run"}
    enriched = await _with_compliance_preflight(async_db_session, "email_outreach", context)

    preflight = enriched["compliance"]
    assert preflight["send_time_verified"] is True, (
        "the worker's dispatch-time assertion is what unlocks the session-free send"
    )
    assert preflight["company"]["sender_legal_name"] == COMPANY.sender_legal_name
    assert "compliance" not in context, "the caller's context is not mutated in place"


# ── R03: the same assertion for customer support (one key, one producer) ──────────────────
@pytest.mark.asyncio
async def test_r03_customer_support_reads_the_same_asserted_preflight(async_db_session):
    from celery_app.tasks.agent_tasks import _with_compliance_preflight

    enriched = await _with_compliance_preflight(async_db_session, "customer_support", {})
    assert enriched["compliance"]["send_time_verified"] is True
    assert compliance_send_key() == "compliance"


@pytest.mark.asyncio
async def test_r04_an_unrelated_agent_gets_no_preflight_at_all(async_db_session):
    from celery_app.tasks.agent_tasks import _with_compliance_preflight

    context = {"task": "social run"}
    assert await _with_compliance_preflight(async_db_session, "social_media", context) is context


# ── R05: a producer failure leaves the key ABSENT (mode 2, fail closed) and is LOGGED ────
@pytest.mark.asyncio
async def test_r05_a_producer_failure_leaves_the_key_absent_and_is_logged(monkeypatch, caplog):
    import logging

    from celery_app.tasks.agent_tasks import _with_compliance_preflight

    class _DeadSession:
        async def execute(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            raise PolsiaError(E_DB, "planted: the database went away")

    with caplog.at_level(logging.WARNING):
        enriched = await _with_compliance_preflight(_DeadSession(), "email_outreach", {"k": 1})

    assert "compliance" not in enriched, (
        "a producer that cannot answer must leave the key ABSENT — an empty preflight would read "
        "as 'nothing suppressed'"
    )
    assert enriched["k"] == 1
    assert any(
        "code=compliance_preflight_unavailable" in record.getMessage()
        for record in caplog.records
    ), "the fallback must be observable: the run is now on mode 2 (fail closed)"


# ── R06: a STALE producer snapshot refuses in the session-free path ──────────────────────
@pytest.mark.asyncio
async def test_r06_a_stale_preflight_without_the_assertion_refuses_before_the_provider(
    async_db_session, provider
):
    preflight = await outreach_service.compliance_preflight(async_db_session)
    assert preflight["send_time_verified"] is False

    with pytest.raises(PolsiaError) as caught:
        await compliance.send_compliant_email(
            None, to=ADDRESS, subject="hello", body="body", preflight=_preflight_from_producer(preflight)
        )

    assert caught.value.code == E_POLICY_DENIED
    assert "send_time_verified" in (caught.value.how_to_fix or "")
    assert provider.calls == [], "a stale snapshot must never reach the provider"


# ── R07: the check is STRICT (`is True`), not truthy ─────────────────────────────────────
@pytest.mark.parametrize("crafted", ["yes", 1, "True", {"verified": True}, None])
@pytest.mark.asyncio
async def test_r07_a_crafted_truthy_marker_is_not_a_verification(crafted, async_db_session, provider):
    preflight = await outreach_service.compliance_preflight(async_db_session)
    crafted_preflight = {**preflight, "send_time_verified": crafted}

    with pytest.raises(PolsiaError) as caught:
        await compliance.send_compliant_email(
            None, to=ADDRESS, subject="s", body="b", preflight=crafted_preflight
        )

    assert caught.value.code == E_POLICY_DENIED
    assert provider.calls == []


# ── R08: the shape floor still runs under the assertion ──────────────────────────────────
@pytest.mark.asyncio
async def test_r08_a_malformed_suppression_list_refuses_even_with_the_assertion(
    async_db_session, provider
):
    preflight = await outreach_service.compliance_preflight(async_db_session)
    crafted = {**preflight, "suppressed": ADDRESS, "send_time_verified": True}

    with pytest.raises(PolsiaError) as caught:
        await compliance.send_compliant_email(None, to=OTHER, subject="s", body="b", preflight=crafted)

    assert caught.value.code == E_CONFIG
    assert "malformed" in caught.value.message
    assert provider.calls == []


async def test_r09_the_snapshot_itself_still_suppresses_an_opted_out_address(
    async_db_session, provider
):
    await _seed_prospect(async_db_session, status="unsubscribed")
    preflight = await outreach_service.compliance_preflight(async_db_session)

    with pytest.raises(PolsiaError) as caught:
        await compliance.send_compliant_email(
            None, to=ADDRESS, subject="s", body="b", preflight={**preflight, "send_time_verified": True}
        )

    assert caught.value.code == E_POLICY_DENIED
    assert provider.calls == [], "the snapshot's own suppression check still gates the send"


# ── R10: MODE 1 — the worker preflight send really happens, with the footer ──────────────
@pytest.mark.asyncio
async def test_r10_mode1_a_worker_verified_preflight_sends_through_the_guarded_path(
    async_db_session, provider, monkeypatch
):
    from app.agents.email_outreach.compliance_send import send_outreach_email
    from celery_app.tasks.agent_tasks import _with_compliance_preflight

    monkeypatch.setattr(settings, "approval_secret", "w8-r1-secret")
    await _seed_prospect(async_db_session, status="new")
    context = await _with_compliance_preflight(async_db_session, "email_outreach", {})
    result = send_outreach_email(
        to=ADDRESS, subject="hello", body="the body", context=context
    )

    assert result["ok"] is True
    assert len(provider.calls) == 1
    body = provider.bodies[0]
    assert "the body" in body
    assert "Meridian Labs, Inc." in body, "the sender identity footer is present"
    assert "/api/v1/outreach/unsubscribe?token=" in body, (
        "a signed one-click opt-out link must be in every guarded send"
    )
    assert compliance.unsubscribe_token(ADDRESS) in body, (
        "the link is the same stable, address-bound token the route verifies"
    )


@pytest.mark.asyncio
async def test_r10b_with_no_unsubscribe_secret_the_footer_falls_back_AND_says_so(
    async_db_session, provider, monkeypatch, caplog
):
    """No APPROVAL_SECRET: the clickable link cannot be minted.  The send is not silently
    link-less — the footer carries the reply-based opt-out and the skip is LOGGED by name."""
    import logging

    from app.agents.email_outreach.compliance_send import send_outreach_email
    from celery_app.tasks.agent_tasks import _with_compliance_preflight

    monkeypatch.setattr(settings, "approval_secret", "")
    await _seed_prospect(async_db_session, status="new")
    context = await _with_compliance_preflight(async_db_session, "email_outreach", {})
    with caplog.at_level(logging.WARNING, logger="app.services.email_compliance"):
        result = send_outreach_email(to=ADDRESS, subject="hello", body="the body", context=context)

    assert result["ok"] is True
    body = provider.bodies[0]
    assert "/api/v1/outreach/unsubscribe?token=" not in body
    assert "opt out" in body.lower(), "the reply-based opt-out is still offered"
    assert any(
        "opt_out_link_skipped" in record.getMessage() for record in caplog.records
    ), "the missing link must be observable, not a silent downgrade"


# ── R11: MODE 2 — no preflight means the DB arm, and ITS suppression gate holds ──────────
@pytest.fixture()
def file_database(monkeypatch, tmp_path):
    """A REAL file-backed database for the loop-local session mode 2 opens by itself."""
    import app.models  # noqa: F401  - register every table before create_all
    from app.core.database import Base

    path = tmp_path / "compliance.sqlite"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)

    async def _create() -> None:
        engine = create_async_engine(url, connect_args={"check_same_thread": False})
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    import asyncio

    asyncio.run(_create())
    return path, url


def _seed_in_file(url: str, *, status: str) -> None:
    import asyncio

    async def _seed() -> None:
        engine = create_async_engine(url, connect_args={"check_same_thread": False})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add(Prospect(email=ADDRESS, status=status, source="w8-r1-file"))
            await db.commit()
        await engine.dispose()

    asyncio.run(_seed())


def test_r11_mode2_without_a_preflight_re_reads_the_db_and_refuses_a_suppressed_address(
    file_database, provider
):
    from app.agents.email_outreach.compliance_send import send_outreach_email

    _path, url = file_database
    _seed_in_file(url, status="unsubscribed")

    with pytest.raises(PolsiaError) as caught:
        send_outreach_email(to=ADDRESS, subject="s", body="b", context=None)

    assert caught.value.code == E_POLICY_DENIED
    assert provider.calls == [], "mode 2 is the DB arm: the opt-out is visible and gates the send"


def test_r11b_mode2_without_a_preflight_sends_for_a_clean_address(file_database, provider):
    from app.agents.email_outreach.compliance_send import send_outreach_email

    _path, url = file_database
    _seed_in_file(url, status="new")

    result = send_outreach_email(to=ADDRESS, subject="s", body="body", context={})

    assert result["ok"] is True
    assert len(provider.calls) == 1


# ── R12: CUSTOMER SUPPORT — both modes, the same key, the same floor ────────────────────
@pytest.mark.asyncio
async def test_r12_cs_mode1_uses_the_worker_assertion(async_db_session, provider):
    from app.agents.customer_support.send_bridge import send_cs_reply
    from celery_app.tasks.agent_tasks import _with_compliance_preflight

    await _seed_prospect(async_db_session, status="new")
    context = await _with_compliance_preflight(async_db_session, "customer_support", {})
    result = send_cs_reply(to=ADDRESS, subject="re: your ticket", body="the reply", context=context)

    assert result["ok"] is True
    assert "unsubscribe" in provider.bodies[0].lower()


@pytest.mark.asyncio
async def test_r12b_cs_mode1_refuses_a_stale_snapshot(async_db_session, provider):
    from app.agents.customer_support.send_bridge import send_cs_reply

    preflight = await outreach_service.compliance_preflight(async_db_session)
    with pytest.raises(PolsiaError) as caught:
        send_cs_reply(to=ADDRESS, subject="s", body="b", context={"compliance": preflight})
    assert caught.value.code == E_POLICY_DENIED
    assert provider.calls == []


def test_r12c_cs_mode2_refuses_a_suppressed_address(file_database, provider):
    from app.agents.customer_support.send_bridge import send_cs_reply

    _path, url = file_database
    _seed_in_file(url, status="unsubscribed")

    with pytest.raises(PolsiaError) as caught:
        send_cs_reply(to=ADDRESS, subject="s", body="b", context=None)
    assert caught.value.code == E_POLICY_DENIED
    assert provider.calls == []


# ── R13: an opt-out AFTER dispatch (CLOSED in wave 9 by the per-SEND re-verification) ────
@pytest.mark.asyncio
async def test_r13_an_opt_out_after_dispatch_must_not_still_receive_mail(
    async_db_session, provider
):
    """W8 carry / R13, CLOSED in wave 9 (the strict xfail was removed when this row went green).

    The defect: `send_time_verified` is asserted at DISPATCH, so an opt-out that lands between
    dispatch and the send loop is invisible to the session-free send — the mail still went out.

    The fix: `_with_compliance_preflight` installs a run-scoped RESOLVER (contextvar, the
    `run_progress` usage-sink pattern) that re-reads `prospects.status` at SEND time through the
    worker's own engine on the worker's own loop; `send_outreach_email` captures it and
    `email_compliance.send_compliant_email` consults it immediately before the provider call.  The
    refusal is `E_POLICY_DENIED` — a policy refusal, not a database error — and the provider is
    never reached.  The marker's dispatch-time meaning is unchanged; it is simply no longer the
    authority for one send.
    """
    from app.agents.email_outreach.compliance_send import send_outreach_email
    from celery_app.tasks.agent_tasks import _with_compliance_preflight

    prospect = await _seed_prospect(async_db_session, status="new")
    context = await _with_compliance_preflight(async_db_session, "email_outreach", {})

    # The opt-out lands AFTER the dispatch-time resolution (an unsubscribe webhook, one second
    # later) and is committed.
    prospect.status = "unsubscribed"
    await async_db_session.flush()

    with pytest.raises(PolsiaError) as caught:
        send_outreach_email(to=ADDRESS, subject="s", body="b", context=context)
    assert caught.value.code == E_POLICY_DENIED, (
        "an opt-out that landed after dispatch is a POLICY refusal, not a database error — the "
        f"send must be stopped for the reason it was stopped: {caught.value.code}"
    )
    assert caught.value.context.get("source") == "send_time_reverification", (
        "the refusal must say WHICH read found the opt-out (the per-send re-verification)"
    )
    assert provider.calls == []


@pytest.mark.asyncio
async def test_r13b_a_run_without_a_resolver_still_sends_and_says_it_could_not_reverify(
    async_db_session, provider, caplog
):
    """The seam degrades OBSERVABLY, not silently: no resolver installed -> marker-only send.

    This is the documented residual (a direct caller that never went through the worker's
    preflight hook — e.g. the invoice path), and it must stay visible in the log rather than
    pretend it re-verified.  The worker path installs a resolver, so a run dispatched by
    `run_agent_task` is covered by R13 itself.
    """
    import logging

    from app.agents.email_outreach.compliance_send import send_outreach_email
    from app.services import email_compliance as compliance

    compliance.clear_send_time_resolver()
    preflight = await outreach_service.compliance_preflight(async_db_session)
    context = {"compliance": {**preflight, "send_time_verified": True}}

    with caplog.at_level(logging.WARNING, logger="app.services.email_compliance"):
        result = send_outreach_email(to=ADDRESS, subject="s", body="body", context=context)

    assert result["ok"] is True, "without a resolver the documented marker-only path still sends"
    assert len(provider.calls) == 1
    assert any(
        "send_time_reverification_unavailable" in record.getMessage()
        for record in caplog.records
    ), "the unverified send must be named in the log, never silent"


@pytest.mark.asyncio
async def test_r13c_a_failing_send_time_read_refuses_instead_of_sending(async_db_session, provider):
    """FAIL CLOSED: a resolver that cannot read the database must stop the send (E_DB), never pass."""
    from app.agents.email_outreach.compliance_send import send_outreach_email
    from app.services import email_compliance as compliance

    preflight = await outreach_service.compliance_preflight(async_db_session)
    context = {"compliance": {**preflight, "send_time_verified": True}}
    token = compliance.set_send_time_resolver(_raising_resolver)
    try:
        with pytest.raises(PolsiaError) as caught:
            send_outreach_email(to=ADDRESS, subject="s", body="b", context=context)
    finally:
        compliance.reset_send_time_resolver(token)

    assert caught.value.code == E_DB, "an unread suppression list is E_DB, not a send"
    assert provider.calls == [], "could-not-check must never read as not-suppressed"


def _raising_resolver(_address: str) -> bool:
    raise RuntimeError("planted: the database is unreachable at send time")


def test_r13d_the_resolver_is_run_scoped_and_clears_to_none():
    """The seam is a contextvar, not a module global: set -> read -> clear -> gone."""
    from app.services import email_compliance as compliance

    assert compliance.current_send_time_resolver() is None
    token = compliance.set_send_time_resolver(_raising_resolver)
    assert compliance.current_send_time_resolver() is _raising_resolver
    compliance.reset_send_time_resolver(token)
    assert compliance.current_send_time_resolver() is None


# ── R14: the worker's preflight carries no credential / no token ─────────────────────────
@pytest.mark.asyncio
async def test_r14_the_preflight_contains_no_secret_material(async_db_session):
    from celery_app.tasks.agent_tasks import _with_compliance_preflight

    enriched = await _with_compliance_preflight(async_db_session, "email_outreach", {})
    serialized = repr(enriched)
    for forbidden in ("api_key", "token", "secret", "password"):
        assert forbidden not in serialized.lower(), (
            f"the run context is persisted on the task/run row: it must not carry {forbidden}"
        )
