"""The email lifecycle's ONE state vocabulary and its provider-acceptance rule (WAVE 7).

WHY this module exists: the email path has four different stores that each hold one part of one
message's life, and nothing in the tree stated which part lives where — so "sent" could be claimed
by an agent that only *tried*, and a refusal could be indistinguishable from a draft.  The state
model below is the contract; `provider_accepted` is the single rule that decides whether a provider
call may be recorded as a send at all.

THE STATE MODEL (one message's life, and the durable home of each state)

    state       durable home                                  writer
    ----------  --------------------------------------------  ------------------------------------
    draft       `agent_runs.output.emails[].status`           the agent (status="draft"; NEVER an
                (plus `drafted_not_sent` / `sandbox` markers)  `email_logs` row — a draft was not a
                                                               send, so it must not consume a send)
    queued      `tasks` row, dedupe key `followup:<log id>`   `celery_app/tasks/followups.py`
                (the follow-up the chain will fire)
    sent        `email_logs.status="sent"` + `sendgrid_id`    `outreach_service.record_sent_emails`
    refused     `agent_runs.output.refusals[]` (named code)   the agent / the guarded sender; the
                                                               row half is the named skip lists in
                                                               `record_sent_emails`
    inbound     `support_inbox_items` (status pending/        `support_inbox_service`
                dispatched) + the per-delivery audit row
    reply       `email_logs.replied_at` (+ the inbox item)    `stamp_prospect_replied`
    follow-up   `email_logs.follow_up_due`                    `record_sent_emails` (from the run's
                                                               stated `follow_ups[]`)

Two invariants follow and are enforced elsewhere:

  * a row in `email_logs` with `status="sent"` is a PROVIDER-ACCEPTED send (this module's
    `provider_accepted` is what the caller asks before claiming it; migration 0019 adds the
    database-side check `status <> 'sent' OR sendgrid_id IS NOT NULL`, and the column default is
    `draft`, so an INSERT that forgets to state a status can never fabricate a send);
  * a state that is not a send carries no provider id and no `sent_at` — the writer's gate.

`EMAIL_ATTEMPT_STATUSES` mirrors `schemas.agent_outputs.EmailDraft.status` (the agent's output
contract); a value outside it is not a state this system knows, and the writer refuses it by name
instead of storing an unreadable word in a `VARCHAR(50)` column.
"""
from __future__ import annotations

from typing import Any, Final

#: The four states an ATTEMPT may carry in a run output (mirrors `EmailDraft.status`).
EMAIL_ATTEMPT_STATUSES: Final[tuple[str, ...]] = ("draft", "queued", "sent", "failed")

#: The states the DATABASE accepts for an `email_logs` row (migration 0002's
#: `ck_email_logs_status`, PostgreSQL-only — verified live 2026-10-29).  It is a SUPERSET of the
#: attempt vocabulary: the four delivery-event states (`delivered`, `opened`, `replied`, `bounced`)
#: are what a provider webhook would later write, and NO producer in this tree writes them today
#: (the `opened_at`/`replied_at` columns carry the two events the app can actually observe).  They
#: are named here so the gap is stated, never pretended: a `delivered` row would be a fabricated
#: event without a provider callback, so nothing writes one.
EMAIL_ROW_STATUSES: Final[tuple[str, ...]] = (
    "draft",
    "queued",
    "sent",
    "delivered",
    "opened",
    "replied",
    "bounced",
    "failed",
)

#: The only state that may become an `email_logs` ROW.  One word: a row IS a performed send.
EMAIL_ROW_SENT: Final[str] = "sent"

#: The column default migration 0019 installs.  A row that states no status claims the least.
EMAIL_ROW_DEFAULT_STATUS: Final[str] = "draft"

#: The provider status window that means "the provider accepted the message".
PROVIDER_ACCEPTED_MIN: Final[int] = 200
PROVIDER_ACCEPTED_MAX: Final[int] = 299


