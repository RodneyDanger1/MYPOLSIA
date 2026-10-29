from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, field_validator

from app.schemas.common import ORMModel, UtcDatetime


class ActivityEventResponse(ORMModel):
    id: int
    agent_type: str
    action: str
    summary: str
    detail: dict[str, Any] | None = None
    level: str = "info"
    created_at: UtcDatetime | None = None

    @field_validator("level", mode="before")
    @classmethod
    def _level_defaults_to_info(cls, value: object) -> object:
        """NULL means the writer did not set a level; it is not an outage (A-T3 decision b).

        The column is DDL-nullable and only raw/Core inserts reach NULL (the ORM fires its
        `default="info"`).  The response vocabulary stays one word, so the boundary
        normalises None -> "info" here — the same rule `events.activity_payload` applies to
        the websocket payload.  Without this the feed's `model_validate` raised on a NULL
        row and the endpoint answered 500 (parent-surgical completion of T3).
        """
        return "info" if value is None else value


class DailyReportResponse(ORMModel):
    id: int
    report_date: date
    morning_plan: str | None = None
    evening_summary: str | None = None
    tasks_planned: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    metrics_snapshot: dict[str, Any] | None = None
    insights: list[str] | None = None
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None
    # V9 B2: the cash/tax block is COMPOSED by the daily-report route from live finance reads
    # (report_service.build_cash_block) — it is not a `daily_reports` column, so the ORM row
    # leaves it at this default and the route fills it in.  None = not attached (no report).
    cash_block: dict[str, Any] | None = None


class DashboardMeasuredFinance(BaseModel):
    """TS-4 — the finance ledger's measured headline, beside the owner-configured KPIs.

    THE DEFECT.  `DashboardSummaryResponse.kpis` is `company_config.kpis` verbatim — values the
    OWNER typed into Settings.  The dashboard page rendered them as "MRR / Active customers /
    Churn rate", so a configured `mrr_usd: 0` read as a measured `$0.00` on the same screen where
    `/finance/summary` correctly said MRR is unmeasured (`mrr_cents: null`).  Two surfaces
    disagreed about the same fact, and the dashboard's copy was the false one.

    THIS BLOCK is the measured counterpart, read from the SAME newest `revenue_snapshots` row
    `get_finance_summary` uses (one statement, no second round trip):

      * `mrr_cents` / `active_subscribers` — `None` means UNMEASURED (the exact value the finance
        page serves), `0` means a measured zero;
      * `last_snapshot_date` — the date of that row (None when no snapshot exists);
      * `provenance` — the TS-1 coverage marker for the row: `complete` / `partial` / `unmeasured`
        (`finance_service.snapshot_provenance`), so a partial day cannot be presented as a full
        reading.

    There is deliberately no measured `churn_rate`: the ledger stores `churned_today` (a COUNT of
    that day's churn), and dividing it is arithmetic the data does not support, so the dashboard
    reports the configured churn rate as configured only.
    """

    mrr_cents: int | None = None
    active_subscribers: int | None = None
    last_snapshot_date: date | None = None
    provenance: str = "unmeasured"


