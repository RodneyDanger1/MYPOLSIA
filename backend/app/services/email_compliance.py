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

import base64
import hashlib
import hmac
import logging
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import integrations
from app.config import settings
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

#: V11/W1 (audit B10): the ONE-CLICK opt-out.  CAN-SPAM wants a clear and conspicuous opt-out
#: that does not make the recipient log in, so the footer carries a signed URL to the public
#: route below — real, reachable without the owner key, and durable (it flips the prospect row
#: through the service layer).  Derived like the privacy link: `website_url` + this path.
UNSUBSCRIBE_PATH: Final[str] = "/api/v1/outreach/unsubscribe"

#: Purpose-separation for the opt-out signature: one secret must never sign two meanings, so a
#: token minted here cannot verify anywhere else.  Bumping the version retires every
#: outstanding link on purpose — the reply-to-opt-out clause stays as the eternal fallback.
_UNSUBSCRIBE_PURPOSE: Final[bytes] = b"polsia-outreach-unsubscribe:v1"

#: The pinned refusal message (plan §9 B3): the sender identity is the 0008 pair.
SENDER_UNCONFIGURED_MESSAGE: Final[str] = "sender identity not configured"

#: The one operator fix worth writing down twice (footer builder + sender path read the same).
_SENDER_HOW_TO_FIX: Final[str] = (
    "set sender_legal_name and sender_postal_address (and website_url, for the privacy link) "
    "in the company config — Settings -> Company, PUT /api/v1/config — then retry the send"
)


def _unsubscribe_secret(secret: str | None = None) -> str:
    """The signing secret — `settings.approval_secret` (the repo's one HMAC secret) or an override.

    NEVER a default: with no secret configured the token cannot be minted, and a guessable token on
    a public route would let anyone unsubscribe anyone — so the send refuses BY NAME instead.
    """
    value = (secret if secret is not None else (settings.approval_secret or "")).strip()
    if not value:
        raise PolsiaError(
            E_CONFIG,
            "no unsubscribe signing secret configured: the one-click opt-out link cannot be minted",
            how_to_fix=(
                "set APPROVAL_SECRET (the same HMAC secret the approvals channel uses) and retry the "
                "send — a footer without a working opt-out is not sent"
            ),
            context={"missing": ["APPROVAL_SECRET"], "source": "settings.approval_secret"},
        )
    return value


def _unsubscribe_key(secret: str) -> bytes:
    """The purpose-bound HMAC key for opt-out tokens (one secret, many meanings, never shared)."""
    return hashlib.sha256(_UNSUBSCRIBE_PURPOSE + b":" + secret.encode()).digest()


def unsubscribe_token(address: str, *, secret: str | None = None) -> str:
    """`<b64url(address)>.<b64url(hmac)>` for `address` — stable, non-guessable, stateless.

    Case-insensitive on purpose (one mailbox is one consent) and signed over the ADDRESS rather than
    a row id, so a recipient can opt out before `record_sent_emails` has created the row.
    """
    normalized = (address or "").strip().lower()
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise PolsiaError(
            E_VALIDATION,
            f"{address!r} is not a usable address to unsubscribe",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"address": address},
        )
    payload = normalized.encode()
    signature = hmac.new(_unsubscribe_key(_unsubscribe_secret(secret)), payload, hashlib.sha256).digest()
    encode = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")  # noqa: E731
    return f"{encode(payload)}.{encode(signature)}"


