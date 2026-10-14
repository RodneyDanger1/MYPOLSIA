"""chat_messages — the owner <-> Polsia conversation (wave V8, migration 0008).

Frozen interface (WAVE-PLAN.md, "Frozen interface — CHAT"): the table `chat_messages`, the class
`ChatMessage` importable from `app.models.chat`, the columns named exactly below.  The C1 lane
(chat service + routes) CONSUMES these names; this module is owned by the C2 schema lane and no
other lane edits it.

Semantics carried by the columns:

* `role` is the speaker: `owner` (the human) or `polsia` (the product's reply).  Escalated to
  `Enum(ChatRole, native_enum=False, create_constraint=True, name="ck_chat_messages_role")` so the
  value set exists in ONE place and renders `VARCHAR(50) ... CONSTRAINT ck_chat_messages_role
  CHECK (...)` on every backend (the `tasks.status` doctrine, D2 / T03);
* `content` is the message text as exchanged — stored verbatim;
* `intent` is the machine-readable parse of the exchange (the pending create-task proposal + its
  single-use confirm token ride the OWNER row's intent JSON until `/chat/confirm` consumes it —
  C1's shape).  NULL when nothing was parsed — never a fabricated placeholder;
* `task_id` links a row to the task the exchange created (`FK tasks.id`, nullable — most exchanges
  create nothing);
* `created_at` carries the exchange time (server default now, NOT NULL).
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import json_type


class ChatRole(enum.StrEnum):
    """The `chat_messages.role` value set — the two speakers of the conversation."""

    OWNER = "owner"
    POLSIA = "polsia"


def chat_role_values(enum_cls: type[enum.Enum] = ChatRole) -> list[str]:
    """`Enum.values_callable`: persist the member VALUES, in definition order (task.py's rule)."""
    return [str(member.value) for member in enum_cls]


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[ChatRole] = mapped_column(
        Enum(
            ChatRole,
            native_enum=False,          # VARCHAR + CHECK on every backend (no PG ENUM type)
            create_constraint=True,     # the named CHECK is the at-rest enforcement
            name="ck_chat_messages_role",
            length=50,                  # the VARCHAR width the schema lane froze
            values_callable=chat_role_values,  # persist values, not member names
        ),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ChatMessage id={self.id} role={self.role}>"
