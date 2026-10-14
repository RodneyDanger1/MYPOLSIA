"""The email compliance floor — one footer builder + one guarded sender path (V9/B3).

Plan §9 B3 + Appendix A4.  Three rules, enforced by construction:

* **The CAN-SPAM floor.**  Every outreach email carries — as a footer — the sender's legal
  name, its postal address, a clear opt-out line and a privacy URL, built from the 0008
  `company_config` fields (`sender_legal_name`, `sender_postal_address`) plus `website_url`
  (the privacy link's host).  Appendix A4: a non-compliant email is up to **$53,088** of civil
  penalty, enforced PER EMAIL, so an incomplete footer is not a cosmetic defect.
* **Refuse, never send non-compliant mail.**  `footer_for_company()` REFUSES to build a footer
  while the sender identity is unset, and the sender path (`send_compliant_email`) raises the
  same `E_CONFIG` "sender identity not configured" — with the operator's fix in `how_to_fix` —
  INSTEAD of reaching the provider.  A silently non-compliant send is the one outcome this
  module exists to prevent.
* **Suppression is honored in the send path.**  A prospect stored `prospects.status ==
  "unsubscribed"` is refused by name (`E_POLICY_DENIED`) before any provider call; the reply
  side of the same rule lives in `support_inbox_service` (replied-stamping) and the follow-up
  sweep's `replied_at IS NULL` filter.

The AI-assistance disclosure is ONE sourced constant, ON by default — EU AI Act **Art. 50**
has been applicable since **2026-08-02** (Cooley, fetched; plan Appendix A4: chatbots must
disclose they are AI, fines up to €15M or 3% of worldwide turnover, and the duty follows the
OUTPUT wherever it is used in the EU, so outreach carries the line by default).

Callers: any path that sends company mail for real (the invoice firewalled path, an operator
drill) routes through `send_compliant_email`; the footer builder is also safe to call for
previews and dry-runs — it is pure and reads only the values handed to it.
"""
from __future__ import annotations

import logging
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import integrations
from app.core.errors import E_CONFIG, E_POLICY_DENIED, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.outreach import Prospect
from app.services.query_helpers import fetch_scalar

logger = logging.getLogger(__name__)


#: The one sourced AI-assistance disclosure line (plan Appendix A4, self-checked 2026-10-14):
#: EU AI Act Art. 50 has been applicable since 2026-08-02 (Cooley, fetched) — AI-generated text
#: must be disclosed and the duty follows the output wherever it is used in the EU; fines run to
#: €15M or 3% of worldwide turnover.  Polsia's outreach is AI-drafted, so every footer carries
#: this line BY DEFAULT; a caller turns it off only where provably no EU nexus exists.  ONE
#: constant on purpose — every footer in the tree reads the same bytes.
AI_ASSISTANCE_DISCLOSURE: Final[str] = (
    "This email was written and sent with AI assistance."
)

#: CAN-SPAM's clear-and-conspicuous opt-out (Appendix A4: "clear opt-out honored within 10
#: business days").  A reply is honored two ways: `support_inbox_service.submit_item` stamps
#: `email_logs.replied_at` for the matching prospect (reply stops the chain), and
#: `send_compliant_email` refuses any address stored `unsubscribed`.
OPT_OUT_LINE: Final[str] = (
    'To opt out: reply to this email with "unsubscribe" and we will stop within 10 business days.'
)

#: The privacy link is derived, never invented: `website_url` + this path.
PRIVACY_PATH: Final[str] = "/privacy"

#: The pinned refusal message (plan §9 B3): the sender identity is the 0008 pair.
SENDER_UNCONFIGURED_MESSAGE: Final[str] = "sender identity not configured"

#: The one operator fix worth writing down twice (footer builder + sender path read the same).
_SENDER_HOW_TO_FIX: Final[str] = (
    "set sender_legal_name and sender_postal_address (and website_url, for the privacy link) "
    "in the company config — Settings -> Company, PUT /api/v1/config — then retry the send"
)


