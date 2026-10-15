"""V11/W1 — outreach compliance routing + the unsubscribe producer (RED-first legs).

WHAT THIS FILE PINS (V10 audit findings B9 "compliance floor NOT WIRED for outreach mail" and
B10 "no unsubscribe producer"):

* the outreach agent's send path reaches the provider ONLY through
  `email_compliance.send_compliant_email` — the guarded sender — so every real outreach email
  carries the CAN-SPAM floor (sender legal name, postal address, opt-out, privacy URL) and the
  AI-assistance disclosure; a direct `integrations.send_email` call from the outreach send path
  is pinned ABSENT at source level (the bypass B9 measured);
* the footer carries a WORKING one-click opt-out URL: `<website_url>/api/v1/outreach/unsubscribe
  ?token=...`, reachable with NO owner key, that durably flips `prospects.status` to
  'unsubscribed' — and every later send to that address is refused by the EXISTING unsubscribed
  check in `send_compliant_email` (`E_POLICY_DENIED`, provider never reached);
* a missing sender identity refuses BY NAME (E_CONFIG "sender identity not configured" + the exact
  missing field list + how_to_fix) and a suppressed address refuses by name — never a silent drop,
  never a provider call.

TWO RESOLUTION MODES (both pinned here, both fail-closed):
  1. the run context carries the worker-resolved preflight (`context["compliance"]` — the
     `channel_sends` pattern: the worker owns the DB, the agent does not) -> the session-free mode;
  2. no preflight in the context -> the guarded sender runs on a LOOP-LOCAL session the bridge
     opens and disposes (the agent thread has no session of its own), so the fix is real on the
     production path TODAY, before any worker hook lands.

Doubles live only here: the SendGrid SDK module (the repo's own pattern, see
`test_r30_email_producer_truth.py`), the settings seam, and the FILE-backed SQLite the bridge needs
(a second engine cannot open an in-memory database, so the file is what makes mode 2 testable).
Every row is written by the REAL services on the real schema; no live provider, no real email.
"""
from __future__ import annotations

import re
import sys
import types
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.config as app_config
import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.agents.email_outreach.agent import EmailOutreachAgent
from app.core.database import Base
from app.core.errors import E_CONFIG, E_POLICY_DENIED, E_VALIDATION, PolsiaError
from app.models.company import CompanyConfig
from app.models.outreach import Prospect
from app.services.email_compliance import (
    AI_ASSISTANCE_DISCLOSURE,
    UNSUBSCRIBE_PATH,
    unsubscribe_token,
)

LEGAL_NAME = "Acme Robotics LLC"
POSTAL_ADDRESS = "123 Main St, Minneapolis, MN 55401, USA"
WEBSITE = "https://acme.example"
PRIVACY_URL = f"{WEBSITE}/privacy"
SECRET = "unit-test-approval-secret"
#: the token alphabet is base64url + the '=' pad; the URL is what a recipient actually clicks
TOKEN_RE = re.compile(re.escape(UNSUBSCRIBE_PATH) + r"\?token=([A-Za-z0-9_\-\.=]+)")
SENDER_FIELDS = ["sender_legal_name", "sender_postal_address", "website_url"]


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch: Any):
    """The opt-out HMAC secret (`settings.approval_secret`) — the repo's one HMAC secret.

    Armed for every leg here: minting a link without it is refused by name on purpose (a
    guessable opt-out token on a public route would let anyone unsubscribe anyone).
    """
    monkeypatch.setattr(app_config.settings, "approval_secret", SECRET)


# ── doubles ───────────────────────────────────────────────────────────────────────────────────


