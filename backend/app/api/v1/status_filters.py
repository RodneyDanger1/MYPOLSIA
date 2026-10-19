"""The `?status=` value sets + the ONE named refusal for an unknown value (V20/L1, card L2-08).

WHY this module exists: `?status=<typo>` answered `200 []` on `/runs`, `/outreach/prospects`,
`/ads/campaigns` and `/social/posts` while `/tasks`, `/invoices` and `/support/inbox` answered the
named `E_VALIDATION` 422 (V19/L2-08) — the same word with two contracts, and an empty list that is
indistinguishable from "no rows".  From V20 on every status-filtered read validates the value
against its column's OWN vocabulary and refuses an unknown one by name.

WHERE THE VALUE SETS COME FROM (not taste): each tuple is the `CHECK` vocabulary the schema
already enforces at rest — `alembic/versions/0002_queue_and_perf.py:94-107` ships
`ck_agent_runs_status`, `ck_prospects_status`, `ck_social_posts_status` and
`ck_ad_campaigns_status`.  `tasks`/`invoices`/`support` keep their service-owned validators
(`task_service.VALID_TASK_STATUSES`, `invoice_service._coerce_status`,
`support_inbox_service.VALID_STATUSES`) — those routes needed no change, and their message shape is
what `require_status` reproduces.

TERRITORY NOTE: the natural home for a shared request-parameter rule is `app/api/params.py` (the
strict date/instant validators); this lane's write territory is `app/api/v1/*` + `app/schemas/*`,
so the helper lives here.  A later lane may lift it into `params.py` without changing a single
call site's wire behaviour.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.core.errors import E_VALIDATION, ERROR_HINTS, PolsiaError

__all__ = [
    "AD_CAMPAIGN_STATUSES",
    "PROSPECT_STATUSES",
    "RUN_STATUSES",
    "SOCIAL_POST_STATUSES",
    "require_status",
]

#: `ck_agent_runs_status` — `alembic/versions/0002_queue_and_perf.py:96-97`.
#: Written by `task_service.create_agent_run` ("running"), the worker ("completed"/"failed"),
#: the daily cycle ("failed"/"completed") and `maintenance.close_stale_runs` ("cancelled").
RUN_STATUSES: tuple[str, ...] = ("running", "completed", "failed", "cancelled")

#: `ck_prospects_status` — `alembic/versions/0002_queue_and_perf.py:99-100`.
PROSPECT_STATUSES: tuple[str, ...] = (
    "new",
    "contacted",
    "replied",
    "converted",
    "unsubscribed",
    "bounced",
)

#: `ck_ad_campaigns_status` — `alembic/versions/0002_queue_and_perf.py:104-105`.
AD_CAMPAIGN_STATUSES: tuple[str, ...] = ("active", "paused", "ended", "draft")

#: `ck_social_posts_status` — `alembic/versions/0002_queue_and_perf.py:102-103`.
SOCIAL_POST_STATUSES: tuple[str, ...] = ("draft", "scheduled", "published", "failed")


def require_status(value: str | None, valid: Sequence[str], *, what: str) -> None:
    """Refuse an unknown `status` BY NAME (`E_VALIDATION` -> 422) BEFORE any query runs.

    `None` means "no filter" and is always allowed.  The message and context are the exact shape
    `task_service.list_tasks` / `support_inbox_service.list_items` already answer, so all seven
    status-filtered reads now share one contract:
    `unknown <what> status '<value>'; valid: <the column's own values>` with
    `context={"status": value, "valid": [...]}`.
    """
    if value is None or value in valid:
        return
    raise PolsiaError(
        E_VALIDATION,
        f"unknown {what} status {value!r}; valid: {', '.join(valid)}",
        how_to_fix=ERROR_HINTS[E_VALIDATION],
        context={"status": value, "valid": list(valid)},
    )
