"""4-01 (S5-01, #1 LOCKED) — the customer_support compliance bridge — RED-first legs.

BUG-DIGEST §A1 measured the legal-risk seam: `customer_support/agent.py` called
`integrations.send_email` DIRECTLY, so CS replies bypassed the whole compliance floor (footer,
AI disclosure, sender identity, suppression) that V11/W1 built for outreach. This suite pins the
fix, mirroring `test_v11_compliance_outreach.py`:

  * EVERY CS reply send goes through `customer_support/send_bridge.py` ->
    `email_compliance.send_compliant_email` — the footer floor (legal name, postal address,
    opt-out, privacy URL, AI disclosure) is on the wire, and a direct `integrations.send_email`
    call is pinned ABSENT from the whole package at source + AST level (the A1 bypass);
  * both resolution modes are pinned: mode 1 = the worker-resolved preflight under
    `send_bridge.PREFLIGHT_KEY` (the `channel_sends` pattern — no DB in this thread), mode 2 =
    a loop-local session the bridge opens and disposes (fail-closed, no silent compliance skip);
  * refusals are NAMED on the run output and the provider is never reached: E_CONFIG for an
    unconfigured sender identity (naming the missing fields), E_POLICY_DENIED for a suppressed
    recipient.

Doubles live only here (the repo's own pattern): the SendGrid SDK module, the settings seam, and
a FILE-backed SQLite the bridge's own engine needs for mode 2. No live provider, no real email.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import sys
import types
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agents.customer_support.send_bridge as send_bridge
import app.config as app_config
import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.agents.customer_support.agent import CustomerSupportAgent
from app.agents.email_outreach import compliance_send
from app.core.database import Base
from app.core.errors import E_CONFIG, E_POLICY_DENIED
from app.models.company import CompanyConfig
from app.services.email_compliance import AI_ASSISTANCE_DISCLOSURE, UNSUBSCRIBE_PATH

LEGAL_NAME = "Acme Robotics LLC"
POSTAL_ADDRESS = "123 Main St, Minneapolis, MN 55401, USA"
WEBSITE = "https://acme.example"
PRIVACY_URL = f"{WEBSITE}/privacy"
SECRET = "unit-test-approval-secret"
SENDER_FIELDS = ["sender_legal_name", "sender_postal_address", "website_url"]


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch: Any):
    """Arm the opt-out HMAC secret (`settings.approval_secret`) for every leg — the footer's
    one-click opt-out link is refused without it (see `email_compliance._unsubscribe_secret`)."""
    monkeypatch.setattr(app_config.settings, "approval_secret", SECRET)


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
    agent.collect_structured = lambda task, context, **kw: (dict(payload), None)
    return agent


def _reply_payload(address: str = "customer@example.com") -> dict[str, Any]:
    return {
        "summary": "1 unanswered email",
        "replies": [{
            "message_ref": "msg-1",
            "from": address,
            "subject": "broken login",
            "reply_body": "we are on it",
            "proposed_action": "send_reply",
        }],
    }


def _run(agent: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = {"channel_sends": {"email": {"today": 0, "hour": 0}}}
    ctx.update(context or {})
    return agent.run({"title": "answer the inbox"}, ctx)


def _arm(monkeypatch: Any, database_url: str) -> None:
    """Real send posture: no sandbox, SendGrid configured, the DB the bridge must open."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(app_config.settings, "sendgrid_api_key", "SG.test-key")
    monkeypatch.setattr(app_config.settings, "sendgrid_from_email", "ops@example.com")
    monkeypatch.setattr(app_config.settings, "approval_secret", SECRET)
    monkeypatch.setattr(app_config.settings, "database_url", database_url)