def coerce_status_code(raw: object) -> int | None:
    """A provider HTTP status as a positive int, or None when the value is not one.

    `True`/`False` are refused even though `bool` subclasses `int`: a boolean is not a status.
    `0`, negatives, floats and strings are None — "not stated", never a coerced guess.
    """
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw


def provider_status_code(result: object) -> int | None:
    """The provider's HTTP status from an `integrations.*` result, or None when not stated.

    `None` is "the adapter did not state a code" — an OBSERVATION, never `0` and never a guess.
    A non-integer or a non-positive code is also None: a provider call cannot answer "HTTP 0".
    """
    if not isinstance(result, dict):
        return None
    body = result.get("result")
    raw = body.get("status_code") if isinstance(body, dict) else None
    return coerce_status_code(raw)


def provider_message_id(result: object) -> str:
    """The provider's OWN per-message id from an `integrations.*` result (`` when none).

    Never the status code: a status code is not an id, and keying on it collapsed distinct sends
    onto one row (the R30/M3 measurement).  Whitespace-stripped; an empty string means "no id".
    """
    if not isinstance(result, dict):
        return ""
    body = result.get("result")
    raw = body.get("message_id") if isinstance(body, dict) else None
    return str(raw).strip() if raw is not None else ""


def provider_accepted(result: object) -> bool:
    """Did the provider ACCEPT this message?  The one rule every "sent" claim must pass.

    Accepted means one of exactly two evidenced shapes:

    * the adapter stated a real HTTP code and it is 2xx — the provider's own answer; or
    * the adapter stated NO code but returned the provider's per-message id — the id is itself
      acceptance evidence (a provider does not mint a message id for a message it refused).

    Everything else is NOT acceptance: a refusal dict (`ok` not True), a non-2xx code (a 4xx/5xx
    answer is a provider answer, and it is "no"), a stated code of 0/None with no id, or a shape
    that is not a dict at all.  The caller records those as `failed` with a named refusal; it must
    never let them reach `status="sent"`.
    """
    if not isinstance(result, dict) or result.get("ok") is not True:
        return False
    code = provider_status_code(result)
    if code is not None:
        return PROVIDER_ACCEPTED_MIN <= code <= PROVIDER_ACCEPTED_MAX
    return bool(provider_message_id(result))


def refusal_for_unaccepted(result: object, *, to: str) -> dict[str, Any]:
    """The named refusal a performed-but-unaccepted send is recorded as (the `refusals[]` shape).

    A provider that ANSWERS with a non-2xx code is not an integration failure to retry blindly:
    the message was refused, so the code and the provider's own error text ride the context and
    the run records `failed` — never `sent`, never silent.
    """
    from app.core.errors import E_INTEGRATION, ERROR_HINTS

    code = provider_status_code(result)
    body = result.get("result") if isinstance(result, dict) else None
    detail = body.get("error") if isinstance(body, dict) else None
    return {
        "ok": False,
        "error": {
            "code": E_INTEGRATION,
            "message": (
                f"the email provider did not accept the message for {to!r}: "
                + (
                    f"HTTP {code}"
                    if code is not None
                    else "no 2xx status and no provider message id was returned"
                )
            ),
            "how_to_fix": ERROR_HINTS.get(E_INTEGRATION, ""),
            "context": {
                "to": to,
                "provider_status": code,
                "provider_detail": detail,
                "source": "integrations.send_email result",
            },
        },
    }


__all__ = [
    "EMAIL_ATTEMPT_STATUSES",
    "EMAIL_ROW_DEFAULT_STATUS",
    "EMAIL_ROW_SENT",
    "EMAIL_ROW_STATUSES",
    "PROVIDER_ACCEPTED_MAX",
    "PROVIDER_ACCEPTED_MIN",
    "coerce_status_code",
    "provider_accepted",
    "provider_message_id",
    "provider_status_code",
    "refusal_for_unaccepted",
]
