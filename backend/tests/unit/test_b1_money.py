"""Wave B1 / D4 — MONEY + EXPORT INTEGRITY: try to make A3's honest numbers lie again.

Every case below is from the seat brief.  The point is not to re-prove A3's docstrings; it is to
attack each number with the input that used to (or could still) collapse it into a fabricated
measurement, and to pin BOTH directions: the measured answer AND the lie it must never serve.

Finding map
-----------
A2A7-03  `reporting_currency_filter` — the currency cases: mixed USD+EUR, all-EUR (UNMEASURED, not
         0), NULL currency, lowercase `usd`, empty-string currency, and a sum whose only rows were
         excluded (the payload must name the row count).
F-19b    `build_cash_block.in_30d` — the inflow sum collapsed a money-in event with NO measured
         amount into a measured `$0.00` (the expense leg already refused to; the inflow leg did not).
A2A7-01  the ads spend: a campaign that never reported spend reads unknown, a repeat run REFRESHES
         the cumulative total (it does not add), and $0 -> $250 reads 250.
A1-03 / A8-02  the planner spend gate: a finished run with no cost refuses; an in-flight run does
         not; six unpriced runs beside one priced run never read as the priced one alone.
F-48 / A4-02 / A4-03  the export: a 3 000-row member streams (bytes handed to the archive), the
         manifest's `declared_total` equals the rows written, and a ledger that moved mid-export
         refuses `E_DB` instead of shipping a manifest that lies.
"""
from __future__ import annotations

import csv
import io
import json
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text

from app.core.errors import E_BUDGET_EXCEEDED, E_DB, PolsiaError
from app.core.timeutil import today_in
from app.models.ads import AdCampaign
from app.models.finance import ExpenseRecord

# ── helpers ──────────────────────────────────────────────────────────────────────────────────


async def _raw_expense(db: Any, *, cents: int, currency: str | None, vendor: str) -> None:
    """One expense row written the way a RAW/ops writer leaves it (no service normalisation).

    The service normaliser (`record_expense`) lowercases and maps blank -> None, so it can never
    produce the at-rest values this attack is about (a NULL, an empty string, an upper-case code
    from an older writer).  The filter's whole job is to survive those, so the fixture must write
    them exactly like the writer the rule exists for.
    """
    await db.execute(
        text(
            "INSERT INTO expense_records (category, vendor, amount_cents, currency, date)"
            " VALUES ('ads', :vendor, :cents, :currency, :day)"
        ),
        {
            "vendor": vendor,
            "cents": cents,
            "currency": currency,
            "day": today_in().isoformat(),
        },
    )


async def _usd_expense(db: Any, *, cents: int, vendor: str = "Google") -> None:
    from app.services.finance_service import record_expense

    await record_expense(
        db, category="ads", vendor=vendor, amount_cents=cents, currency="USD", date=today_in()
    )


# ── A2A7-03 — the currency filter, one case at a time ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_mixed_usd_eur_day_totals_the_usd_rows_only(async_db_session):
    """BOTH DIRECTIONS: $100.00 USD + €500.00 EUR reads 10000 cents, never 60000."""
    from app.services.finance_service import get_finance_summary, record_expense

    await _usd_expense(async_db_session, cents=10000)
    await record_expense(
        async_db_session,
        category="ads",
        vendor="Vendor GmbH",
        amount_cents=50000,
        currency="EUR",
        date=today_in(),
    )

    summary = await get_finance_summary(async_db_session)

    assert summary["total_expenses_month_cents"] == 10000, "a EUR row is not 50000 USD cents"
    assert summary["total_expenses_month_cents"] != 60000, "the mixed sum is the exact lie"
    assert summary["foreign_expenses_month_count"] == 1


@pytest.mark.asyncio
async def test_an_all_eur_day_is_unmeasured_not_a_measured_zero(async_db_session):
    """BOTH DIRECTIONS: no USD row at all ⇒ None (an em dash), never `0`."""
    from app.services.finance_service import get_finance_summary, record_expense

    for cents in (50000, 250):
        await record_expense(
            async_db_session,
            category="ads",
            vendor="Vendor GmbH",
            amount_cents=cents,
            currency="EUR",
            date=today_in(),
        )

    summary = await get_finance_summary(async_db_session)

    assert summary["total_expenses_month_cents"] is None, (
        "a day whose only rows are excluded measured NOTHING in USD; `0` would claim a "
        "measured zero dollar of spend"
    )
    assert summary["total_expenses_month_cents"] != 0
    assert summary["foreign_expenses_month_count"] == 2