class _FakeSendGrid:
    """Provider double: 202 + a unique `X-Message-Id`, capturing every message it was handed."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def install(self, monkeypatch: Any) -> _FakeSendGrid:
        fake = self

        class _Response:
            def __init__(self, message_id: str) -> None:
                self.status_code = 202
                self.headers = {"X-Message-Id": message_id}

        class _Client:
            def __init__(self, api_key: str) -> None:
                self.api_key = api_key

            def send(self, message: Any) -> _Response:
                fake.sent.append(dict(message["mail"]))
                return _Response(f"sg-msg-{len(fake.sent)}")

        sendgrid_mod = types.ModuleType("sendgrid")
        helpers = types.ModuleType("sendgrid.helpers")
        mail_mod = types.ModuleType("sendgrid.helpers.mail")
        sendgrid_mod.SendGridAPIClient = _Client            # type: ignore[attr-defined]
        mail_mod.Mail = lambda **kw: {"mail": kw}            # type: ignore[attr-defined]
        helpers.mail = mail_mod                              # type: ignore[attr-defined]
        sendgrid_mod.helpers = helpers                       # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sendgrid", sendgrid_mod)
        monkeypatch.setitem(sys.modules, "sendgrid.helpers", helpers)
        monkeypatch.setitem(sys.modules, "sendgrid.helpers.mail", mail_mod)
        return fake


def _stub_collection(agent: Any, payload: dict[str, Any]) -> Any:
    """Hand the agent the payload its CLI collection would have produced."""
    agent.collect_structured = lambda task, context, **kw: (dict(payload), None)
    return agent


def _send_payload(address: str = "prospect@example.com") -> dict[str, Any]:
    return {"action": "send", "emails": [{
        "prospect_email": address, "subject": "one honest question", "body": "the first touch",
    }]}


def _run(agent: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = {"channel_sends": {"email": {"today": 0, "hour": 0}}}
    ctx.update(context or {})
    return agent.run({"title": "send"}, ctx)


def _arm(monkeypatch: Any, database_url: str) -> None:
    """Real send posture: no sandbox, SendGrid configured, the DB the bridge must open."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(app_config.settings, "sendgrid_api_key", "SG.test-key")
    monkeypatch.setattr(app_config.settings, "sendgrid_from_email", "ops@example.com")
    monkeypatch.setattr(app_config.settings, "approval_secret", SECRET)
    monkeypatch.setattr(app_config.settings, "database_url", database_url)


@pytest.fixture
async def file_db(tmp_path: Any, monkeypatch: Any):
    """A FILE-backed sqlite the bridge's own engine can open — the mode-2 production shape.

    The session factory is handed to the test; `settings.database_url` points at the same file so
    the bridge (which builds a loop-local engine per call) reads the very rows the test wrote.
    """
    url = f"sqlite+aiosqlite:///{(tmp_path / 'v11_compliance.db').as_posix()}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    _arm(monkeypatch, url)
    yield factory
    await engine.dispose()


def _configured_company() -> CompanyConfig:
    return CompanyConfig(name="Acme Robotics", sender_legal_name=LEGAL_NAME,
                         sender_postal_address=POSTAL_ADDRESS, website_url=WEBSITE)


# ── 1. the agent's real send path carries the footer + a working opt-out link ───────────────────


async def test_agent_send_goes_through_the_compliant_path_with_a_working_opt_out_link(
    file_db, monkeypatch,
):
    """Mode 2 (no preflight in the context): the guarded sender runs on the bridge's session.

    The body the provider is handed must carry the whole CAN-SPAM floor — legal name, postal
    address, privacy URL, the AI disclosure — AND a one-click opt-out URL that resolves to the
    public endpoint (pinned reachable below). Before V11 this leg produced a bare body: the agent
    called `integrations.send_email` itself.
    """
    fake = _FakeSendGrid().install(monkeypatch)
    async with file_db() as db:
        db.add(_configured_company())
        await db.commit()

    out = _run(_stub_collection(EmailOutreachAgent(), _send_payload()))

    assert len(fake.sent) == 1                                   # exactly one real provider send
    body = str(fake.sent[0]["plain_text_content"])
    assert "the first touch" in body                             # the agent's own body survives
    assert LEGAL_NAME in body                                    # CAN-SPAM: sender legal name
    assert POSTAL_ADDRESS in body                                # CAN-SPAM: postal address
    assert PRIVACY_URL in body                                   # privacy link (website_url path)
    assert AI_ASSISTANCE_DISCLOSURE in body                      # EU AI Act Art. 50 line
    assert "unsubscribe" in body.lower()                         # clear opt-out line

    link = TOKEN_RE.search(body)
    assert link is not None, f"no one-click opt-out URL in the footer: {body!r}"
    assert f"{WEBSITE}{UNSUBSCRIBE_PATH}" in body                # absolute, same-origin URL
    assert link.group(1)                                         # ... with a non-empty token

    assert out["emails"][0]["status"] == "sent"
    assert out["emails"][0]["sendgrid_id"] == "sg-msg-1"          # the provider's OWN id
    assert out["refusals"] == []