@pytest.fixture
async def file_db(tmp_path: Any, monkeypatch: Any):
    """A FILE-backed sqlite the bridge's own engine can open — the mode-2 production shape."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'v15_cs_bridge.db').as_posix()}"
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


def _preflight(suppressed: list[str] | None = None) -> dict[str, Any]:
    return {
        "company": {"sender_legal_name": LEGAL_NAME, "sender_postal_address": POSTAL_ADDRESS,
                    "website_url": WEBSITE},
        "suppressed": list(suppressed or []),
    }


# ── 1. the CS send path carries the whole compliance floor ─────────────────────────────────────


async def test_cs_reply_send_carries_the_can_spam_floor_and_the_provider_id(
    file_db, monkeypatch,
):
    """Mode 2 (no preflight): the bridge's own session runs the guarded sender for a CS reply.

    Before this fix the CS agent called `integrations.send_email` itself: no footer, no
    disclosure, no suppression. The body the provider is handed must now carry the CAN-SPAM
    floor + the AI disclosure, and the reply must record the provider's OWN message id.
    """
    fake = _FakeSendGrid().install(monkeypatch)
    async with file_db() as db:
        db.add(_configured_company())
        await db.commit()

    out = _run(_stub_collection(CustomerSupportAgent(), _reply_payload()))

    assert len(fake.sent) == 1                                   # exactly one real provider send
    mail = fake.sent[0]
    body = str(mail["plain_text_content"])
    assert "we are on it" in body                                # the agent's own reply survives
    assert LEGAL_NAME in body                                    # CAN-SPAM: sender legal name
    assert POSTAL_ADDRESS in body                                # CAN-SPAM: postal address
    assert PRIVACY_URL in body                                   # privacy link (website_url path)
    assert AI_ASSISTANCE_DISCLOSURE in body                      # EU AI Act Art. 50 line
    assert "unsubscribe" in body.lower()                         # clear opt-out line
    assert f"{WEBSITE}{UNSUBSCRIBE_PATH}" in body                # the one-click opt-out URL
    assert mail["to_emails"] == "customer@example.com"
    assert mail["subject"] == "Re: broken login"

    assert out["replies"][0]["status"] == "sent"
    assert out["replies"][0]["sendgrid_id"] == "sg-msg-1"        # the provider's OWN id
    assert out["refusals"] == []
    assert out["side_effects"][0]["integration"]["ok"] is True


async def test_a_worker_resolved_preflight_sends_the_cs_reply_without_any_database(monkeypatch):
    """Mode 1: the preflight alone is enough — `database_url` is deliberately unusable here."""
    fake = _FakeSendGrid().install(monkeypatch)
    _arm(monkeypatch, "postgresql+asyncpg://polsia:polsia_password@127.0.0.1:1/polsia")

    out = _run(_stub_collection(CustomerSupportAgent(), _reply_payload()),
               {"compliance": _preflight()})

    assert out["replies"][0]["status"] == "sent"
    body = str(fake.sent[0]["plain_text_content"])
    assert LEGAL_NAME in body and POSTAL_ADDRESS in body and PRIVACY_URL in body
    assert AI_ASSISTANCE_DISCLOSURE in body


# ── 2. refusal legs: named on the run output, provider never reached ───────────────────────────


async def test_a_suppressed_customer_refuses_the_cs_reply_by_name(monkeypatch):
    """The suppressed address in the preflight refuses E_POLICY_DENIED before any provider call."""
    fake = _FakeSendGrid().install(monkeypatch)
    _arm(monkeypatch, "postgresql+asyncpg://polsia:polsia_password@127.0.0.1:1/polsia")

    out = _run(_stub_collection(CustomerSupportAgent(), _reply_payload()),
               {"compliance": _preflight(suppressed=["customer@example.com"])})

    assert fake.sent == []                                       # the provider was never reached
    assert out["replies"][0]["status"] == "failed"
    refusal = out["refusals"][0]["error"]
    assert refusal["code"] == E_POLICY_DENIED
    assert "suppressed" in refusal["message"]


async def test_missing_sender_identity_refuses_the_cs_reply_by_name(file_db, monkeypatch):
    """Unconfigured identity -> E_CONFIG naming all three fields; ZERO provider sends."""
    fake = _FakeSendGrid().install(monkeypatch)
    async with file_db() as db:
        db.add(CompanyConfig(name="Acme Robotics"))          # the 0008 fields default to ''
        await db.commit()

    out = _run(_stub_collection(CustomerSupportAgent(), _reply_payload()))

    assert fake.sent == []
    assert out["replies"][0]["status"] == "failed"
    refusal = out["refusals"][0]["error"]
    assert refusal["code"] == E_CONFIG
    assert "sender identity not configured" in refusal["message"]
    assert refusal["context"]["missing"] == SENDER_FIELDS        # the exact fields, by name
    assert refusal["how_to_fix"]


# ── 3. the frozen interface (H1 -> E3) + the source-level pin (the A1 bypass cannot come back) ─


def test_the_bridge_preflight_key_is_the_one_outreach_contract() -> None:
    """H1's frozen interface: CS consumes the SAME worker-resolved preflight key as outreach, so
    E3's enlistment flip (`agent_tasks._with_compliance_preflight`) needs ONE key, not two."""
    assert send_bridge.PREFLIGHT_KEY == compliance_send.PREFLIGHT_KEY == "compliance"
    signature = inspect.signature(send_bridge.send_cs_reply)
    for name in ("to", "subject", "body", "context"):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
    assert signature.parameters["context"].default is None


def test_cs_send_path_has_no_direct_provider_call() -> None:
    """`integrations.send_email` is unreachable from the customer_support package — source AND
    AST level (A1's measured defect was exactly that call; the compliance floor is the only path)."""
    package = pathlib.Path(inspect.getfile(CustomerSupportAgent)).parent
    sources = {path.name: path.read_text(encoding="utf-8") for path in package.glob("*.py")}
    assert "send_bridge.py" in sources

    for name, source in sources.items():
        assert "integrations.send_email(" not in source, f"{name}: the A1 bypass is back"

    offenders: list[str] = []
    for name, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send_email"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "integrations"
            ):
                offenders.append(f"{name}:{node.lineno}")
    assert offenders == [], f"direct provider calls in the CS package: {offenders}"

    bridge_src = sources["send_bridge.py"]
    assert "send_compliant_email(" in bridge_src
    assert "PREFLIGHT_KEY" in bridge_src
    assert "send_cs_reply(" in sources["agent.py"]