@pytest.mark.asyncio
async def test_a_null_currency_row_is_unknown_and_counted(async_db_session):
    """BOTH DIRECTIONS: a NULL currency is NOT usd — excluded from the sum and COUNTED."""
    from app.services.finance_service import get_finance_summary

    await _raw_expense(async_db_session, cents=7000, currency=None, vendor="raw-null")
    stored = await async_db_session.scalar(
        select(ExpenseRecord.currency).where(ExpenseRecord.vendor == "raw-null")
    )
    assert stored is None, "the fixture must really carry the NULL currency"

    summary = await get_finance_summary(async_db_session)

    assert summary["total_expenses_month_cents"] is None
    assert summary["foreign_expenses_month_count"] == 1


@pytest.mark.asyncio
async def test_a_lowercase_and_an_uppercase_usd_at_rest_are_both_usd(async_db_session):
    """BOTH DIRECTIONS: `usd` and `USD` at rest are the same currency; `eur` still is not.

    `record_expense` stores `USD` as `usd`, but the column is text and older/raw writers left the
    upper-case spelling: the READ filter must fold case, or a legacy row silently leaves the
    owner's month total while the payload reports no exclusion either.
    """
    from app.services.finance_service import get_finance_summary

    await _raw_expense(async_db_session, cents=1234, currency="usd", vendor="raw-lower")
    await _raw_expense(async_db_session, cents=766, currency="USD", vendor="raw-upper")
    await _raw_expense(async_db_session, cents=999, currency="eur", vendor="raw-eur")

    summary = await get_finance_summary(async_db_session)

    assert summary["total_expenses_month_cents"] == 2000, "usd + USD = 2000; the eur row is out"
    assert summary["foreign_expenses_month_count"] == 1


@pytest.mark.asyncio
async def test_an_empty_string_currency_is_not_usd(async_db_session):
    """BOTH DIRECTIONS: `''` is an unstated unit, not dollars — excluded and counted.

    A raw writer that left the column blank must not be summed as USD, and must not vanish from
    the payload's exclusion count either (a silent drop is the other half of the same lie).
    """
    from app.services.finance_service import get_finance_summary

    await _raw_expense(async_db_session, cents=4321, currency="", vendor="raw-blank")
    stored = await async_db_session.scalar(
        select(ExpenseRecord.currency).where(ExpenseRecord.vendor == "raw-blank")
    )
    assert stored == "", "the fixture must really carry the empty-string currency"

    summary = await get_finance_summary(async_db_session)

    assert summary["total_expenses_month_cents"] is None
    assert summary["foreign_expenses_month_count"] == 1, "a blank currency is still an exclusion"


@pytest.mark.asyncio
async def test_a_sum_whose_only_rows_are_excluded_names_the_row_count(async_db_session):
    """BOTH DIRECTIONS: three excluded rows ⇒ the payload says 3, not "0 rows, $0.00"."""
    from app.services.finance_service import get_finance_summary, record_expense

    for cents, currency in ((100, "eur"), (200, "gbp"), (300, "jpy")):
        await record_expense(
            async_db_session,
            category="ads",
            vendor=f"Vendor {currency}",
            amount_cents=cents,
            currency=currency,
            date=today_in(),
        )

    summary = await get_finance_summary(async_db_session)

    assert summary["total_expenses_month_cents"] is None, "nothing was measured in USD"
    assert summary["foreign_expenses_month_count"] == 3, (
        "the label must NAME the number of excluded rows — that count is the only thing that "
        "says why the total is unmeasured"
    )
    assert summary["foreign_expenses_month_count"] != 0


