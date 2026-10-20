"""Wire schemas for `/api/v1/prospects` (V27/L5 — the owner-managed prospect CRM).

The row shape is NOT redefined here: `ProspectResponse` from `app/schemas/social.py` is the one
description of a `prospects` row (it is what `/outreach/prospects` already serves), and it is
re-exported so this module is the complete wire vocabulary of the new surface.  Two row models
over one table would drift the moment a column is added.

Request models carry the field NAMES and their storage limits only:

* `email` has no `min_length`/`pattern` on purpose.  A blank or structurally unusable address must
  answer the SAME named refusal as a duplicate (`E_VALIDATION` with `context.field = "email"`,
  raised by `prospect_service`), not pydantic's uncoded 422 array — the owner's form shows one
  message shape for every email problem.  An ABSENT `email` is a blank address for the same
  reason (`default=""`), so omitting the field cannot take a different refusal path.
* `status` is a plain `str`: the column is an open string in the brief's terms ("free text from
  the existing rows"), so no client-side enum is invented here.  The database's own CHECK
  (`ck_prospects_status`, migration 0002) stays the last word, and the service maps a violation
  of it to the named refusal instead of a 500.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.social import ProspectResponse

__all__ = [
    "ProspectCreate",
    "ProspectListResponse",
    "ProspectResponse",
    "ProspectUpdate",
]


class ProspectCreate(BaseModel):
    """POST body — every field but `email` is optional (the column is nullable or defaulted)."""

    email: str = Field(default="", description="the prospect's address; normalised server-side")
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    company: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=255)
    source: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=50)


class ProspectUpdate(BaseModel):
    """PATCH body — only the PROVIDED keys are applied (`model_dump(exclude_unset=True)`).

    An explicit `null` CLEARS a nullable column (`{"title": null}`); an omitted key changes
    nothing.  `email`, when provided, is normalised + uniqueness-checked exactly like a create.
    """

    email: str | None = None
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    company: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=255)
    source: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=50)


class ProspectListResponse(BaseModel):
    """`GET /prospects` — the page of rows plus the FULL match count (never the page length)."""

    items: list[ProspectResponse]
    total: int