async def test_the_sent_link_unsubscribes_durably_and_the_next_send_is_refused(file_db, monkeypatch):
    """The end-to-end opt-out loop: the link in the SENT body flips the prospect, durably.

    Then the very next send to that address is refused by the EXISTING `unsubscribed` check
    (`E_POLICY_DENIED`, `send_compliant_email` step 2) and the provider is not reached again.
    """
    from app.api.v1.outreach import outreach_unsubscribe
    from app.services.email_compliance import verify_unsubscribe_token

    fake = _FakeSendGrid()
    async with file_db() as db:
        db.add(_configured_company())
        await db.commit()

        # (a) the first touch really goes out, carrying the link
        fake.install(monkeypatch)
        out = _run(_stub_collection(EmailOutreachAgent(), _send_payload()))
        assert out["emails"][0]["status"] == "sent"
        token = TOKEN_RE.search(str(fake.sent[0]["plain_text_content"])).group(1)
        assert verify_unsubscribe_token(token).lower() == "prospect@example.com"

        # (b) the recipient clicks it — no session of theirs, no login, no owner key
        page = await outreach_unsubscribe(token=token, db=db)
        assert page.status_code == 200
        await db.commit()

        stored = (await db.execute(
            select(Prospect).where(Prospect.email == "prospect@example.com"))).scalars().one()
        assert stored.status == "unsubscribed"                   # DURABLE: the row itself moved

        # (c) the next touch to that address is refused by the existing suppression check
        second = _run(_stub_collection(EmailOutreachAgent(), _send_payload()))
        assert second["emails"][0]["status"] == "failed"
        assert second["refusals"][0]["error"]["code"] == E_POLICY_DENIED
        assert "unsubscribed" in second["refusals"][0]["error"]["message"]
        assert len(fake.sent) == 1                                # the provider was never re-reached



# ── 2. the refusal legs: named, fail-closed, provider never reached ────────────────────────────


async def test_missing_sender_identity_refuses_by_name_and_names_the_fields(file_db, monkeypatch):
    """Unconfigured identity -> E_CONFIG naming all three fields; ZERO provider sends.

    The refusal is on the run output (a named entry, never a silent drop) and `prospects` is not
    created for a mail that never left.
    """
    fake = _FakeSendGrid().install(monkeypatch)
    async with file_db() as db:
        db.add(CompanyConfig(name="Acme Robotics"))      # the 0008 fields default to ''
        await db.commit()

    out = _run(_stub_collection(EmailOutreachAgent(), _send_payload()))

    assert fake.sent == []                                       # never reached the provider
    assert out["emails"][0]["status"] == "failed"
    refusal = out["refusals"][0]["error"]
    assert refusal["code"] == E_CONFIG
    assert "sender identity not configured" in refusal["message"]
    assert refusal["context"]["missing"] == SENDER_FIELDS        # the exact fields, by name
    assert refusal["how_to_fix"]


async def test_a_suppressed_address_in_the_preflight_is_refused_without_a_provider_call(monkeypatch):
    _arm(monkeypatch, "postgresql+asyncpg://polsia:polsia_password@127.0.0.1:1/polsia")
    """Mode 1 leg (b): the worker-resolved preflight suppresses an address -> E_POLICY_DENIED.

    The preflight is used when the context carries it; nothing here can reach a database (the
    settings URL is deliberately unusable), so the refusal proves the suppression check ran on the
    preflight itself.
    """
    fake = _FakeSendGrid().install(monkeypatch)
    preflight = {
        "company": {"sender_legal_name": LEGAL_NAME, "sender_postal_address": POSTAL_ADDRESS,
                    "website_url": WEBSITE},
        "suppressed": ["prospect@example.com"],
    }
    out = _run(_stub_collection(EmailOutreachAgent(), _send_payload()),
               {"compliance": preflight})

    assert fake.sent == []
    assert out["emails"][0]["status"] == "failed"
    assert out["refusals"][0]["error"]["code"] == E_POLICY_DENIED


async def test_a_worker_resolved_preflight_sends_without_any_database(monkeypatch):
    """Mode 1 leg (a): the preflight alone is enough — no DB, no session, footer on the wire.

    This is the `channel_sends` pattern: the worker owns the DB and hands the agent what it needs.
    `database_url` is deliberately unusable, so a send here proves mode 1 does not touch the DB.
    """
    fake = _FakeSendGrid().install(monkeypatch)
    _arm(monkeypatch, "postgresql+asyncpg://polsia:polsia_password@127.0.0.1:1/polsia")
    preflight = {
        "company": {"sender_legal_name": LEGAL_NAME, "sender_postal_address": POSTAL_ADDRESS,
                    "website_url": WEBSITE},
        "suppressed": [],
    }
    out = _run(_stub_collection(EmailOutreachAgent(), _send_payload()),
               {"compliance": preflight})

    assert out["emails"][0]["status"] == "sent"
    body = str(fake.sent[0]["plain_text_content"])
    assert LEGAL_NAME in body and POSTAL_ADDRESS in body and PRIVACY_URL in body
    assert TOKEN_RE.search(body) is not None


async def test_a_broken_preflight_refuses_by_name_instead_of_sending_non_compliant_mail(monkeypatch):
    _arm(monkeypatch, "postgresql+asyncpg://polsia:polsia_password@127.0.0.1:1/polsia")
    """A preflight that cannot produce a footer (no identity fields) refuses — never a bare send."""
    fake = _FakeSendGrid().install(monkeypatch)
    out = _run(_stub_collection(EmailOutreachAgent(), _send_payload()),
               {"compliance": {"company": {}, "suppressed": []}})

    assert fake.sent == []
    assert out["emails"][0]["status"] == "failed"
    refusal = out["refusals"][0]["error"]
    assert refusal["code"] == E_CONFIG
    assert refusal["context"]["missing"] == SENDER_FIELDS