@pytest.mark.asyncio
async def test_the_cash_block_refuses_to_price_a_money_in_event_with_no_amount(async_db_session):
    """F-19b, both directions: a USD `charge.succeeded` with NO amount is UNMEASURED inflow.

    The expense leg of the same block already refuses to sum an amount-less row into a fabricated
    `$0.00` ("expense rows in the window carry no measured amounts").  The inflow leg summed
    `case(...) -> amount_cents` and then `usd_in_cents or 0`, so one USD money-in delivery whose
    amount Stripe did not state served `in_30d = 0.0` — a measured zero for a payment nobody
    measured.
    """
    from app.services.report_service import build_cash_block

    async_db_session.add_all(
        [
            # the honest measurement that must survive beside it
            ExpenseRecord(
                category="ads", vendor="Google", amount_cents=10000,
                currency="usd", date=today_in(),
            ),
            _stripe_event("evt_no_amount", amount_cents=None),
        ]
    )
    await async_db_session.flush()

    block = await build_cash_block(async_db_session)

    assert block["in_30d"] is None, (
        "an amount-less money-in delivery is NOT $0.00 of inflow; the block must say unmeasured"
    )
    assert block["in_30d"] != 0.0
    assert "no measured amount" in block["reasons"]["in_30d"]
    assert block["out_30d"] == 100.0, "the measured expense beside it still serves"


@pytest.mark.asyncio
async def test_a_partial_money_in_sum_is_not_served_as_the_total(async_db_session):
    """BOTH DIRECTIONS: $50.00 measured + one amount-less delivery is UNMEASURED, not 50.00.

    The all-NULL case and the MIXED case are different lies.  The mixed one is A2A7-02's exact
    shape (`task_service.run_totals`): `SUM` skips the NULL row and hands back the priced part as
    if it were the whole inflow.  The same ledger with the amount present must read 50.00, so the
    refusal is about the missing measurement and not about refusing to serve inflow at all.
    """
    from app.services.report_service import build_cash_block

    async_db_session.add(_stripe_event("evt_priced", amount_cents=5000))
    await async_db_session.flush()
    assert (await build_cash_block(async_db_session))["in_30d"] == 50.0

    async_db_session.add(_stripe_event("evt_unpriced", amount_cents=None))
    await async_db_session.flush()

    block = await build_cash_block(async_db_session)
    assert block["in_30d"] is None, "50.00 is the priced PART, not the window's inflow"
    assert block["in_30d"] != 50.0
    assert "1 of the window's 2" in block["reasons"]["in_30d"], "the count must be named"


@pytest.mark.asyncio
async def test_events_with_no_money_in_are_still_a_measured_zero(async_db_session):
    """The guard on the fix: only a MISSING amount is unmeasured — a real zero stays 0.0.

    Telemetry exists and none of it is money in (a failed charge, say): the window's inflow is a
    measured `$0.00`, and the new refusal must not swallow that state.
    """
    from app.services.report_service import build_cash_block

    async_db_session.add(
        _stripe_event("evt_failed", amount_cents=5000, event_type="charge.failed")
    )
    await async_db_session.flush()

    block = await build_cash_block(async_db_session)

    assert block["in_30d"] == 0.0, "no money-in event at all is a measured zero"
    assert "in_30d" not in block["reasons"]