class DashboardReportCycle(BaseModel):
    """R-W10-1 — the report cycle's REAL state, derived from committed rows, never guessed.

    THE DEFECT.  The panel had exactly two facts: `last_report_date` (the newest
    `daily_reports.report_date`) and `GET /dashboard/reports/daily` (today's report or `null`).
    With beat OFF by policy, a stale date and a broken cycle rendered IDENTICALLY — the UI could
    not tell "the cycle has not run since 2026-10-20" from "the cycle is running and failing".
    Worse, `last_report_date` itself is not a completed report: the MORNING cycle
    (`save_morning_plan`) creates the row and the EVENING cycle (`save_evening_summary`) is what
    writes the report — the live row for 2026-10-20 has a morning plan and NO evening summary, so
    the dashboard's "last report 2026-10-20" overstated what was written (the last COMPLETED
    report is 2026-10-19).  `last_completed_report_date` here states that fact separately; the
    pre-existing `last_report_date` is left untouched (no behaviour change).

    EVIDENCE, not inference.  The daily cycle commits an `activity_log` row with
    `action='cycle_claimed'` and `detail={cycle, day}` BEFORE any spend
    (`celery_app/tasks/daily_cycle.py::_claim_cycle_tick`), so a claim row is durable proof that
    the cycle STARTED for that company-local day; a `daily_reports` row with a non-NULL
    `evening_summary` is proof the report was WRITTEN.  Everything below is read from those two
    ledgers (plus `cycle_tick_skipped` for the last named skip reason) — nothing is scheduled,
    estimated, or inferred about the future.

    WHY there is no `next_run_scheduled: bool`: the report cycle is a Celery **beat** entry in a
    separate process.  The API cannot observe whether beat is running (it is profile-gated and
    OFF by policy in this deployment), so a boolean there would be a guess wearing a measurement's
    shape.  The block declares `scheduler_observable: false` plus the reason instead.

    `state` vocabulary (never a fabricated happy path — `unknown` when the evidence cannot decide):

      * `never_triggered` — no claim ever, no report ever: nothing has run on this deployment;
      * `not_run_today` — the newest report-cycle claim is an earlier day whose report WAS written;
        no cycle has started since (the honest "idle, not broken" state);
      * `report_running` — the report cycle claimed TODAY and today's evening summary is not
        written yet (in progress — the panel must not call it failed);
      * `report_written_today` — claimed today AND the summary exists;
      * `report_missing` — the report cycle claimed a PAST day and no evening summary exists for
        that day: it started and did not finish.  THIS is the "broken" signature;
      * `unknown` — no claim is retained but reports exist, the claim's day is unparsable, or the
        claimed day is in the future (clock/attribution fault).  Never guessed into another state.
    """

    state: str = "unknown"
    #: One sentence naming the comparison that decided `state` — derived, not authored per state.
    state_reason: str | None = None
    #: The newest `cycle_claimed` row of ANY cycle (the last time a cycle started), and its
    #: own `detail.cycle` / `detail.day` (company-local ISO day as the writer recorded it).
    last_claim_at: UtcDatetime | None = None
    last_claim_cycle: str | None = None
    last_claim_day: str | None = None
    #: The newest `cycle_claimed` row of the REPORT cycle (evening) — the writer of the report.
    last_evening_claim_day: str | None = None
    #: Newest `daily_reports.report_date` whose `evening_summary` is non-NULL.  A row can exist
    #: without one (the morning created it) — that is why this is separate from
    #: `last_report_date`, and why `null` here means "no report has ever been completed".
    last_completed_report_date: date | None = None
    #: The newest `cycle_tick_skipped.reason` (e.g. `already_claimed`, `paused`, `budget_full`) —
    #: the last named reason a tick did NOT run.  `None` = no skip is retained.
    last_skip_reason: str | None = None
    #: The oldest retained claim row: the evidence window's floor.  Claims are purged by the
    #: 90-day activity retention, so "no claim" means "none in this window", not "never".
    claims_retained_since: UtcDatetime | None = None
    #: Always False on this deployment: the beat process is invisible to the API (see docstring).
    scheduler_observable: bool = False
    scheduler_reason: str | None = None


class DashboardSummaryResponse(BaseModel):
    """`GET /dashboard/summary`.

    TS-4 additive provenance (appended AFTER the pre-existing keys, so every pre-TS-4 client keeps
    parsing the seven original fields unchanged): `kpis` remains the configured values verbatim,
    but the response now DECLARES their provenance (`kpis_provenance: "configured"`) and carries
    the measured finance headline beside them (`measured_finance`).  A configured KPI zero can no
    longer be the only thing a client sees on this wire.

    R-W10-1 appends `report_cycle` after that — the report cycle's evidence-derived state, so a
    schedule that has not run and a cycle that is failing stop rendering identically.
    """

    tasks_today_total: int = 0
    tasks_today_completed: int = 0
    tasks_today_pending: int = 0
    tasks_today_failed: int = 0
    active_agents: list[str] = []
    kpis: dict[str, Any] = {}
    last_report_date: date | None = None
    kpis_provenance: str = "configured"
    measured_finance: DashboardMeasuredFinance | None = None
    report_cycle: DashboardReportCycle = DashboardReportCycle()


class ReportsListResponse(BaseModel):
    """`GET /dashboard/reports` — the documents overlay's page + the TABLE's count (V8/C5).

    V20/L1 (card L2-06): this wrapper used to be an untyped `dict[str, Any]`; the field order is
    the dict's own build order, and each item is the same `DailyReportResponse` the old route
    dumped into it, so the served bytes do not move.  `total` is the honest table count — the
    route refuses `E_DB` rather than fabricate a 0 when the count read returns no row (L2-09).
    """

    items: list[DailyReportResponse]
    total: int
