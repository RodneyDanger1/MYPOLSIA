"""Chat wire contract (wave V8, seat C1) — the FROZEN shape the UI codes against.

Source of truth (verbatim): plan `2026-10-14_142249-polsia-remaining-waves-v7-v10.md` §7 C1 +
`wave-v8/WAVE-PLAN.md` "Frozen interface — CHAT".  Every field name below is quoted from that
contract; nothing here is invented.

    POST /api/v1/chat           body {"message": str}        -> ChatResponse (201)
    POST /api/v1/chat/confirm   body {"confirm_token": str}  -> ConfirmResponse (200)
    GET  /api/v1/chat?limit=50                               -> ChatHistoryResponse (200)
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


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
    """

    kind: Literal["answer", "create_task", "refused"]
    source: str | None = None
    agent_type: str | None = None
    title: str | None = None
    matched: str | None = None


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
    created_at: datetime | None
    task_id: int | None


class ChatHistoryResponse(BaseModel):
    """`{"items": [...], "total": int}` — `total` is the FULL stored-row count, not the page size.

    `items` is the most recent `limit` rows in READING order (oldest -> newest), matching how a
    chat transcript renders.
    """

    items: list[ChatHistoryItem]
    total: int