@pytest.mark.asyncio
async def test_the_thirty_day_window_counts_its_first_and_last_day_and_nothing_outside(async_db_session):
    """BOTH DIRECTIONS, at all four edges: `[today-29, today]` in, `tomorrow` and `+30` out.

    The window is `day_window(...)`'s half-open `[start, end)`, and the pre-fix reads used only
    `>= start` — the same unbounded-range defect `_month_bounds` records for the month.  Measured
    pre-fix: a real `out_30d` of 100.00 read 1099.99 (`raw/D4/probe_attacks.out.json` §B).

    The LOWER edge is pinned too: `expense_records.date` is a DATE column, so a DATE-vs-timestamp
    bound renders `'YYYY-MM-DD'` against `'YYYY-MM-DDThh:mm:ss'` on SQLite and silently drops the
    window's first day (the pre-fix read had exactly that bug on its lower edge).  Both bounds are
    compared as DATES here, so the first day is inside on both engines.
    """
    from app.services.report_service import CASH_WINDOW_DAYS, build_cash_block

    today = today_in()
    window_start = today - timedelta(days=CASH_WINDOW_DAYS - 1)

    async_db_session.add_all(
        [
            # the window's first day — INSIDE
            ExpenseRecord(
                category="taxes", vendor="first-day", amount_cents=1000,
                currency="usd", date=window_start,
            ),
            # today — INSIDE
            ExpenseRecord(
                category="ads", vendor="today", amount_cents=2000,
                currency="usd", date=today,
            ),
        ]
    )
    await async_db_session.flush()

    block = await build_cash_block(async_db_session)
    assert block["out_30d"] == 30.0, "both edges of the window are inside it"
    assert block["tax_set_aside"] == 10.0, "the tax read uses the same window"

    async_db_session.add_all(
        [
            # tomorrow (the day after the window) — OUTSIDE
            ExpenseRecord(
                category="ads", vendor="tomorrow", amount_cents=4000,
                currency="usd", date=today + timedelta(days=1),
            ),
            # a future-dated booking — OUTSIDE
            ExpenseRecord(
                category="taxes", vendor="next-month", amount_cents=99999,
                currency="usd", date=today + timedelta(days=30),
            ),
        ]
    )
    await async_db_session.flush()

    block = await build_cash_block(async_db_session)
    assert block["out_30d"] == 30.0, "a future-dated bill is not money already out"
    assert block["out_30d"] != 1099.99
    assert block["tax_set_aside"] == 10.0, "the future tax row stays outside the set-aside"


def _stripe_event(
    event_id: str, *, amount_cents: int | None, currency: str = "usd",
    event_type: str = "charge.succeeded",
):
    from app.core.database import utcnow
    from app.models.finance import StripeEvent

    return StripeEvent(
        stripe_event_id=event_id,
        event_type=event_type,
        amount_cents=amount_cents,
        currency=currency,
        processed_at=utcnow(),
    )


# ── A2A7-01 — the ads spend: unknown, refreshed, not accumulated ─────────────────────────────


def _ads_payload(**campaign: Any) -> dict:
    entry: dict[str, Any] = {
        "platform": "google_ads",
        "external_id": "camp-b1",
        "name": "B1 attack",
    }
    entry.update(campaign)
    return {"campaigns": [entry], "metrics": []}


async def _stored_campaign(db: Any) -> AdCampaign:
    return (await db.execute(select(AdCampaign).order_by(AdCampaign.id))).scalars().one()


@pytest.mark.asyncio
async def test_a_campaign_that_never_reported_spend_reads_unknown(async_db_session):
    """BOTH DIRECTIONS: the row is NULL and the owner's card is None — never `$0.00`."""
    from app.services.ads_service import record_ads_run
    from app.services.finance_service import get_finance_summary

    report = await record_ads_run(async_db_session, _ads_payload())
    assert report["ok"] is True, report

    stored = await _stored_campaign(async_db_session)
    assert stored.total_spent_usd is None, "an unreported spend is UNKNOWN, not a measured zero"

    summary = await get_finance_summary(async_db_session)
    assert summary["total_ad_spend_usd"] is None
    assert summary["total_ad_spend_usd"] != Decimal("0")


@pytest.mark.asyncio
async def test_a_repeat_run_refreshes_the_cumulative_spend_instead_of_adding_it(async_db_session):
    """BOTH DIRECTIONS: $10.00 then $250.00 reads 250 — never 260 (an accumulator) nor 10 (frozen)."""
    from app.services.ads_service import record_ads_run
    from app.services.finance_service import get_finance_summary

    first = await record_ads_run(async_db_session, _ads_payload(total_spent_usd=10.0))
    assert first["campaigns_written"] == 1

    second = await record_ads_run(async_db_session, _ads_payload(total_spent_usd=250.0))
    assert second["ok"] is True, second
    assert second["duplicates_skipped"] == 1, "the same provider id is the same campaign"
    assert second["campaigns_updated"] == 1, "a moved cumulative total is a write"

    stored = await _stored_campaign(async_db_session)
    assert stored.total_spent_usd == Decimal("250.0000")
    summary = await get_finance_summary(async_db_session)
    assert summary["total_ad_spend_usd"] == Decimal("250.0000")
    assert summary["total_ad_spend_usd"] != Decimal("260.0000"), "a refresh is not a += on a total"


