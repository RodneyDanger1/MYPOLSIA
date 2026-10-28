"""W2 TS-1 — `/finance/revenue` carries a measured/unmeasured provenance marker per snapshot row.

THE DEFECT.  `RevenueSnapshotResponse` served seven nullable money/count columns with no marker
for WHICH of them were measured.  After the wave-1 repair, a legacy row (its fabricated zeros set
to NULL) and a genuinely partial row look the same shape, and an all-NULL row has no marker at
all — every client inferred the state differently, and the finance table rendered a partial day
exactly like a complete one.

THE FIX under test: the service derives `measured` / `unmeasured` / `provenance` from the row's
own values (a supplied `0` is a MEASURED zero, not an absent one) and the route attaches them to
the wire row.  The seven pre-TS-1 fields are unchanged and in order; the three new fields are
appended last, so every pre-TS-1 client keeps parsing the response.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models.finance import RevenueSnapshot
from app.services.finance_service import SNAPSHOT_MEASUREMENT_COLUMNS, snapshot_provenance

#: A row where every measurement column carries a value (the `complete` state).
COMPLETE = {
    "mrr_cents": 100000,
    "arr_cents": 1200000,
    "active_subscribers": 3,
    "churned_today": 0,
    "new_today": 1,
    "total_revenue_month_cents": 50000,
    "stripe_balance_cents": 0,
}


# ── 1. the derivation (a plain unit pin: no HTTP needed) ─────────────────────────────────────


def test_measured_is_the_non_null_columns_in_column_order_and_zero_is_measured() -> None:
    complete = snapshot_provenance(COMPLETE)
    assert complete["measured"] == list(SNAPSHOT_MEASUREMENT_COLUMNS)
    assert complete["unmeasured"] == []
    assert complete["provenance"] == "complete"

    # 0 is a MEASURED zero: `is not None`, never truthiness.  This is the same law the writer
    # obeys (DT-01) and it is the difference the marker must preserve on the wire.
    partial = snapshot_provenance({"total_revenue_month_cents": 0, "stripe_balance_cents": 0})
    assert partial["measured"] == ["total_revenue_month_cents", "stripe_balance_cents"]
    assert partial["unmeasured"] == [
        "mrr_cents",
        "arr_cents",
        "active_subscribers",
        "churned_today",
        "new_today",
    ]
    assert partial["provenance"] == "partial"

    empty = snapshot_provenance({})
    assert empty["measured"] == []
    assert empty["unmeasured"] == list(SNAPSHOT_MEASUREMENT_COLUMNS)
    assert empty["provenance"] == "unmeasured"


# ── 2. the wire: the marker travels with the row, and the old keys do not move ───────────────


@pytest.mark.asyncio
async def test_revenue_wire_carries_the_marker_for_complete_partial_and_unmeasured_rows(
    async_db_session, api_client, auth_headers
) -> None:
    async_db_session.add_all(
        [
            RevenueSnapshot(snapshot_date=date(2026, 10, 1), **COMPLETE),
            # the repaired/partial shape: two measured values, one of them a measured zero
            RevenueSnapshot(
                snapshot_date=date(2026, 10, 2),
                total_revenue_month_cents=0,
                stripe_balance_cents=0,
            ),
            # a row with a date and no measurement at all (raw/ops writer)
            RevenueSnapshot(snapshot_date=date(2026, 10, 3)),
        ]
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/finance/revenue", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    rows = {row["snapshot_date"]: row for row in resp.json()}
    assert set(rows) == {"2026-10-01", "2026-10-02", "2026-10-03"}

    complete = rows["2026-10-01"]
    assert complete["measured"] == list(SNAPSHOT_MEASUREMENT_COLUMNS)
    assert complete["unmeasured"] == []
    assert complete["provenance"] == "complete"
    assert complete["mrr_cents"] == 100000, "the pre-TS-1 values are served unchanged"

    partial = rows["2026-10-02"]
    assert partial["measured"] == ["total_revenue_month_cents", "stripe_balance_cents"]
    assert partial["provenance"] == "partial"
    # a measured zero stays a zero; an unmeasured column stays null (the em-dash contract)
    assert partial["total_revenue_month_cents"] == 0
    assert partial["mrr_cents"] is None

    unmeasured = rows["2026-10-03"]
    assert unmeasured["measured"] == []
    assert unmeasured["unmeasured"] == list(SNAPSHOT_MEASUREMENT_COLUMNS)
    assert unmeasured["provenance"] == "unmeasured"

    # The seven original keys are still present, and every value is null where nothing measured.
    old_keys = {
        "id",
        "snapshot_date",
        "mrr_cents",
        "arr_cents",
        "active_subscribers",
        "churned_today",
        "new_today",
        "total_revenue_month_cents",
        "stripe_balance_cents",
        "created_at",
    }
    assert old_keys <= set(complete), sorted(set(complete) - old_keys)
    assert {"measured", "unmeasured", "provenance"} <= set(complete)


@pytest.mark.asyncio
async def test_the_summary_contract_is_untouched_by_ts1(async_db_session, api_client, auth_headers) -> None:
    """TS-1 is additive on `/finance/revenue`; `/finance/summary` keeps its exact old key set."""
    pre_ts1 = {
        "mrr_cents",
        "arr_cents",
        "active_subscribers",
        "total_ad_spend_usd",
        "total_expenses_month_cents",
        "stripe_balance_cents",
        "last_snapshot_date",
        "ai_spend",
    }
    resp = await api_client.get("/api/v1/finance/summary", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == pre_ts1, "the summary wire must not grow TS-1's marker"
    assert resp.json()["mrr_cents"] is None, "no snapshot row exists yet: unmeasured, never 0"
