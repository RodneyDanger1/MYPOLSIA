"""V9/B3 — the email compliance floor: `footer_for_company` + the guarded sender path.

Plan §9 B3 (frozen plan 474-490) + Appendix A4. What this file pins:

* `footer_for_company()` builds the CAN-SPAM floor of every outreach email — sender legal
  name, postal address, opt-out line, privacy URL (from the 0008 `company_config` fields
  `sender_legal_name` / `sender_postal_address` + `website_url`) — plus the AI-assistance
  disclosure (EU AI Act Art. 50, applicable since 2026-08-02; ONE sourced constant, ON by
  default).
* It REFUSES (`E_CONFIG`, "sender identity not configured") while the identity is unset —
  and the sender path (`send_compliant_email`) therefore refuses REAL sends instead of
  sending non-compliant mail: the provider double records ZERO sends in every refusal leg.
* Suppression is honored in the send path: a prospect stored `unsubscribed` is refused by
  name and the provider is never called.

Doubles live only here: the SendGrid SDK module (the repo's own pattern —
`test_r30_email_producer_truth.py`) and the settings seam. Every DB row is written by the
REAL service on the real (sqlite) schema; no live model calls, no real emails.
"""
from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from sqlalchemy import select

import app.config as app_config
from app.core.errors import E_CONFIG, E_POLICY_DENIED, PolsiaError
from app.models.company import CompanyConfig
from app.models.outreach import Prospect
from app.services.company_service import get_company
from app.services.email_compliance import (
    AI_ASSISTANCE_DISCLOSURE,
    footer_for_company,
    send_compliant_email,
)

LEGAL_NAME = "Acme Robotics LLC"
POSTAL_ADDRESS = "123 Main St, Minneapolis, MN 55401, USA"
WEBSITE = "https://acme.example"
PRIVACY_URL = f"{WEBSITE}/privacy"


def _configured_company(db: Any) -> CompanyConfig:
    """The 0008 sender-identity fields, fully configured — the compliant state."""
    return CompanyConfig(
        name="Acme Robotics",
        sender_legal_name=LEGAL_NAME,
        sender_postal_address=POSTAL_ADDRESS,
        website_url=WEBSITE,
    )


class _FakeSendGrid:
    """Provider double: 202 + a unique X-Message-Id per send, capturing every message.

    Same shape as `test_r30_email_producer_truth.py`'s double (202 + `X-Message-Id`), plus
    the capture this file needs: the composed `plain_text_content` the provider was handed.
    """

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