@pytest.mark.asyncio
async def test_a_campaign_reporting_250_after_an_earlier_zero_reads_250(async_db_session):
    """BOTH DIRECTIONS: the `$0.00 -> $250.00` transition is the one `==` can silently swallow.

    A stored `Decimal('0.0000')` and a reported `0.0` compare equal, so a refresh that keyed on
    truthiness (`if value:`) would skip this exact update and the campaign would stay at $0.00.
    """
    from app.services.ads_service import record_ads_run

    zero = await record_ads_run(async_db_session, _ads_payload(total_spent_usd=0.0))
    assert zero["campaigns_written"] == 1
    stored = await _stored_campaign(async_db_session)
    assert stored.total_spent_usd == Decimal("0.0000"), "a reported 0 is a MEASURED zero"

    moved = await record_ads_run(async_db_session, _ads_payload(total_spent_usd=250.0))
    assert moved["campaigns_updated"] == 1, "0.0000 -> 250.0000 is a refresh"

    stored = await _stored_campaign(async_db_session)
    assert stored.total_spent_usd == Decimal("250.0000")
    assert stored.total_spent_usd != Decimal("0.0000"), "the $0.00 must not survive the refresh"


# ── A1-03 / A8-02 — the planner spend gate ──────────────────────────────────────────────────


def _run(*, cost: str | None, finished: bool, agent: str = "competitor_research"):
    from app.models.task import AgentRun

    started = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    return AgentRun(
        agent_type=agent,
        status="completed" if finished else "running",
        cost_usd=None if cost is None else Decimal(cost),
        started_at=started,
        ended_at=started + timedelta(minutes=1) if finished else None,
    )


@pytest.mark.asyncio
async def test_a_finished_run_with_no_cost_refuses_the_gate(async_db_session):
    """BOTH DIRECTIONS: the same day WITHOUT the unpriced run passes — so it is that row that refuses."""
    from app.services import planner_service

    async_db_session.add(_run(cost="3.0000", finished=True))
    await async_db_session.commit()
    await planner_service._gate_planner_budget(
        async_db_session, "company_planner", step="propose"
    )  # the measured-only day is allowed

    async_db_session.add(_run(cost=None, finished=True))
    await async_db_session.commit()

    with pytest.raises(PolsiaError) as excinfo:
        await planner_service._gate_planner_budget(
            async_db_session, "company_planner", step="propose"
        )

    assert excinfo.value.code == E_BUDGET_EXCEEDED
    assert "UNMEASURED" in excinfo.value.message
    assert excinfo.value.context["unmeasured_runs"] == 1
    assert excinfo.value.how_to_fix


@pytest.mark.asyncio
async def test_an_in_flight_run_does_not_refuse_the_gate(async_db_session):
    """BOTH DIRECTIONS: the open run neither refuses the gate nor inflates the measured total."""
    from app.services import planner_service
    from app.services.finance_service import day_spend_truth

    async_db_session.add_all([_run(cost="3.0000", finished=True), _run(cost=None, finished=False)])
    await async_db_session.commit()

    await planner_service._gate_planner_budget(async_db_session, "company_planner", step="propose")

    truth = await day_spend_truth(async_db_session)
    assert truth.total == Decimal("3.0000"), "the in-flight run is not guessed into the total"
    assert truth.in_flight == 1
    assert truth.finished_unpriced == 0
    assert truth.measured_runs == 1