def _field(company: object, name: str) -> str:
    """One 0008 field off a `CompanyConfig` row OR a plain mapping — stripped, `""` when unset.

    Both shapes are accepted because the sender path reads the ORM row while previews and
    tests hand in dicts; a missing attribute and an empty value are the same state here
    ("not configured"), which is exactly what the refusal below reports.
    """
    if company is None:
        return ""
    value = company.get(name) if isinstance(company, dict) else getattr(company, name, None)
    return str(value).strip() if value is not None else ""


def footer_for_company(company: object, *, include_ai_disclosure: bool = True) -> str:
    """The compliance footer for `company`, or a named refusal while the identity is unset.

    REQUIRED (each missing one is listed in the refusal's context): `sender_legal_name`,
    `sender_postal_address` — the CAN-SPAM pair the 0008 migration added — and `website_url`,
    because the footer contract carries a privacy link and a footer without it is incomplete.
    `include_ai_disclosure` defaults ON (plan Appendix A4 / AI Act Art. 50).
    """
    legal_name = _field(company, "sender_legal_name")
    postal_address = _field(company, "sender_postal_address")
    website_url = _field(company, "website_url")
    missing = [
        name
        for name, value in (
            ("sender_legal_name", legal_name),
            ("sender_postal_address", postal_address),
            ("website_url", website_url),
        )
        if not value
    ]
    if missing:
        raise PolsiaError(
            E_CONFIG,
            SENDER_UNCONFIGURED_MESSAGE,
            how_to_fix=_SENDER_HOW_TO_FIX,
            context={"missing": missing, "source": "company_config (migration 0008)"},
        )
    lines = [
        legal_name,
        postal_address,
        OPT_OUT_LINE,
        f"Privacy: {website_url.rstrip('/')}{PRIVACY_PATH}",
    ]
    if include_ai_disclosure:
        lines.append(AI_ASSISTANCE_DISCLOSURE)
    return "\n".join(lines)


async def send_compliant_email(
    db: AsyncSession,
    *,
    to: str,
    subject: str,
    body: str,
    include_ai_disclosure: bool = True,
) -> dict[str, Any]:
    """The guarded sender path: footer + suppression checks, THEN the provider call.

    Order matters and is the point of this function:
      1. the footer is built FIRST — while `sender_legal_name` / `sender_postal_address` /
         `website_url` are unset the call raises `E_CONFIG` "sender identity not configured"
         and the provider is never reachable (never a silently non-compliant send);
      2. suppression is checked against the stored prospect row — `unsubscribed` raises
         `E_POLICY_DENIED` by name, before any provider contact;
      3. only then is the composed body (`body` + blank line + footer) handed to
         `integrations.send_email`, which itself refuses by name on missing credentials.

    Returns the integration result verbatim (`{"ok": True, "result": {...}}`, or the named
    refusal dict) so callers key on the same shape as every other provider call.  Reads only;
    writes nothing — recording a performed send stays the caller's job
    (`outreach_service.record_sent_emails`).
    """
    from app.services.company_service import get_company

    address = (to or "").strip()
    if not address:
        raise PolsiaError(
            E_VALIDATION,
            "outreach refuses an empty recipient address",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"to": to},
        )

    footer = footer_for_company(
        await get_company(db), include_ai_disclosure=include_ai_disclosure
    )

    prospect = await fetch_scalar(
        db,
        select(Prospect).where(func.lower(Prospect.email) == address.lower()),
        what=f"prospect {address!r}",
    )
    if prospect is not None and (prospect.status or "").strip().lower() == "unsubscribed":
        raise PolsiaError(
            E_POLICY_DENIED,
            f"outreach to {address!r} is suppressed: prospect status is 'unsubscribed'",
            how_to_fix=(
                "honor the opt-out; clear it only at the prospect's own request "
                "(update prospects.status), then retry"
            ),
            context={"prospect_id": int(prospect.id), "status": prospect.status},
        )

    composed = "\n\n".join(part for part in (body.strip(), footer) if part)
    return integrations.send_email(to=address, subject=subject, body=composed)


__all__ = [
    "AI_ASSISTANCE_DISCLOSURE",
    "OPT_OUT_LINE",
    "PRIVACY_PATH",
    "SENDER_UNCONFIGURED_MESSAGE",
    "footer_for_company",
    "send_compliant_email",
]
