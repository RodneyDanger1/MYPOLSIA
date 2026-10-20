"""`vision_descriptions` — the vision memory row (V27 / L4).

ONE row per describe that actually reached the model: the attachment whose bytes were described,
the task it belongs to, the bytes' sha256 (the cache key), the model's sentence, the model id the
provider served, and the measured token total (`NULL` = unmeasured — never a fabricated 0).

WHY THE ROW IS NOT THE TASK'S RESULT: a description is MODEL OUTPUT for the operator to read.
It is remembered so the same image is not measured (and paid for) twice, but it never becomes
`tasks.result_summary` and it never rewrites the attachment row
(`test_describe_with_memory_still_never_overwrites_the_task_result`).

WHY `sha256` IS NOT UNIQUE: the same bytes may legitimately be described by another model, or
re-measured after a provider change, and the task history keeps every measurement.  The lookup
is `(sha256, model)` and takes the newest row; both indexes are non-unique
(`ix_vision_descriptions_sha256`, `ix_vision_descriptions_task_id`).

The two FKs are ON DELETE CASCADE, matching migration 0016: a deleted task takes its images'
descriptions with it, and deleting one attachment removes only that attachment's rows.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class VisionDescriptionRow(Base):
    """One stored description: `(attachment bytes, model)` -> the sentence that was measured."""

    __tablename__ = "vision_descriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attachment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("task_attachments.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    #: The attachment bytes' digest, copied from `task_attachments.sha256` at describe time —
    #: the row already carries the measured digest, so the describe path never re-hashes.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The model's sentence, exactly as served (never trimmed to a summary, never rewritten).
    description: Mapped[str] = mapped_column(Text, nullable=False)
    #: The model id the provider actually served; NULL only for a row written by hand.
    model: Mapped[str | None] = mapped_column(String(120))
    #: Measured prompt+completion tokens; NULL = the provider reported no usage.
    tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_vision_descriptions_sha256", "sha256"),
        Index("ix_vision_descriptions_task_id", "task_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<VisionDescriptionRow id={self.id} attachment={self.attachment_id} "
            f"task={self.task_id} {self.model}>"
        )


__all__ = ["VisionDescriptionRow"]