# ── 3. the unsubscribe producer: public, durable, non-guessable ────────────────────────────────


async def test_the_unsubscribe_route_is_public_and_the_prospects_route_still_needs_the_key(
    api_client, async_db_session,
):
    """NO owner key on the unsubscribe route (a recipient has none); the read route keeps its auth.

    Both halves matter: the opt-out must not require a login, and moving the auth dependency to
    per-route (the `finance.py` pattern, whose `/stripe/webhook` is unauthenticated the same way)
    must not OPEN the owner surface.
    """
    db = async_db_session
    db.add(Prospect(email="recipient@example.com", status="new"))
    await db.commit()
    token = unsubscribe_token("recipient@example.com")

    resp = await api_client.get(f"{UNSUBSCRIBE_PATH}?token={token}")   # no headers at all
    assert resp.status_code == 200
    assert "unsubscribed" in resp.text.lower()

    stored = (await db.execute(
        select(Prospect).where(Prospect.email == "recipient@example.com"))).scalars().one()
    assert stored.status == "unsubscribed"

    guarded = await api_client.get("/api/v1/outreach/prospects")
    assert guarded.status_code == 401                                        # still owner-only
    assert guarded.json()["error"]["code"] == "E_AUTH"


async def test_a_forged_token_is_refused_and_changes_nothing(api_client, async_db_session):
    """A tampered/garbage token is a NAMED refusal (E_VALIDATION) with no state change.

    The token is an HMAC over the address: guessing an address must not unsubscribe it, and a
    malformed token must not become a 500.
    """
    db = async_db_session
    db.add(Prospect(email="victim@example.com", status="new"))
    await db.commit()
    good = unsubscribe_token("victim@example.com")

    for bad in ("", "not-a-token", good[:-2] + "xx", good.split(".")[0] + ".deadbeef"):
        resp = await api_client.get(f"{UNSUBSCRIBE_PATH}?token={bad}")
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == E_VALIDATION

    still = (await db.execute(
        select(Prospect).where(Prospect.email == "victim@example.com"))).scalars().one()
    assert still.status == "new"                                             # nothing moved


async def test_unsubscribe_records_the_opt_out_even_with_no_prospect_row_yet(
    api_client, async_db_session,
):
    """A recipient of a list-send can opt out before the row exists — the opt-out is durable.

    `record_sent_emails` creates prospects as a side effect of real sends, so an address can be
    missing while its mail is in flight; the opt-out must still be recorded (never a 404) or the
    next send would go out to someone who already said stop.
    """
    db = async_db_session
    assert (await db.execute(select(func.count()).select_from(Prospect))).scalar() == 0

    resp = await api_client.get(
        f"{UNSUBSCRIBE_PATH}?token={unsubscribe_token('future@example.com')}")
    assert resp.status_code == 200

    stored = (await db.execute(
        select(Prospect).where(Prospect.email == "future@example.com"))).scalars().one()
    assert stored.status == "unsubscribed"


async def test_the_unsubscribe_token_is_purpose_bound_and_stable():
    """One address -> one stable token; a different address -> a different token (no collisions)."""
    assert unsubscribe_token("a@example.com") == unsubscribe_token("A@Example.com")
    assert unsubscribe_token("a@example.com") != unsubscribe_token("b@example.com")

    from app.services.email_compliance import verify_unsubscribe_token

    with pytest.raises(PolsiaError) as refusal:
        verify_unsubscribe_token("garbage")
    assert refusal.value.code == E_VALIDATION


# ── 4. the source-level pin: the B9 bypass cannot come back ────────────────────────────────────


def test_the_outreach_send_path_has_no_direct_provider_call() -> None:
    """`integrations.send_email` is unreachable from the outreach agent — pinned at source level.

    B9's measured defect was exactly this line in `agent.py`; the only provider call for outreach
    mail now lives behind the guarded sender in `email_compliance`.
    """
    import inspect

    import app.agents.email_outreach.agent as agent_module
    import app.agents.email_outreach.compliance_send as send_module
    from app.services import email_compliance

    agent_src = inspect.getsource(agent_module)
    send_src = inspect.getsource(send_module)
    compliance_src = inspect.getsource(email_compliance)

    assert "integrations.send_email(" not in agent_src, "the B9 bypass is back in the agent"
    assert "integrations.send_email(" not in send_src, (
        "the send path must reach the provider through send_compliant_email only")
    assert "send_compliant_email(" in send_src
    assert "send_compliant_email(" in compliance_src
    assert "integrations.send_email(" in compliance_src        # the ONE guarded provider edge
