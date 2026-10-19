"""Wire models for `GET /api/v1/pricing` (V20/L1 — closes V19/L2-06 + L2-11 for that route).

WHY: the route declared `-> dict[str, Any]`, so the catalog row shape (the one the Settings →
Pricing panel reads, `frontend/src/components/settings/PricingSection.tsx`) was published only as
`additionalProperties: true`.  The model below names every key the dataset actually carries and
keeps the served bytes identical:

  * `price_low`/`price_high` are floats in the frozen dataset (49/50 rows) and are declared
    `float | None` — the repo's documented money-as-float-on-the-wire convention (V19/L2 §4).
  * `hours.low`/`.high` are INTEGERS today; `int | float | None` keeps `25` on the wire as `25`
    (a `float` field would silently re-spell it `25.0` — a wire change for a number nobody
    changed), while still admitting a fractional hour figure if the research file gains one.
  * `date` is a STRING, not `date`: `catalog_provenance` reports an unparseable stored date
    verbatim with a `reason` (F24); a `date` field would turn that documented state into a 500.
  * `price_text` exists on exactly ONE row (the free-form price claims); it is optional and the
    route serializes with `exclude_unset`, so the 49 rows that do not carry it do NOT grow a
    `"price_text": null` — that would be both a wire change and a fake "unmeasured" value.

`count` vs `total` (L2-11): `count` is the number of rows SERVED (page length — what the panel
prints, "N rows served"), `total` is the FULL match count for `q`, so a caller can never read a
truncated page as the whole catalog.  Both are declared; the panel keeps working unchanged.
"""
from __future__ import annotations

from pydantic import BaseModel

__all__ = ["PricingCatalogResponse", "PricingHours", "PricingRow"]


class PricingHours(BaseModel):
    """A workload estimate as stated by the cited source — whole hours today."""

    low: int | float | None = None
    high: int | float | None = None


class PricingRow(BaseModel):
    """One dated market-rate CLAIM with its source (never a quote, never live pricing)."""

    service: str
    price_low: float | None = None
    price_high: float | None = None
    model: str | None = None
    hours: PricingHours | None = None
    source_url: str | None = None
    fetch_type: str | None = None
    #: ISO-8601 `YYYY-MM-DD` as stored (kept a string — see the module docstring).
    date: str | None = None
    source_label: str | None = None
    taxable_hint: str | None = None
    #: Free-form price claim on the row(s) whose source states no numeric band.
    price_text: str | None = None


class PricingCatalogResponse(BaseModel):
    """`{query, count, total, items, catalog_as_of, staleness_days, stale[, reason]}`."""

    query: str | None
    #: Rows SERVED on this page (page length — the field the panel already reads).
    count: int
    #: FULL match count for `query` (the catalog holds exactly 50 rows).
    total: int
    items: list[PricingRow]
    # --- dated-snapshot provenance, computed per call ---------------------------------------
    catalog_as_of: str | None
    staleness_days: int | None
    stale: bool | None
    #: Present only when `catalog_as_of` is unparseable (F24).
    reason: str | None = None