@pytest.mark.asyncio
async def test_six_unpriced_runs_beside_one_priced_run_never_read_as_the_priced_one_alone(
    async_db_session,
):
    """BOTH DIRECTIONS: `total` is None (not 3.00) and the refusal names all SIX.

    This is the F-19 shape at scale: one priced run ($3.00) next to six finished runs that never
    wrote a cost.  A sum that ignores the NULLs serves `$3.00` as the day's spend — a partial
    total presented as a measurement, and the gate then spends on the headroom it never had.
    """
    from app.services import planner_service
    from app.services.finance_service import day_spend_truth

    async_db_session.add(_run(cost="3.0000", finished=True))
    async_db_session.add_all([_run(cost=None, finished=True) for _ in range(6)])
    await async_db_session.commit()

    truth = await day_spend_truth(async_db_session)

    assert truth.total is None, "the priced run alone is NOT the day's spend"
    assert truth.total != Decimal("3.0000")
    assert truth.finished_unpriced == 6
    assert truth.measured_runs == 1
    assert truth.per_agent == {"competitor_research": Decimal("3.0000")}, (
        "the measured row is still named — it is outside the total, not hidden"
    )

    with pytest.raises(PolsiaError) as excinfo:
        await planner_service._gate_planner_budget(
            async_db_session, "company_planner", step="propose"
        )
    assert excinfo.value.code == E_BUDGET_EXCEEDED
    assert excinfo.value.context["unmeasured_runs"] == 6
    assert "6 finished run(s)" in excinfo.value.message


# ── F-48 / A4-02 / A4-03 — the export streams and declares a matching total ─────────────────


def _archive_bytes(monkeypatch) -> dict[str, list[int]]:
    """Record the size of every payload handed to the archive, per member name.

    The measurement is BYTES HANDED TO THE ARCHIVE, not RSS: a whole-member build hands over one
    payload the size of the member, a streamed build hands over page-sized chunks.
    """
    sizes: dict[str, list[int]] = {}
    real_open = zipfile.ZipFile.open
    real_writestr = zipfile.ZipFile.writestr

    class _WriteCounter:
        def __init__(self, handle: Any, name: str) -> None:
            self._handle = handle
            self._name = name

        def write(self, data: bytes) -> int:
            sizes.setdefault(self._name, []).append(len(data))
            return self._handle.write(data)

        def flush(self) -> None:
            self._handle.flush()

        def writable(self) -> bool:
            return True

        def __enter__(self) -> _WriteCounter:
            return self

        def __exit__(self, *exc_info: Any) -> None:
            self._handle.close()

    def counting_open(self: zipfile.ZipFile, name: Any, mode: str = "r", *args: Any, **kwargs: Any):
        handle = real_open(self, name, mode, *args, **kwargs)
        return _WriteCounter(handle, str(name)) if mode == "w" else handle

    def counting_writestr(self: zipfile.ZipFile, info: Any, data: Any, *args: Any, **kwargs: Any):
        name = info if isinstance(info, str) else str(getattr(info, "filename", info))
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        sizes.setdefault(name, []).append(len(payload))
        return real_writestr(self, info, data, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", counting_open)
    monkeypatch.setattr(zipfile.ZipFile, "writestr", counting_writestr)
    return sizes


async def _seed_runs(db: Any, count: int) -> None:
    from app.models.task import AgentRun

    db.add_all(
        [
            AgentRun(
                agent_type="code_generation",
                status="succeeded",
                usage_json={"note": "x" * 200, "n": index},
            )
            for index in range(count)
        ]
    )
    await db.flush()


@pytest.mark.asyncio
async def test_a_three_thousand_row_member_streams_into_the_archive(async_db_session, monkeypatch):
    """A4-02, BOTH DIRECTIONS: page-sized chunks AND the whole member really is in the archive.

    The negative direction matters as much as the positive one: a "streaming" build that wrote
    nothing would also pass a max-chunk assertion, so the row count, the header and the declared
    total are all pinned from the SAME archive.
    """
    from app.api.v1 import data as data_module

    seeded = 3_000
    await _seed_runs(async_db_session, seeded)

    sizes = _archive_bytes(monkeypatch)
    path, total_rows, exported_at = await data_module._build_export(async_db_session)
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("MANIFEST.json"))
            csv_text = archive.read("agent_runs.csv").decode("utf-8")
            by_name = {member["name"]: member for member in manifest["members"]}
    finally:
        data_module._remove_export_tempfile(path)

    assert by_name["agent_runs.csv"]["row_count"] == seeded
    assert manifest["total_rows"] == total_rows == seeded
    assert len(csv_text.splitlines()) == seeded + 1, "header + one line per run"
    assert csv_text.split("\n", 1)[0] == ",".join(by_name["agent_runs.csv"]["columns"]), (
        "the archive must carry the columns the manifest declares"
    )

    chunks = sizes["agent_runs.csv"]
    total_bytes = sum(chunks)
    assert total_bytes > 200_000, "the member must be big enough for the measurement to mean something"
    assert len(chunks) >= 2, "a streamed member is handed over in more than one chunk"
    assert max(chunks) < total_bytes / 2, (
        f"one call handed the archive {max(chunks)} of {total_bytes} bytes: the member was "
        "materialised instead of streamed"
    )