def verify_unsubscribe_token(token: str, *, secret: str | None = None) -> str:
    """The address `token` authorises, or a named `E_VALIDATION` refusal (never a silent accept).

    Malformed, truncated, forged and empty tokens are ONE refusal shape: a prober learns nothing
    from the difference, and no state changes on any of them.
    """
    def invalid(detail: str) -> PolsiaError:
        return PolsiaError(
            E_VALIDATION,
            "the unsubscribe link is not valid: its token is malformed or was not signed here",
            how_to_fix=(
                'use the link exactly as it arrived; if it was truncated by a mail client, reply to '
                'the email with "unsubscribe" instead — that path needs no token'
            ),
            context={"token": detail},
        )

    raw = (token or "").strip()
    if not raw or raw.count(".") != 1:
        raise invalid("empty or not <payload>.<signature>")
    encoded_payload, encoded_signature = raw.split(".")
    try:
        pad = lambda part: part + "=" * (-len(part) % 4)  # noqa: E731
        payload = base64.urlsafe_b64decode(pad(encoded_payload))
        signature = base64.urlsafe_b64decode(pad(encoded_signature))
    except Exception as exc:  # noqa: BLE001 — any decoding failure is the one refusal above
        raise invalid(f"not base64url: {type(exc).__name__}") from exc
    expected = hmac.new(_unsubscribe_key(_unsubscribe_secret(secret)), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise invalid("signature does not verify")
    address = payload.decode("utf-8", errors="replace").strip().lower()
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        raise invalid("payload is not an address")
    return address


def unsubscribe_url(website_url: str, address: str, *, secret: str | None = None) -> str:
    """The absolute one-click opt-out URL a footer carries (derived, never invented)."""
    return f"{str(website_url).rstrip('/')}{UNSUBSCRIBE_PATH}?token={unsubscribe_token(address, secret=secret)}"


def _opt_out_url_or_none(website_url: str, address: str) -> str | None:
    """The one-click opt-out URL for `address`, or None + a NAMED warning when it cannot be minted.

    With no signing secret the footer keeps the shipped reply-to-opt-out line — the opt-out route
    CAN-SPAM has always accepted — instead of stalling EVERY send behind an unset setting; the
    skip is logged by name so an install can see exactly what is missing (APPROVAL_SECRET).
    """
    try:
        return unsubscribe_url(website_url, address)
    except PolsiaError as exc:
        logger.warning(
            "code=opt_out_link_skipped: %s (the footer carries the reply-based opt-out instead)",
            exc.message,
        )
        return None


def opt_out_line(opt_out_url: str | None = None) -> str:
    """The footer's opt-out line: the one-click link when there is one, else the reply route.

    BOTH routes stay in the link form on purpose — a mail client that strips the URL must not turn
    an otherwise compliant footer into a linkless one.
    """
    if not opt_out_url:
        return OPT_OUT_LINE
    return (
        f"To opt out: open {opt_out_url} — one click, no login — or reply to this email with "
        '"unsubscribe"; we will stop within 10 business days.'
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


def footer_for_company(
    company: object,
    *,
    include_ai_disclosure: bool = True,
    opt_out_url: str | None = None,
) -> str:
    """The compliance footer for `company`, or a named refusal while the identity is unset.

    REQUIRED (each missing one is listed in the refusal's context): `sender_legal_name`,
    `sender_postal_address` — the CAN-SPAM pair the 0008 migration added — and `website_url`,
    because the footer contract carries a privacy link and a footer without it is incomplete.
    `include_ai_disclosure` defaults ON (plan Appendix A4 / AI Act Art. 50).

    V11/W1 (additive): `opt_out_url` — the one-click opt-out URL for THIS recipient —
    replaces the reply-only opt-out line.  Absent -> the shipped reply line verbatim, so
    every existing caller and pin reads the same bytes as before.
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
        opt_out_line(opt_out_url),
        f"Privacy: {website_url.rstrip('/')}{PRIVACY_PATH}",
    ]
    if include_ai_disclosure:
        lines.append(AI_ASSISTANCE_DISCLOSURE)
    return "\n".join(lines)


async def send_compliant_email(
    db: AsyncSession | None = None,
    *,
    to: str,
    subject: str,
    body: str,
    include_ai_disclosure: bool = True,
    preflight: dict[str, Any] | None = None,
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

    The composed footer carries the ONE-CLICK opt-out URL for `to`
    (`<website_url>/api/v1/outreach/unsubscribe?token=...`; V11/W1 — a recipient must not need a
    login to stop).

    V11/W1 (additive) `db=None`: the SESSION-FREE mode for callers that hold no DB session — the
    outreach agent runs in a worker thread, and the worker hands it a resolved `preflight`
    (`{"company": <identity mapping>, "suppressed": [addresses]}` — the `channel_sends` pattern:
    the worker owns the DB).  The SAME three steps run in the SAME order on that data, and an
    absent preflight raises the SAME `E_CONFIG` "sender identity not configured": no path through
    this function reaches the provider without a complete footer and a suppression check.

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

    if db is None:
        return _session_free_send(
            address=address, subject=subject, body=body,
            include_ai_disclosure=include_ai_disclosure, preflight=preflight,
        )

    company = await get_company(db)
    # The identity gate runs FIRST and unchanged (E_CONFIG naming the missing fields), so the
    # opt-out URL is only minted once there is a website to derive it from.
    footer_for_company(company, include_ai_disclosure=include_ai_disclosure)
    footer = footer_for_company(
        company,
        include_ai_disclosure=include_ai_disclosure,
        opt_out_url=_opt_out_url_or_none(_field(company, "website_url"), address),
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


def _session_free_send(
    *,
    address: str,
    subject: str,
    body: str,
    include_ai_disclosure: bool,
    preflight: dict[str, Any] | None,
) -> dict[str, Any]:
    """`send_compliant_email(db=None)`'s arm: the same three steps on a WORKER-RESOLVED preflight.

    Footer first (E_CONFIG naming the missing fields), suppression second (E_POLICY_DENIED by
    name), provider last — identical to the session path, and nothing here reads a database, so a
    session-less caller cannot silently skip the floor.  `outreach_service.compliance_preflight`
    is what builds this block for the worker.

    V12/F2 (D1): the suppression list's SHAPE is part of the floor.  The old code iterated
    whatever `preflight["suppressed"]` was handed, so a bare STRING iterated its characters, the
    membership test could never match, and an address the list named was SENT.  Now: a value
    that demonstrably names the address (any iterable — e.g. a mapping whose KEYS include it,
    W-D's hostile-shape floor) still refuses `E_POLICY_DENIED`; anything else — bare string, int,
    mapping without the address, absent/None — raises the named `E_CONFIG` refusal instead of
    ever being read as "nothing suppressed".
    """
    identity = preflight.get("company") if isinstance(preflight, dict) else None
    if identity is None:
        raise PolsiaError(
            E_CONFIG,
            SENDER_UNCONFIGURED_MESSAGE,
            how_to_fix=_SENDER_HOW_TO_FIX,
            context={
                "missing": ["sender_legal_name", "sender_postal_address", "website_url"],
                "source": "run context compliance preflight (absent)",
            },
        )
    footer_for_company(identity, include_ai_disclosure=include_ai_disclosure)
    footer = footer_for_company(
        identity,
        include_ai_disclosure=include_ai_disclosure,
        opt_out_url=_opt_out_url_or_none(_field(identity, "website_url"), address),
    )
    # V12 settle (parent): mypy narrowing — by contract a None preflight was already refused
    # upstream (mode (b) engine path); the assert also documents that this read may not see one.
    assert preflight is not None
    raw_suppressed = preflight.get("suppressed")
    matched = False
    if raw_suppressed is not None:
        try:
            matched = address.lower() in {
                str(item).strip().lower() for item in raw_suppressed
            }
        except TypeError:
            # Not iterable at all: it cannot be READ as a suppression list, and a list that
            # cannot be read must never read as an empty one — the shape floor below refuses
            # it by name.
            matched = False
    if matched:
        raise PolsiaError(
            E_POLICY_DENIED,
            f"outreach to {address!r} is suppressed: prospect status is 'unsubscribed'",
            how_to_fix=(
                "honor the opt-out; clear it only at the prospect's own request "
                "(update prospects.status), then retry"
            ),
            context={"status": "unsubscribed", "source": "run context suppression list"},
        )
    if not isinstance(raw_suppressed, (list, tuple)):
        raise PolsiaError(
            E_CONFIG,
            "the run context suppression list ('suppressed') is malformed: expected a list of "
            f"addresses, got {type(raw_suppressed).__name__} — refusing rather than sending with "
            "the suppression check unread",
            how_to_fix=(
                "fix the producer (`outreach_service.compliance_preflight` writes a sorted list "
                "of addresses under 'suppressed') and retry the send"
            ),
            context={
                "suppressed_type": type(raw_suppressed).__name__,
                "source": "run context compliance preflight",
            },
        )
    composed = "\n\n".join(part for part in (body.strip(), footer) if part)
    return integrations.send_email(to=address, subject=subject, body=composed)


__all__ = [
    "AI_ASSISTANCE_DISCLOSURE",
    "OPT_OUT_LINE",
    "PRIVACY_PATH",
    "SENDER_UNCONFIGURED_MESSAGE",
    "UNSUBSCRIBE_PATH",
    "footer_for_company",
    "opt_out_line",
    "send_compliant_email",
    "unsubscribe_token",
    "unsubscribe_url",
    "verify_unsubscribe_token",
]
