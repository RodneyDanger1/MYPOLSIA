"""competitors — a table with NO writer and NO reader in this product (R-W5-CA-1).

WHY THIS HEADER EXISTS (measured): the generated header said "unreferenced", which was wrong in one
direction and right in another, and both halves matter:

  * REFERENCED by the data-portability surface: `Competitor` is a WIPE member
    (`app/api/v1/data.py::MEMBER_MODELS`) and a data-wipe approval token digests that member set, so
    the model is imported and every wipe deletes this table;
  * NOT a member of the export (`EXPORT_MODELS`, 8 members) and NOT read anywhere else: no route, no
    page, no service renders a row.  The wave-4 claim that a generic `/data/{table}` dump reads it
    named a route that does not exist (the live OpenAPI carries only `/api/v1/data` and
    `/api/v1/data/export`; forge/raw/w5/live-data-routes.txt).

A competitor_research run's findings live on `agent_runs.output` (its schema, `CompetitorReport`),
which IS read.  Landing a producer here without a consumer would create a write-only table, so wave
5 refuses that and carries the exact producer+consumer seam as `R-W5-CA-1` in
`forge/REQUESTS-W5.md`.  Keep this docstring true: if a reader is ever added, this is the file that
must stop saying it has none.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import json_type, string_list_type


class Competitor(Base):
    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    website: Mapped[str | None] = mapped_column(String(512))
    pricing_info: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    positioning: Mapped[str | None] = mapped_column(Text)
    strengths: Mapped[list[str] | None] = mapped_column(string_list_type())
    weaknesses: Mapped[list[str] | None] = mapped_column(string_list_type())
    last_researched: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
