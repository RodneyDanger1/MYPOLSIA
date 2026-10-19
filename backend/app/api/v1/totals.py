"""The declared total for the `limit`-only list reads (V20/L1, cards L2-07 and L2-11).

WHY: `/tasks`, `/invoices`, `/outreach/prospects`, `/support/inbox`, `/ads/campaigns`, `/memory`
and `/dashboard/activity` serve a bare JSON array with `limit` and no total, so a FULL page is
indistinguishable from a truncated one (V19/L2-07: "no FALSE total is declared, so this is
completeness, not dishonesty" — the fix is to declare the true one).

WHY A HEADER, NOT A BODY FIELD: those bodies are arrays, and every existing client parses them as
arrays (the tasks table, `OutreachWidget`/`ProspectRow`, the support page).  Wrapping them in
`{items, total}` is exactly the breaking change the runs wrapper avoided by being a NEW route.
`X-Polsia-Total` is additive — the same "declared count, verified before the first byte" idea
`X-Polsia-Row-Count` already carries on `/runs/export` (`runs.py:291-295`).

WHAT THE NUMBER MEANS: the FULL count of rows the request's filters match — the SAME `WHERE` the
page used, WITHOUT LIMIT/OFFSET.  It is therefore never the page length (the `pricing.count`
defect, L2-11), and `total == len(items)` only when the page really is the whole match set.

RESIDUAL (recorded, not hidden): a service-layer list read does not expose its conditions, so each
route restates its own filter predicates for the count.  `tests/unit/test_v20_api_typing.py` pins
every route's header against a seeded table under each filter combination — a future filter added
to one side and not the other fails that pin instead of serving a quiet lie.

HANDOFF (outside this lane's territory): `app/main.py`'s `expose_headers` list should gain
`X-Polsia-Total` so a CROSS-ORIGIN browser client can read it (`X-Polsia-Row-Count` is already
there for the same reason).  Nothing consumes it from the browser today, so the header is
server-complete and browser-invisible until that one line lands.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_DB, ERROR_HINTS, PolsiaError
from app.services.query_helpers import fetch_scalar

__all__ = ["TOTAL_HEADER", "declared_total"]

#: The response header the honest total rides.
TOTAL_HEADER = "X-Polsia-Total"


async def declared_total(
    db: AsyncSession, model: Any, conditions: Sequence[Any], *, what: str
) -> int:
    """`COUNT(*)` over the SAME filters as the page — or a named `E_DB`, never a fabricated 0.

    `fetch_scalar` already turns a driver failure into `E_DB`; the extra `None` branch covers the
    other half of the honesty rule: `SELECT count(*)` always yields one row, so no row means the
    driver broke its own contract.  The sibling precedent is `run_service.count_runs`, which
    refuses here rather than serving a `0` that reads as "no rows" (L2-09).
    """
    stmt = select(func.count()).select_from(model)
    if conditions:
        stmt = stmt.where(*conditions)
    total = await fetch_scalar(db, stmt, what=what)
    if total is None:
        raise PolsiaError(
            E_DB,
            f"{what} count query returned no row",
            how_to_fix=ERROR_HINTS[E_DB],
            context={"query": what},
        )
    return int(total)