@pytest.mark.asyncio
async def test_the_declared_total_matches_the_rows_written_for_every_member(async_db_session):
    """A4-03, BOTH DIRECTIONS: manifest count == rows in the archive == live COUNT(*).

    The independent third opinion is the database's own count taken AFTER the archive was built:
    a manifest that declared its own intended number instead of the written one cannot agree with
    all three.
    """
    from app.api.v1 import data as data_module
    from app.models.chat import ChatMessage

    await _seed_runs(async_db_session, 7)
    async_db_session.add_all([ChatMessage(role="owner", content=f"note {i}") for i in range(3)])
    await async_db_session.flush()

    path, total_rows, exported_at = await data_module._build_export(async_db_session)
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("MANIFEST.json"))
            written: dict[str, int] = {}
            for member in manifest["members"]:
                raw = archive.read(member["name"])
                if member["format"] == "csv":
                    written[member["table"]] = len(list(csv.reader(io.StringIO(raw.decode())))) - 1
                else:
                    written[member["table"]] = len(json.loads(raw))
    finally:
        data_module._remove_export_tempfile(path)

    declared = {member["table"]: member["row_count"] for member in manifest["members"]}
    assert written == declared, "a manifest may only declare what the archive really carries"
    assert total_rows == sum(written.values()) == manifest["total_rows"]

    for table, model in data_module.EXPORT_MODELS.items():
        live = len((await async_db_session.execute(select(model).limit(10_000))).scalars().all())
        assert declared[table] == live, f"{table}: the manifest disagrees with the ledger"

    assert declared["agent_runs"] == 7 and declared["chat_messages"] == 3


@pytest.mark.asyncio
async def test_a_row_count_mismatch_refuses_with_e_db_and_leaves_no_archive(
    async_db_session, monkeypatch, tmp_path
):
    """A4-03's refusal face: a ledger that moved mid-export must refuse, not ship a lying manifest.

    Simulated the way it really happens — the count is taken FIRST, then the stream sees one row
    fewer (a concurrent delete).  The refusal must be a coded `E_DB` naming both numbers, and the
    half-written temp archive must not survive.
    """
    from app.api.v1 import data as data_module

    await _seed_runs(async_db_session, 12)

    real_stream = data_module._stream_rows
    seen = {"agent_runs": False}

    async def shrinking_stream(db: Any, model: Any):
        dropped = 0
        async for row in real_stream(db, model):
            if getattr(model, "__tablename__", "") == "agent_runs":
                seen["agent_runs"] = True
                if dropped == 0:
                    dropped += 1
                    continue  # the row the COUNT(*) saw is gone by the time the stream arrives
            yield row

    monkeypatch.setattr(data_module, "_stream_rows", shrinking_stream)

    scratch = Path(tempfile.gettempdir())
    before = {p.name for p in scratch.glob(data_module.EXPORT_TEMP_PREFIX + "*")}

    with pytest.raises(PolsiaError) as excinfo:
        await data_module._build_export(async_db_session)

    assert seen["agent_runs"], "the attack never reached the member it is about"
    assert excinfo.value.code == E_DB
    assert excinfo.value.context == {"table": "agent_runs", "declared": 12, "written": 11}
    assert "manifest declares 12 row(s) but 11 were written" in excinfo.value.message
    assert excinfo.value.how_to_fix
    after = {p.name for p in scratch.glob(data_module.EXPORT_TEMP_PREFIX + "*")}
    assert after == before, f"a refused export leaked its temp archive: {after - before}"
