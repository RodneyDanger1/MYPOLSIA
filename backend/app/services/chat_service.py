"""Chat backend service (wave V8, seat C1) — the DETERMINISTIC intent router.

Design source (verbatim contract): plan `2026-10-14_142249-polsia-remaining-waves-v7-v10.md`
§7 C1 + `wave-v8/WAVE-PLAN.md` "Frozen interface — CHAT".  This module implements exactly that
frozen shape; `app/api/v1/chat.py` is its wire form.

WHAT THIS MODULE IS

* A keyword/pattern classifier (`classify_message`) mapping ONE owner message to exactly one
  intent — `answer` / `create_task` / `refused`.  No model calls, no free-roaming tool use:
  the same message always classifies the same way, and the rules (in order) are:

      1. blank            -> bounded guidance answer
      2. `task <n>` / `#<n>`                 -> task lookup by id (read-only; safe first)
      3. money/external stems                -> REFUSED `E_POLICY_DENIED` (never queued)
      4. create cues ("create a task to ...", "run the finance agent", ...) -> create_task
      5. finance stem                        -> finance summary answer
      6. runs stem                           -> runs ledger head answer
      7. report stem                         -> latest daily report answer
      8. dashboard stem                      -> dashboard summary answer
      9. everything else                     -> bounded capabilities answer

  The money check runs BEFORE the create branches on purpose: "create a task to pay X" must be
  refused by name, never silently turned into a task.  The rules are deliberately dumb (a stem
  match, not an NLU pass): the safe direction is over-refusal with an actionable message, and
  the refusal text names how to ask about money instead ("finance summary").

* The ANSWER branches read EXISTING surfaces only, one each: the dashboard summary statement
  (`app/api/v1/dashboard.py::_summary_statement` — imported, not re-implemented, because a
  second copy of that SELECT is the drift the one-truth rule forbids), `run_service.list_runs`
  (ledger head), `finance_service.get_finance_summary`, `task_service.get_task` (lookup by id)
  and `report_service.get_latest_daily_report`.  Unmeasured values render their honest
  placeholder ("not measured" / "none yet" / "—"), NEVER 0 — only real `COUNT(*)` readings
  print a zero.

* The CREATE_TASK branch mints a SINGLE-USE confirm token with the approvals MINT pattern
  (V8-D4): `policy_firewall.issue_approval(secret, action_fingerprint="chat-confirm:<row_id>")`
  is called for the pending chat row, so `{exp}.{sig}` is the same HMAC-SHA256/`[:32]`/TTL wire
  form `POST /api/v1/approvals` mints, prefixed with the row id (`"{id}.{exp}.{sig}"`) — WHY the
  id rides in-band: unlike an approval token (whose fingerprint travels out-of-band on the
  parked task), `/chat/confirm` receives ONLY the token, so the row the token is bound to must
  be derivable from the token itself before the HMAC can be recomputed.  The id is not a
  secret; the signature is what makes the token unforgeable.  `verify_approval` verifies it —
  the firewall's own verifier, never a second crypto implementation.  With no `APPROVAL_SECRET`
  configured nothing is minted (mirrors approvals.py: a token that could never verify is
  refused by name `E_CONFIG`, never issued).

* `confirm_task` consumes the token EXACTLY ONCE inside one transaction: the pending row is
  re-read `FOR UPDATE` (a no-op on SQLite's single writer, a real row lock on PostgreSQL) and
  atomically marked consumed in the same commit that creates the task, so a reused, expired or
  foreign token is a named 409 (`E_TASK_NOT_RUNNABLE`) and NEVER a second task.  The task is
  created through the SAME path `POST /agents/{type}/trigger` uses: `create_task` (source
  `"chat"`) -> commit (so the worker can read the row it is dispatched with) ->
  `asyncio.to_thread(run_agent_task.delay, task_id)` -> on a dead broker the E_REDIS honesty
  branch with the load-bearing commit (`agents.py:102-128`): the row records its measured
  terminal `failed` state, the caller gets the named refusal, and the created id survives.

* PERSISTENCE: every exchange writes the owner row and the Polsia row to `chat_messages` and
  exactly ONE `chat_message` activity row per request through `log_activity` (publish included —
  the single-row, publishing variant; `log_activities` does not publish by design).  Nothing
  here touches `/ws/activity` or the activity feed ordering.

SECRET DISCIPLINE (load-bearing): `redact_secrets` replaces every `sk-` byte-sequence in a
message with `[redacted]` BEFORE that message can reach the reply, the intent JSON, the stored
`chat_messages` content, the task title/description, the refusal's `matched` phrase, or a log
line.  The rule is deliberately mechanical and substring-level — a word like `disk-space`
redacts to `di[redacted]` — because the guarantee must hold against text scrapers that cannot
know word intent; the invariant is "no response, row, task or log line ever contains `sk-`".
`SANDBOX_MODE` is respected: refusals and task confirmations carry the sandbox note when it is
on, so chat never implies a real external action under the sandbox.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.policy_firewall import (
    configured_approval_secret,
    issue_approval,
    verify_approval,
)
from app.api.v1.dashboard import _summary_statement
from app.config import settings
from app.core.errors import (
    E_CONFIG,
    E_DB,
    E_POLICY_DENIED,
    E_REDIS,
    E_TASK_NOT_RUNNABLE,
    E_VALIDATION,
    ERROR_HINTS,
    PolsiaError,
)
from app.models.chat import ChatMessage
from app.schemas.chat import (
    ChatHistoryItem,
    ChatHistoryResponse,
    ChatIntent,
    ChatRefusal,
    ChatReply,
    ChatResponse,
)
from app.services.activity_service import log_activity
from app.services.finance_service import get_finance_summary
from app.services.query_helpers import fetch_one, fetch_scalar, fetch_scalars
from app.services.report_service import get_latest_daily_report
from app.services.run_service import RunFilters, list_runs
from app.services.task_service import (
    VALID_AGENT_TYPES,
    create_task,
    get_task,
    update_task_status,
)

logger = logging.getLogger(__name__)

#: TTL of a chat confirm token, in seconds (15 min): long enough to read the card, short
#: enough that a leaked token dies quickly.  The approvals channel defaults to 1h for runs
#: that park themselves; a chat card is an immediate owner decision.
CONFIRM_TOKEN_TTL_S = 900

#: `issued_to` inside the HMAC message — the SAME literal `issue_approval` defaults to, so
#: mint and verify can never disagree about it.
_CONFIRM_ISSUER = "operator"

#: The action fingerprint prefix a confirm token is scoped to.  The whole token is bound to
#: ONE pending chat row; no other row can verify against it.
_CONFIRM_FP_PREFIX = "chat-confirm:"


# ── secret redaction ─────────────────────────────────────────────────────────


_SK_RE = re.compile(r"sk-[A-Za-z0-9_\-]*", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """Replace every `sk-` byte-sequence with `[redacted]`.  See the module docstring.

    Mechanical and substring-level on purpose: it fires inside words too (`disk-space` ->
    `di[redacted]`), because the load-bearing invariant is "no chat output/stored text/log
    line ever contains `sk-`" and that must hold for text scrapers that cannot know word
    intent.  The replacement itself contains no `sk-`.
    """
    return _SK_RE.sub("[redacted]", text)


# ── deterministic classifier ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Classification:
    """One deterministic verdict over one message.  `rule` is the stable rule name."""

    kind: str  # "answer" | "create_task" | "refused"
    rule: str
    task_id: int | None = None
    matched: str = ""


#: `task <n>`, `task #<n>`, `task number <n>`, `#<n>` — a digit-pinned row lookup.
_LOOKUP_RE = re.compile(r"\btask\s*(?:number|no\.?|#)?\s*(\d{1,9})\b|#(\d{1,9})\b", re.IGNORECASE)

#: Money / external-shaped stems (the brief's named set + its natural kin: transfers, refunds,
#: deposits, purchases, wires).  Matched BEFORE the create branches — never silently queued.
_MONEY_EXTERNAL_RE = re.compile(
    r"\bpay(?:s|ing|ment|ments)?\b"
    r"|\bpaid\b"
    r"|\bbuy(?:s|ing)?\b"
    r"|\bpurchas(?:e|es|ed|ing)\b"
    r"|\bwithdraw(?:s|al|ing)?\b"
    r"|\btransfer(?:s|red|ring)?\b"
    r"|\brefund(?:s|ed|ing)?\b"
    r"|\bdeposit(?:s|ed|ing)?\b"
    r"|\bwire\b"
    r"|\bsend\b[^.\n]{1,40}\bto\b",
    re.IGNORECASE,
)

#: Task-creation cues.  Deliberately literal ("create/make/file/queue/add/open/start ... task",
#: "have the social agent ...", "run the finance agent") plus two direct external-work shapes.
_CREATE_TASK_RE = re.compile(
    r"\b(?:create|make|file|queue|add|open|start)\b[^.\n]{0,40}\btask\b"
    r"|\bnew\s+task\b"
    r"|\b(?:have|ask|tell)\s+(?:the\s+)?(?:[\w-]+\s+){0,2}agent\b"
    r"|\brun\s+(?:the\s+)?(?:[\w-]+\s+){0,2}agent\b"
    r"|\b(?:tweet about|post to|post on|write a post)\b"
    r"|\b(?:email|draft|write)\b[^.\n]{0,40}\b(?:customers?|prospects?|clients?|subscribers?|followers?)\b",
    re.IGNORECASE,
)

#: Answer stems, in match order: finance -> runs -> report -> dashboard (the generic one last).
_FINANCE_RE = re.compile(
    r"\bfinance\b|\bmoney\b|\brevenue\b|\bmrr\b|\barr\b|\bcash\b|\bbalance\b|\bexpenses?\b"
    r"|\bprofit\b|\bburn\b|\bstripe\b|\bmargin\b",
    re.IGNORECASE,
)
_RUNS_RE = re.compile(r"\bruns?\b|\bledger\b|\busage\b|\bagentruns\b", re.IGNORECASE)
_REPORT_RE = re.compile(
    r"\breports?\b|\bmorning plan\b|\bevening summary\b|\bwhat happened\b|\bplan for (?:today|tomorrow)\b",
    re.IGNORECASE,
)
_DASHBOARD_RE = re.compile(
    r"\bstatus\b|\boverview\b|\bsummary\b|\bprogress\b"
    r"|\bhow (?:are|is) (?:things|it|we|the (?:business|company))\b"
    r"|\bhow'?s it going\b|\bwhat'?s happening\b|\bhow are we doing\b|\bdoing today\b",
    re.IGNORECASE,
)

#: Agent routing keywords.  FIXED table order decides ties ("email the research team" ->
#: the first table entry whose keyword matches), so routing is as deterministic as the rest.
_AGENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("competitor_research", ("research", "competitor", "market analysis", "niche")),
    ("business_planning", ("plan", "planning", "strategy", "roadmap", "business model")),
    ("social_media", ("social", "tweet", "twitter", "linkedin", "instagram", "tiktok",
                      "youtube", "post to")),
    ("ads_management", ("ads", "ad campaign", "advertising", "campaign", "meta ads",
                        "google ads")),
    ("email_outreach", ("email", "outreach", "cold outreach", "prospect")),
    ("code_generation", ("code", "bug", "repo", "github", "deploy", "website",
                         "landing page")),
    ("customer_support", ("support", "customer", "inbox", "ticket", "complaint")),
    ("finance", ("finance", "invoice", "revenue", "budget", "bookkeeping")),
)

#: The task source recorded on tasks chat files (the trigger route records "manual").
CHAT_TASK_SOURCE = "chat"

#: The agent_type chat-created tasks default to when no keyword matches.
DEFAULT_CHAT_AGENT = "orchestrator"

_BLANK_TEXT = (
    "I didn't catch a message. Ask for 'status', 'runs', 'finance', 'report' or 'task <id>', "
    "or describe a task (\"create a task to ...\") and I'll ask you to confirm."
)

_FALLBACK_TEXT = (
    "I answer only from the company's own records and I don't guess. What I can do: "
    "'status' for today's task counts and the last report; 'runs' for the latest agent runs; "
    "'finance' for the money summary; 'report' for the latest daily report; 'task <id>' for one "
    "task; or say 'create a task to ...' and I'll ask you to confirm before anything is filed."
)


def classify_message(message: str) -> Classification:
    """The deterministic v1 router (rules and their order: module docstring)."""
    text = message.strip()
    if not text:
        return Classification(kind="answer", rule="blank")

    lookup = _LOOKUP_RE.search(text)
    if lookup is not None:
        raw_id = lookup.group(1) or lookup.group(2)
        return Classification(kind="answer", rule="task_lookup", task_id=int(raw_id))

    money = _MONEY_EXTERNAL_RE.search(text)
    if money is not None:
        return Classification(kind="refused", rule="money_external", matched=money.group(0))

    if _CREATE_TASK_RE.search(text):
        return Classification(kind="create_task", rule="create_task")

    if _FINANCE_RE.search(text):
        return Classification(kind="answer", rule="finance")
    if _RUNS_RE.search(text):
        return Classification(kind="answer", rule="runs")
    if _REPORT_RE.search(text):
        return Classification(kind="answer", rule="report")
    if _DASHBOARD_RE.search(text):
        return Classification(kind="answer", rule="dashboard")
    return Classification(kind="answer", rule="fallback")


def route_agent_type(message: str) -> str:
    """The agent a create_task message is routed to (fixed keyword table; default orchestrator)."""
    lowered = message.lower()
    for agent_type, keywords in _AGENT_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return agent_type
    return DEFAULT_CHAT_AGENT


# ── confirm tokens (the approvals mint pattern, V8-D4) ───────────────────────


def issue_confirm_token(secret: bytes, *, chat_message_id: int, ttl_s: int = CONFIRM_TOKEN_TTL_S) -> str:
    """Mint the single-use confirm token for ONE pending chat row.

    Wire form `"{chat_message_id}.{exp}.{sig}"`: the `{exp}.{sig}` half is produced by
    `policy_firewall.issue_approval` — the approvals minter itself (TTL-bounded, HMAC-SHA256
    over `"chat-confirm:<id>|<exp>|operator"`, signature truncated to 32 hex chars) — and the
    row id is prefixed so `/chat/confirm` can find the row BEFORE recomputing the HMAC (see the
    module docstring for why the id must ride in-band).
    """
    minted = issue_approval(
        secret,
        action_fingerprint=f"{_CONFIRM_FP_PREFIX}{chat_message_id}",
        ttl_s=ttl_s,
    )
    return f"{chat_message_id}.{minted.token}"


@dataclass(frozen=True)
class ConfirmTokenCheck:
    """The parsed verdict over one presented token.  `reason` is stable for refusals."""

    ok: bool
    chat_message_id: int | None = None
    reason: str = ""  # "malformed" | "expired" | "invalid" | ""


def check_confirm_token(secret: bytes, token: str) -> ConfirmTokenCheck:
    """Parse + verify a presented token with the firewall's own verifier.

    Expiry is reported BEFORE signature verification so an expired token gets its own named
    message; a tampered id/expiry fails the HMAC ("invalid"), which is the fail-closed answer.
    """
    try:
        id_part, exp_part, sig_part = token.split(".", 2)
        chat_message_id = int(id_part)
        exp = int(exp_part)
    except (ValueError, AttributeError):
        return ConfirmTokenCheck(ok=False, reason="malformed")
    if int(time.time()) > exp:
        return ConfirmTokenCheck(ok=False, chat_message_id=chat_message_id, reason="expired")
    verified = verify_approval(
        secret,
        f"{exp_part}.{sig_part}",
        action_fingerprint=f"{_CONFIRM_FP_PREFIX}{chat_message_id}",
        issued_to=_CONFIRM_ISSUER,
    )
    if not verified:
        return ConfirmTokenCheck(ok=False, chat_message_id=chat_message_id, reason="invalid")
    return ConfirmTokenCheck(ok=True, chat_message_id=chat_message_id)


# ── answer rendering (real reads; honest placeholders) ───────────────────────


def _sandbox_note() -> str:
    """The SANDBOX_MODE note appended to refusal/confirm texts while the sandbox is on."""
    if getattr(settings, "sandbox_mode", False):
        return (" Note: SANDBOX_MODE=true — even an approved action is recorded as a labelled "
                "draft, nothing is really sent.")
    return ""


def _money_cents(cents: Any) -> str:
    """Cents -> `$x.yy`; an unmeasured value stays an honest placeholder, never `$0.00`.

    F17 (V12/W-C): Decimal division — `int(cents) / 100` crossed binary floats and lost the
    cents past 2^53 ($1,000,000,000,000,000.01 printed as ...00).
    """
    if cents is None:
        return "not measured"
    return f"${Decimal(int(cents)) / 100:,.2f}"


def _render_dashboard(row: Any) -> str:
    """The dashboard summary answer (counts are real COUNT reads -> a measured 0 may print)."""
    last = row.last_report_date
    last_text = last.isoformat() if hasattr(last, "isoformat") else (str(last) if last else "none yet")
    return (
        f"Today: {int(row.total or 0)} task(s) created — {int(row.completed or 0)} completed, "
        f"{int(row.pending or 0)} pending, {int(row.failed or 0)} failed. "
        f"Last daily report: {last_text}."
    )


def _render_finance(summary: dict[str, Any]) -> str:
    ad_spend = summary.get("total_ad_spend_usd")
    ad_text = "not measured" if ad_spend is None else f"${Decimal(str(ad_spend)):,.2f}"
    snapshot = summary.get("last_snapshot_date")
    parts = [
        f"MRR {_money_cents(summary.get('mrr_cents'))}",
        f"cash balance {_money_cents(summary.get('stripe_balance_cents'))}",
        f"revenue this month {_money_cents(summary.get('total_revenue_month_cents'))}",
        f"expenses this month {_money_cents(summary.get('total_expenses_month_cents'))}",
        f"ad spend {ad_text}",
        f"last snapshot {snapshot if snapshot else 'none yet'}",
    ]
    return "Finance: " + "; ".join(parts) + "."


def _render_runs(rows: Any, total: int) -> str:
    if total == 0:
        return ("The runs ledger is empty — no agent runs have been recorded yet "
                "(nothing to measure, not a zero reading).")
    head = "; ".join(
        "#{id} {agent} {status} started {started}".format(
            id=row.id,
            agent=row.agent_type,
            status=row.status or "not measured",
            started=row.started_at.isoformat(sep=" ", timespec="seconds") if row.started_at else "not measured",
        )
        for row in rows[:3]
    )
    return f"Runs ledger: {total} run(s) total. Most recent: {head}."


def _render_report(report: Any) -> str:
    if report is None:
        return "No daily report has been written yet."
    planned = report.tasks_planned if report.tasks_planned is not None else "—"
    completed = report.tasks_completed if report.tasks_completed is not None else "—"
    failed = report.tasks_failed if report.tasks_failed is not None else "—"
    plan = " ".join((report.morning_plan or "").split())
    snippet = f" Plan: {plan[:160]}" if plan else ""
    return (
        f"Latest daily report {report.report_date.isoformat()}: {planned} planned, "
        f"{completed} completed, {failed} failed.{snippet}"
    )


def _render_task(task: Any, task_id: int) -> str:
    if task is None:
        return f"I have no task #{task_id} — the ledger holds no row with that id."
    status = str(task.status) if task.status else "—"
    created = task.created_at.isoformat(sep=" ", timespec="seconds") if task.created_at else "not measured"
    description = " ".join((task.description or "").split())
    snippet = f" Description: {description[:160]}" if description else ""
    return (
        f"Task #{task.id} '{task.title}' is {status} for the {task.agent_type} agent "
        f"(created {created}).{snippet}"
    )


async def _build_answer(db: AsyncSession, classification: Classification) -> tuple[str, str | None]:
    """Compose the answer text for one classified message from the EXISTING reads only."""
    rule = classification.rule
    if rule == "blank":
        return _BLANK_TEXT, None
    if rule == "fallback":
        return _FALLBACK_TEXT, None
    if rule == "task_lookup":
        task = await get_task(db, classification.task_id or 0)
        return _render_task(task, classification.task_id or 0), "task lookup"
    if rule == "finance":
        summary = await get_finance_summary(db)
        return _render_finance(summary), "finance summary"
    if rule == "runs":
        rows, total = await list_runs(db, filters=RunFilters(), limit=3, offset=0)
        return _render_runs(rows, total), "runs ledger"
    if rule == "report":
        report = await get_latest_daily_report(db)
        return _render_report(report), "latest daily report"
    if rule == "dashboard":
        row = await fetch_one(db, _summary_statement(db), what="dashboard summary")
        if row is None:  # the aggregate always yields exactly one row; None is a real fault
            # V20/L1 (card L2-09): a bare `RuntimeError` here was an UNCODED 500 in the chat
            # reply path.  The aggregate breaking its one-row contract is a database fault, so it
            # is refused BY NAME — the `run_service.count_runs` sibling pattern — and the chat
            # route's `refusal_guard` turns it into the same envelope every other route serves.
            raise PolsiaError(
                E_DB,
                "dashboard summary statement returned no row",
                how_to_fix=ERROR_HINTS[E_DB],
                context={"query": "dashboard summary", "surface": "chat dashboard answer"},
            )
        return _render_dashboard(row), "dashboard summary"
    raise RuntimeError(f"chat classifier produced unknown answer rule {rule!r}")  # unreachable


# ── persistence helpers ──────────────────────────────────────────────────────


async def _write_exchange(
    db: AsyncSession,
    *,
    owner_content: str,
    owner_intent: dict[str, Any] | None,
    reply_text: str,
    reply_intent: dict[str, Any],
    activity_summary: str,
    activity_level: str = "info",
    activity_detail: dict[str, Any] | None = None,
) -> ChatMessage:
    """Persist one exchange: the owner row, the Polsia row, ONE published activity row.

    Returns the Polsia row.  The owner row is flushed first only when a caller needs its id
    (create_task does, for the token); everything else shares the request transaction.
    """
    db.add(ChatMessage(role="owner", content=owner_content, intent=owner_intent, task_id=None))
    polsia_row = ChatMessage(role="polsia", content=reply_text, intent=reply_intent, task_id=None)
    db.add(polsia_row)
    await db.flush()
    await log_activity(
        db,
        agent_type="polsia",
        action="chat_message",
        summary=activity_summary,
        level=activity_level,
        detail=activity_detail,
    )
    return polsia_row


# ── the handlers ─────────────────────────────────────────────────────────────


async def handle_message(db: AsyncSession, message: str) -> ChatResponse:
    """Classify + handle ONE owner message; persist the exchange; return the frozen 201 body."""
    clean = redact_secrets(message)
    classification = classify_message(clean)
    logger.info(
        "chat message handled: rule=%s kind=%s chars=%d",
        classification.rule, classification.kind, len(clean),
    )

    if classification.kind == "refused":
        matched = redact_secrets(classification.matched).strip()[:60]
        refusal = ChatRefusal(
            code=E_POLICY_DENIED,
            message=(
                f"chat refused this: '{matched}' is a money or external action, and chat never "
                "performs or queues those — nothing was created or filed."
            ),
            how_to_fix=(
                "money and external actions go through the approvals path: POST "
                "/api/v1/approvals mints a single-use token for the action fingerprint, and the "
                "responsible agent run performs the action under the policy firewall. If you "
                "meant to ASK about money, say 'finance summary'." + _sandbox_note()
            ),
        )
        await _write_exchange(
            db,
            owner_content=clean,
            owner_intent={"kind": "refused", "code": E_POLICY_DENIED},
            reply_text=refusal.message,
            reply_intent={"kind": "refused", "code": refusal.code, "matched": matched},
            activity_summary=f"Chat: refused a money/external request ({E_POLICY_DENIED})",
            activity_level="warning",
            activity_detail={"kind": "refused", "refusal_code": E_POLICY_DENIED},
        )
        return ChatResponse(
            reply=ChatReply(text=refusal.message),
            intent=ChatIntent(kind="refused", matched=matched),
            task_id=None,
            requires_confirm=False,
            confirm_token=None,
            refusal=refusal,
        )

    if classification.kind == "create_task":
        secret = configured_approval_secret()
        if not secret:
            refusal = ChatRefusal(
                code=E_CONFIG,
                message=(
                    "I can't offer a confirmation card: APPROVAL_SECRET is unset, so the "
                    "single-use confirm token could never verify."
                ),
                how_to_fix=(
                    "set APPROVAL_SECRET in .env (see .env.example) and restart the service, "
                    "then ask again — the same rule the approvals channel enforces."
                ),
            )
            await _write_exchange(
                db,
                owner_content=clean,
                owner_intent={"kind": "refused", "code": E_CONFIG},
                reply_text=refusal.message,
                reply_intent={"kind": "refused", "code": refusal.code},
                activity_summary=f"Chat: could not mint a confirm token ({E_CONFIG})",
                activity_level="warning",
                activity_detail={"kind": "refused", "refusal_code": E_CONFIG},
            )
            return ChatResponse(
                reply=ChatReply(text=refusal.message),
                intent=ChatIntent(kind="refused"),
                task_id=None,
                requires_confirm=False,
                confirm_token=None,
                refusal=refusal,
            )

        agent_type = route_agent_type(clean)
        title = clean[:500]
        plan = {"title": title, "agent_type": agent_type, "description": clean}
        owner_row = ChatMessage(
            role="owner",
            content=clean,
            intent={"kind": "create_task", "state": "pending", "plan": plan},
            task_id=None,
        )
        db.add(owner_row)
        await db.flush()  # the token is bound to this row's id
        confirm_token = issue_confirm_token(secret, chat_message_id=owner_row.id)
        reply_text = (
            f"Ready to file this for the {agent_type} agent:\n\"{title}\"\n"
            "Confirm and I'll queue it — nothing is created until you confirm "
            f"(single-use token, expires in {CONFIRM_TOKEN_TTL_S // 60} min)." + _sandbox_note()
        )
        polsia_row = ChatMessage(
            role="polsia",
            content=reply_text,
            intent={"kind": "create_task", "agent_type": agent_type},
            task_id=None,
        )
        db.add(polsia_row)
        await db.flush()
        await log_activity(
            db,
            agent_type="polsia",
            action="chat_message",
            summary=f"Chat: proposed a task for the {agent_type} agent (awaiting confirm)",
            level="info",
            detail={"kind": "create_task", "agent_type": agent_type},
        )
        return ChatResponse(
            reply=ChatReply(text=reply_text),
            intent=ChatIntent(kind="create_task", agent_type=agent_type, title=title),
            task_id=None,
            requires_confirm=True,
            confirm_token=confirm_token,
            refusal=None,
        )

    # ── answer branch ──
    answer_text, source = await _build_answer(db, classification)
    answer_text = redact_secrets(answer_text)  # blanket: no `sk-` can survive into any reply
    await _write_exchange(
        db,
        owner_content=clean,
        owner_intent=None,
        reply_text=answer_text,
        reply_intent={"kind": "answer", **({"source": source} if source else {})},
        activity_summary=f"Chat: answered from {source}" if source else "Chat: bounded answer",
        activity_detail={"kind": "answer", "source": source} if source else {"kind": "answer"},
    )
    return ChatResponse(
        reply=ChatReply(text=answer_text),
        intent=ChatIntent(kind="answer", source=source),
        task_id=None,
        requires_confirm=False,
        confirm_token=None,
        refusal=None,
    )


def _refuse_confirm(reason: str, detail: str, *, context: dict[str, Any] | None = None) -> PolsiaError:
    """The named 409 for any token that cannot confirm (expired / used / invalid / foreign).

    `E_TASK_NOT_RUNNABLE` is the repo's 409 (`core/errors.HTTP_STATUS_BY_CODE`); the message
    and `how_to_fix` carry the precise cause — never a second task, never a silent success.
    """
    return PolsiaError(
        E_TASK_NOT_RUNNABLE,
        detail,
        how_to_fix=(
            "send a new chat message describing the task — Polsia replies with a fresh "
            f"confirmation card; tokens are single-use and expire after {CONFIRM_TOKEN_TTL_S // 60} min"
        ),
        context={"reason": reason, **(context or {})},
    )


async def confirm_task(db: AsyncSession, confirm_token: str) -> int:
    """Consume a confirm token EXACTLY ONCE and create the task via the agents-trigger path.

    Returns the created task id.  Every non-consumable token is a named 409 — a used token can
    never create a second task, because the consumption marker and the task row are written in
    the same transaction whose commit precedes the broker hand-off.
    """
    secret = configured_approval_secret()
    if not secret:
        raise PolsiaError(
            E_CONFIG,
            "the chat confirm channel is not configured: APPROVAL_SECRET is unset, so no token "
            "can verify",
            how_to_fix="set APPROVAL_SECRET in .env (see .env.example) and restart the service, "
                       "then ask for the task again",
        )
    check = check_confirm_token(secret, confirm_token)
    if not check.ok:
        if check.reason == "malformed":
            raise PolsiaError(
                E_VALIDATION,
                "the confirm_token is not a Polsia chat confirmation token",
                how_to_fix="send the confirm_token value exactly as it came back from "
                           "POST /api/v1/chat",
                context={"reason": "malformed"},
            )
        if check.reason == "expired":
            raise _refuse_confirm("expired", "the confirm token expired — nothing was created")
        raise _refuse_confirm(
            "invalid",
            "the confirm token failed verification (it was not minted by this server for that "
            "chat row, or APPROVAL_SECRET changed) — nothing was created",
        )

    row = await fetch_scalar(
        db,
        select(ChatMessage).where(ChatMessage.id == check.chat_message_id).with_for_update(),
        what=f"chat message {check.chat_message_id}",
    )
    if row is None:
        raise _refuse_confirm(
            "missing_row",
            "the chat row this confirm token was bound to no longer exists — nothing was created",
            context={"chat_message_id": check.chat_message_id},
        )
    intent = row.intent if isinstance(row.intent, dict) else None
    state = intent.get("state") if intent else None
    if state == "consumed":
        raise _refuse_confirm(
            "already_used",
            f"this confirm token was already used — task #{row.task_id} was created from it "
            "and one token confirms exactly one task; nothing else was created",
            context={"chat_message_id": row.id, "task_id": row.task_id},
        )
    if not intent or intent.get("kind") != "create_task" or state != "pending":
        raise _refuse_confirm(
            "no_pending_plan",
            "this chat row carries no pending task plan (it was not a create_task confirmation "
            "card) — nothing was created",
            context={"chat_message_id": row.id},
        )
    raw_plan = intent.get("plan")
    plan: dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
    agent_type = plan.get("agent_type") or DEFAULT_CHAT_AGENT
    if agent_type not in VALID_AGENT_TYPES:
        raise PolsiaError(
            E_VALIDATION,
            f"the pending plan names an unknown agent_type {agent_type!r}",
            how_to_fix="ask again with a valid agent (see crew_factory.AGENT_MAP)",
            context={"agent_type": agent_type},
        )
    title = redact_secrets(plan.get("title") or "Task from chat")[:500]
    description = redact_secrets(plan["description"]) if plan.get("description") else None

    task = await create_task(
        db,
        title=title,
        agent_type=agent_type,
        description=description,
        source=CHAT_TASK_SOURCE,
    )
    # Consumption marker and the task row share ONE commit (below), BEFORE any broker hand-off.
    row.intent = {"kind": "create_task", "state": "consumed", "plan": plan}
    row.task_id = task.id
    db.add(ChatMessage(
        role="polsia",
        content=f"Filed task #{task.id} for the {agent_type} agent — it is queued.",
        intent={"kind": "create_task", "state": "queued", "agent_type": agent_type},
        task_id=task.id,
    ))
    await log_activity(
        db,
        agent_type="polsia",
        action="chat_message",
        summary=f"Chat: confirmed — task #{task.id} for the {agent_type} agent queued",
        level="info",
        detail={"kind": "create_task", "task_id": task.id, "agent_type": agent_type},
    )
    await db.flush()
    # Commit BEFORE the hand-off: the worker claims in its own session and must be able to
    # READ the token's consumption + the task row it was dispatched with (agents.py rule).
    await db.commit()

    try:
        from celery_app.tasks.agent_tasks import run_agent_task

        # `.delay()` is BLOCKING (redis-py retry ladder); `to_thread` keeps the loop free —
        # the same measured call shape as POST /agents/{type}/trigger.
        await asyncio.to_thread(run_agent_task.delay, task.id)
    except Exception as exc:  # broker unavailable: NO delivery was accepted for this row
        logger.warning("chat: failed to enqueue confirmed task %s", task.id, exc_info=True)
        await update_task_status(
            db,
            task.id,
            "failed",
            error_message=f"{E_REDIS}: enqueue failed ({exc.__class__.__name__})",
        )
        # Load-bearing commit (T19): the row the 503 names must survive the refusal — the
        # caller saw the id, the ledger keeps it, and the token stays consumed (no retry
        # path can create a second task).
        await db.commit()
        raise PolsiaError(
            E_REDIS,
            f"task #{task.id} was created but could not be handed to a worker",
            context={"task_id": task.id, "agent_type": agent_type},
        ) from exc
    return task.id


async def list_history(db: AsyncSession, limit: int = 50) -> ChatHistoryResponse:
    """The most recent `limit` rows in reading order (oldest -> newest) + the FULL total."""
    rows = await fetch_scalars(
        db,
        select(ChatMessage)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit),
        what="chat messages",
    )
    total = await fetch_scalar(
        db, select(func.count()).select_from(ChatMessage), what="chat message count"
    )
    items = [
        ChatHistoryItem(role=row.role, content=row.content, created_at=row.created_at, task_id=row.task_id)
        for row in reversed(rows)
    ]
    return ChatHistoryResponse(items=items, total=int(total or 0))
