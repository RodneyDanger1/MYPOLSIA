"""Chat wire contract (wave V8, seat C1) — the FROZEN shape the UI codes against.

Source of truth (verbatim): plan `2026-10-14_142249-polsia-remaining-waves-v7-v10.md` §7 C1 +
`wave-v8/WAVE-PLAN.md` "Frozen interface — CHAT".  Every field name below is quoted from that
contract; nothing here is invented.

    POST /api/v1/chat           body {"message": str}        -> ChatResponse (201)
    POST /api/v1/chat/confirm   body {"confirm_token": str}  -> ConfirmResponse (200)
    GET  /api/v1/chat?limit=50                               -> ChatHistoryResponse (200)
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.schemas.common import UtcDatetime


class ChatRequest(BaseModel):
    """`body = JSON object with one key, message (string)` — the frozen request shape."""

    message: str


class ChatReply(BaseModel):
    """`reply = object with key text`."""

    text: str


class ChatIntent(BaseModel):
    """`intent = object with key kind (one of answer, create_task, refused — plus per-branch extras)`.

    The per-branch extras are these optional fields and nothing else:

    * `source` — an `answer`: the name of the existing read the text was composed from
      (`dashboard summary` / `runs ledger` / `finance summary` / `latest daily report` /
      `task lookup`); `None` for the bounded capabilities fallback.
    * `agent_type` / `title` — a `create_task` proposal: which agent the task is for and the
      redacted title that will be filed (both `sk-`-safe: key material never reaches here).
    * `matched` — a `refused`: the literal phrase the deterministic rule keyed on (redacted,
      truncated), so the owner can see WHAT tripped the policy.
    * `paid_run` / `run_cost_cap_usd` / `day_cost_cap_usd` — F-29 (ADDITIVE, `create_task` only):
      confirming a chat proposal creates the task AND hands it to the worker, which runs the agent
      against the model provider — a PAID run.  The UI must say so BEFORE the click, and these
      three fields are where the caps come from (`accounting.budget_policy_from_settings`: the
      enforced per-run ceiling and the daily ceiling).  They are `None` on every other branch:
      an answer/refusal dispatches nothing, so no cost claim belongs on it.
    """

    kind: Literal["answer", "create_task", "refused"]
    source: str | None = None
    agent_type: str | None = None
    title: str | None = None
    matched: str | None = None
    paid_run: bool | None = None
    run_cost_cap_usd: float | None = None
    day_cost_cap_usd: float | None = None


class ChatRefusal(BaseModel):
    """`refusal = object with keys code (an E_* string), message, how_to_fix, or null`."""

    code: str
    message: str
    how_to_fix: str


class ChatResponse(BaseModel):
    """The frozen 201 body of `POST /api/v1/chat`."""

    reply: ChatReply
    intent: ChatIntent
    task_id: int | None = None
    requires_confirm: bool = False
    confirm_token: str | None = None
    refusal: ChatRefusal | None = None


class ConfirmRequest(BaseModel):
    """`body = JSON object with key confirm_token (string)`."""

    confirm_token: str


class ConfirmResponse(BaseModel):
    """`200 JSON object with key task_id (int)`."""

    task_id: int


class ChatHistoryItem(BaseModel):
    """One persisted `chat_messages` row: `{role, content, created_at, task_id}` exactly."""

    role: str
    content: str
    created_at: UtcDatetime | None
    task_id: int | None


class ChatPendingConfirm(BaseModel):
    """F-40 (wave 6, ADDITIVE): the stored proposal's still-open confirm gate.

    THE DEFECT IT CLOSES (`visuals/VISUALS-FINDINGS-W5.md` §4): the stored reply says *"Confirm and
    I'll queue it … single-use token, expires in 15 min"* and the activity strip says *awaiting
    confirm*, but the token lived only in the 201 body — so any reload dropped the card while the
    rail kept instructing the owner to click it.  The owner lost the request and the guidance lied.

    WHAT THIS IS: `token` re-issues the SAME single-use binding for the stored OWNER row — the
    approvals minter's HMAC scope `chat-confirm:<chat_message_id>`, expiring at the row's original
    `created_at + 15 min` — so the card can be restored.  (The mint is nonce-bearing, so its bytes
    differ from the original 201's token; the row, scope and expiry are identical, and confirming
    either token consumes the row.)  It is present ONLY while that window is open:
    `pending_confirm` is `null` once the token expired or was consumed, so a client that renders a
    card only when this block exists cannot offer a dead gate.  `intent` carries the same
    `create_task` facts the 201 served (agent, title, paid-run caps), so the restored card's
    disclosure is not re-guessed.
    """

    chat_message_id: int
    token: str
    expires_at: UtcDatetime
    intent: ChatIntent


class ChatHistoryResponse(BaseModel):
    """`{"items": [...], "total": int}` — `total` is the FULL stored-row count, not the page size.

    `items` is the most recent `limit` rows in READING order (oldest -> newest), matching how a
    chat transcript renders.  F-40 appends the ADDITIVE `pending_confirm` key (the restored gate,
    or `null`); the per-row shape is unchanged.
    """

    items: list[ChatHistoryItem]
    total: int
    pending_confirm: ChatPendingConfirm | None = None