def _arm(monkeypatch: Any) -> None:
    """Real send path: no sandbox, SendGrid configured (the R30 `_arm` shape)."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(app_config.settings, "sendgrid_api_key", "SG.test-key")
    monkeypatch.setattr(app_config.settings, "sendgrid_from_email", "ops@acme.example")


# ── footer_for_company: the compliant footer, from the 0008 fields ───────────────────────────


async def test_footer_carries_name_address_opt_out_privacy_url_and_the_ai_line(
    async_db_session,
) -> None:
    """All five parts, built from the REAL company row (identity + website)."""
    db = async_db_session
    db.add(_configured_company(db))
    await db.commit()

    company = await get_company(db)
    footer = footer_for_company(company)

    assert LEGAL_NAME in footer                     # legal name
    assert POSTAL_ADDRESS in footer                 # postal address (CAN-SPAM)
    assert "unsubscribe" in footer.lower()          # clear opt-out line
    assert PRIVACY_URL in footer                    # privacy URL derived from website_url
    assert AI_ASSISTANCE_DISCLOSURE in footer       # the AI-assistance disclosure line


def test_the_ai_disclosure_line_is_one_constant_and_default_on() -> None:
    """The disclosure is ONE sourced constant, included by default and opt-out-able."""
    assert isinstance(AI_ASSISTANCE_DISCLOSURE, str) and AI_ASSISTANCE_DISCLOSURE.strip()
    company = {
        "sender_legal_name": LEGAL_NAME,
        "sender_postal_address": POSTAL_ADDRESS,
        "website_url": WEBSITE,
    }
    assert AI_ASSISTANCE_DISCLOSURE in footer_for_company(company)
    assert AI_ASSISTANCE_DISCLOSURE not in footer_for_company(
        company, include_ai_disclosure=False
    )


async def test_footer_refuses_by_name_when_sender_identity_is_unconfigured(
    async_db_session,
) -> None:
    """Empty name/address -> `E_CONFIG` "sender identity not configured", never a footer."""
    db = async_db_session
    db.add(CompanyConfig(name="Acme Robotics"))     # 0008 fields default to ''
    await db.commit()

    with pytest.raises(PolsiaError) as unconfigured:
        footer_for_company(await get_company(db))
    assert unconfigured.value.code == E_CONFIG
    assert "sender identity not configured" in unconfigured.value.message
    assert unconfigured.value.how_to_fix

    with pytest.raises(PolsiaError) as no_company:  # no row at all: the same refusal
        footer_for_company(None)
    assert no_company.value.code == E_CONFIG
    assert "sender identity not configured" in no_company.value.message


# ── the sender path: refuses REAL sends while unconfigured, sends compliantly when set ───────


async def test_send_path_refuses_a_real_send_while_sender_identity_is_unset(
    async_db_session, monkeypatch,
) -> None:
    """The E_CONFIG posture, proven end-to-end: refusal happens INSTEAD of a send.

    No company row -> the sender path raises before the provider is reachable, and the
    provider double records ZERO sends — never a silently non-compliant send.
    """
    _arm(monkeypatch)
    fake = _FakeSendGrid().install(monkeypatch)
    db = async_db_session

    with pytest.raises(PolsiaError) as refusal:
        await send_compliant_email(
            db, to="prospect@example.com", subject="hello", body="first touch"
        )
    assert refusal.value.code == E_CONFIG
    assert "sender identity not configured" in refusal.value.message
    assert fake.sent == []                          # zero provider calls


async def test_send_path_sends_with_the_footer_appended_when_configured(
    async_db_session, monkeypatch,
) -> None:
    """Configured identity -> the REAL provider call carries body + footer, nothing less."""
    _arm(monkeypatch)
    fake = _FakeSendGrid().install(monkeypatch)
    db = async_db_session
    db.add(_configured_company(db))
    await db.commit()

    result = await send_compliant_email(
        db, to="prospect@example.com", subject="a real subject", body="the first touch body"
    )

    assert result["ok"] is True
    assert result["result"]["message_id"] == "sg-msg-1"
    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent["to_emails"] == "prospect@example.com"
    assert sent["subject"] == "a real subject"
    assert "the first touch body" in sent["plain_text_content"]
    for part in (LEGAL_NAME, POSTAL_ADDRESS, "unsubscribe", PRIVACY_URL,
                 AI_ASSISTANCE_DISCLOSURE):
        assert part in sent["plain_text_content"], f"missing {part!r} in the sent body"


async def test_send_path_suppresses_unsubscribed_prospects(
    async_db_session, monkeypatch,
) -> None:
    """A stored `unsubscribed` prospect is refused by name; the provider is never called."""
    _arm(monkeypatch)
    fake = _FakeSendGrid().install(monkeypatch)
    db = async_db_session
    db.add(_configured_company(db))
    db.add(Prospect(email="opted-out@example.com", status="unsubscribed"))
    await db.commit()

    with pytest.raises(PolsiaError) as suppressed:
        await send_compliant_email(
            db, to="opted-out@example.com", subject="hello", body="still here?"
        )
    assert suppressed.value.code == E_POLICY_DENIED
    assert "unsubscribed" in suppressed.value.message
    assert suppressed.value.how_to_fix
    assert fake.sent == []                          # suppression honored: no provider call

    # The refusal is about the STATUS, not the prospect's existence: the same address with
    # status 'new' sends normally (and the stored row is untouched by the refusal above).
    prospect = (
        await db.execute(select(Prospect).where(Prospect.email == "opted-out@example.com"))
    ).scalar_one()
    prospect.status = "new"
    await db.commit()
    result = await send_compliant_email(
        db, to="opted-out@example.com", subject="hello again", body="back on the list"
    )
    assert result["ok"] is True
    assert len(fake.sent) == 1
