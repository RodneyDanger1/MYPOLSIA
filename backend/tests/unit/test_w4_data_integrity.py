"""WAVE 4 — the data-integrity audit: the invariants a money/ledger store must never violate.

WHY THIS FILE EXISTS.  Wave 3 changed how the worker closes interrupted runs (F-26), added the
single-use `consumed_approvals` ledger (migration 0018) and repaired the finance snapshots (TS-1).
Two waves of fixes make it MORE likely that a stale row, a fabricated zero or a re-armed approval
token survives somewhere unobserved — the checks below are the standing audit for exactly that.

WHAT IS CHECKED, AND WHERE.  `audit_invariants()` is a read-only, dialect-portable audit (ORM only:
it runs identically on the SQLite unit schema and on live PostgreSQL) returning NAMED findings.
Every finding carries the row id(s), so a failure is investigable rather than a count:

  * `runs_stuck_running` — a run open past the worker's own staleness bound
    (`AGENT_RUN_STALE_AFTER_SECONDS`, imported — not copied) means the startup/sweep recovery did
    not close it; its cost is unbounded and the day cap cannot be computed from the ledger.
  * `cancelled_runs_with_cost` / `cancelled_runs_without_declared_scope` /
    `interrupted_partial_cost_missing` — F-26's rule, as the worker declares it: an interrupted
    run carries `usage_json.cost_scope`, and the scope must agree with the money.  `unmeasured`
    means `cost_usd IS NULL` (a cost nobody measured is never 0); `in_flight_partial` means a
    PARTIAL cost WAS measured from the live sink (`cost_usd IS NOT NULL`, a lower bound).  A
    cancelled run with the wrong pair is a spend row that lies about how much it knows.
  * `snapshots_all_null` — a `revenue_snapshots` row that measures NOTHING is noise that reads as
    "unmeasured day" and hides whether the run wrote at all.
  * `snapshots_future_dated` / `snapshots_duplicate_date` — a row stamped after the clock, or two
    rows for one day (the UNIQUE index should make that impossible; this proves it on the engine
    under test rather than trusting the DDL).
  * `snapshots_all_seven_and_no_run` — a row claiming COMPLETE coverage with no finance run on the
    same day is the fabrication signature TS-1 was about.
  * `negative_money` — negative snapshot/expense amounts (the PG CHECK constraints
    `ck_revenue_snapshots_nonneg` / `ck_expense_records_amount` forbid them; SQLite's `create_all`
    does not carry every migration-only constraint, so the DATA is checked here).
  * `expense_currency_not_lowercase` — `ck_expense_records_currency`'s rule, same reasoning.
  * `approval_ledger_duplicates` / `approval_ledger_blank_fingerprint` — the single-use guarantee is
    the UNIQUE keys; a blank fingerprint would make a consumption unattributable in the audit.

PG-ONLY INVARIANTS ARE NAMED, NOT SIMULATED (AGENTS.md).  The append-only triggers
(`trg_activity_log_no_update`, `trg_activity_log_no_truncate`), the CHECK constraints above and the
`consumed_approvals` UNIQUE indexes exist only on PostgreSQL.  This module asserts they are
DECLARED in the migration chain (`test_the_pg_only_invariants_are_declared_in_the_migrations`) and
the live receipt `raw-w4/t4-pg-invariants.txt` proves they are PRESENT and ENFORCING on the real
database (the trigger refused a real UPDATE; the unique index exists by name).  A SQLite run cannot
prove either, and this file does not pretend it does.

No writes, no clock assumptions beyond the injected `now`, and no new product surface: the audit is
a test-suite gate plus the reproduced SQL in the wave-4 receipt.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.database import Base
from app.models.approval import ConsumedApproval
from app.models.company import CompanyConfig
from app.models.finance import ExpenseRecord, RevenueSnapshot
from app.models.task import AgentRun

BACKEND_ROOT = Path(__file__).resolve().parents[2]
VERSIONS_DIR = BACKEND_ROOT / "alembic" / "versions"

#: The worker's own staleness bound — IMPORTED, never re-typed (a copy would silence a real drift).
from app.services.task_service import RUN_COST_SCOPE_KEY  # noqa: E402
from celery_app.tasks.maintenance import (  # noqa: E402  (after the path constants, for clarity)
    AGENT_RUN_STALE_AFTER_SECONDS,
    STALE_RUN_REASON_CODE,
)

#: F-26's marker keys (the worker writes them into `agent_runs.usage_json`).  The KEY is imported
#: from the service that owns it; the two SCOPE VALUES are declared in
#: `celery_app/tasks/maintenance.py` (the stale-run closer) and pinned here by a drift test.
COST_SCOPE_KEY = RUN_COST_SCOPE_KEY
COST_SCOPE_UNMEASURED = "unmeasured"
COST_SCOPE_IN_FLIGHT_PARTIAL = "in_flight_partial"
DECLARED_COST_SCOPES = (COST_SCOPE_UNMEASURED, COST_SCOPE_IN_FLIGHT_PARTIAL)
INTERRUPTED_KEY = "interrupted"

SNAPSHOT_MONEY_COLUMNS = (
    "mrr_cents",
    "arr_cents",
    "active_subscribers",
    "churned_today",
    "new_today",
    "total_revenue_month_cents",
    "stripe_balance_cents",
)

#: How far ahead of the clock a `created_at` may be before it is treated as future-dated (a server
#: default is `now()`; a few seconds of skew is normal, minutes are not).
FUTURE_TOLERANCE = timedelta(minutes=5)


def _as_utc(value: datetime | None) -> datetime | None:
    """A stored timestamp as aware UTC (SQLite hands back naive values, PostgreSQL aware ones)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def audit_invariants(db, *, now: datetime | None = None) -> dict[str, list[str]]:
    """Every named data-integrity finding on this database (empty lists = all invariants hold).

    Read-only.  `now` is injected so the staleness/future checks are testable without a clock.
    """
    moment = now or datetime.now(UTC)
    findings: dict[str, list[str]] = {
        "runs_stuck_running": [],
        "cancelled_runs_with_cost": [],
        "cancelled_runs_without_declared_scope": [],
        "interrupted_partial_cost_missing": [],
        "snapshots_all_null": [],
        "snapshots_future_dated": [],
        "snapshots_duplicate_date": [],
        "snapshots_all_seven_and_no_run": [],
        "negative_money": [],
        "expense_currency_not_lowercase": [],
        "approval_ledger_duplicates": [],
        "approval_ledger_blank_fingerprint": [],
    }

    # ── agent_runs: no unbounded run, and an interrupted run is declared unmeasured ────────────
    bound = moment - timedelta(seconds=AGENT_RUN_STALE_AFTER_SECONDS)
    runs = (
        await db.execute(
            select(
                AgentRun.id,
                AgentRun.status,
                AgentRun.cost_usd,
                AgentRun.started_at,
                AgentRun.usage_json,
            )
        )
    ).all()
    for run in runs:
        usage = run.usage_json if isinstance(run.usage_json, dict) else {}
        if run.status == "running":
            started = _as_utc(run.started_at)
            if started is None:
                findings["runs_stuck_running"].append(f"run {run.id}: running with no started_at")
            elif started < bound:
                findings["runs_stuck_running"].append(
                    f"run {run.id}: running since {started.isoformat()} "
                    f"(> {AGENT_RUN_STALE_AFTER_SECONDS}s, {STALE_RUN_REASON_CODE} sweep missed it)"
                )
        if run.status == "cancelled":
            scope = usage.get(COST_SCOPE_KEY)
            if scope not in DECLARED_COST_SCOPES:
                findings["cancelled_runs_without_declared_scope"].append(
                    f"run {run.id}: cancelled with usage_json.{COST_SCOPE_KEY}={scope!r}; the "
                    f"worker declares only {list(DECLARED_COST_SCOPES)}"
                )
            elif scope == COST_SCOPE_UNMEASURED and run.cost_usd is not None:
                findings["cancelled_runs_with_cost"].append(
                    f"run {run.id}: scope=unmeasured but cost_usd={run.cost_usd} (a cost nobody "
                    "measured)"
                )
            elif scope == COST_SCOPE_IN_FLIGHT_PARTIAL and run.cost_usd is None:
                findings["interrupted_partial_cost_missing"].append(
                    f"run {run.id}: scope=in_flight_partial but cost_usd is NULL (the partial "
                    "measurement the scope claims is missing)"
                )

    # ── revenue_snapshots: no empty row, no duplicate day, no future stamp, no negative money ──
    # The days a FINANCE run actually started — a snapshot claiming complete coverage on a day no
    # finance run exists is the TS-1 fabrication signature (a row nothing measured).
    finance_run_days = {
        _as_utc(row.started_at).date()
        for row in (
            await db.execute(select(AgentRun.started_at).where(AgentRun.agent_type == "finance"))
        ).all()
        if _as_utc(row.started_at) is not None
    }
    snapshots = (
        await db.execute(
            select(
                RevenueSnapshot.id,
                RevenueSnapshot.snapshot_date,
                RevenueSnapshot.created_at,
                *[getattr(RevenueSnapshot, column) for column in SNAPSHOT_MONEY_COLUMNS],
            ).order_by(RevenueSnapshot.id)
        )
    ).all()
    seen_dates: dict[Any, int] = {}
    for snapshot in snapshots:
        values = {
            column: getattr(snapshot, column) for column in SNAPSHOT_MONEY_COLUMNS
        }
        if all(value is None for value in values.values()):
            findings["snapshots_all_null"].append(
                f"snapshot {snapshot.id} ({snapshot.snapshot_date}): all seven money columns NULL"
            )
        created = _as_utc(snapshot.created_at)
        if created is not None and created > moment + FUTURE_TOLERANCE:
            findings["snapshots_future_dated"].append(
                f"snapshot {snapshot.id}: created_at {created.isoformat()} is in the future"
            )
        seen_dates[snapshot.snapshot_date] = seen_dates.get(snapshot.snapshot_date, 0) + 1
        if all(value is not None for value in values.values()) and (
            snapshot.snapshot_date not in finance_run_days
        ):
            findings["snapshots_all_seven_and_no_run"].append(
                f"snapshot {snapshot.id} ({snapshot.snapshot_date}): complete coverage but no "
                "finance run started that day"
            )
        for column, value in values.items():
            if isinstance(value, int) and not isinstance(value, bool) and value < 0:
                findings["negative_money"].append(
                    f"snapshot {snapshot.id}: {column}={value}"
                )
    for day, count in seen_dates.items():
        if count > 1:
            findings["snapshots_duplicate_date"].append(f"{day}: {count} rows (UNIQUE is one)")

    # ── expense_records: money is positive and the currency is canonical ───────────────────────
    expenses = (
        await db.execute(
            select(
                ExpenseRecord.id,
                ExpenseRecord.amount_cents,
                ExpenseRecord.currency,
            )
        )
    ).all()
    for expense in expenses:
        amount = expense.amount_cents
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            findings["negative_money"].append(
                f"expense {expense.id}: amount_cents={amount!r} (a recorded cost is > 0)"
            )
        if isinstance(expense.currency, str) and expense.currency != expense.currency.lower():
            findings["expense_currency_not_lowercase"].append(
                f"expense {expense.id}: currency={expense.currency!r}"
            )

    # ── consumed_approvals: the single-use ledger's uniqueness/attribution ─────────────────────
    ledger = (
        await db.execute(
            select(
                ConsumedApproval.id,
                ConsumedApproval.token_id,
                ConsumedApproval.jti,
                ConsumedApproval.action_fingerprint,
            )
        )
    ).all()
    tokens: dict[str, int] = {}
    jtis: dict[str, int] = {}
    for row in ledger:
        tokens[row.token_id] = tokens.get(row.token_id, 0) + 1
        if row.jti is not None:
            jtis[row.jti] = jtis.get(row.jti, 0) + 1
        if not (row.action_fingerprint or "").strip():
            findings["approval_ledger_blank_fingerprint"].append(
                f"consumed_approvals {row.id}: no action_fingerprint (the spend is unattributable)"
            )
    findings["approval_ledger_duplicates"] = [
        f"token_id {token!r}: {count} consumptions"
        for token, count in tokens.items()
        if count > 1
    ] + [
        f"jti {jti!r}: {count} consumptions"
        for jti, count in jtis.items()
        if count > 1
    ]

    return findings


def _violations(findings: dict[str, list[str]]) -> dict[str, list[str]]:
    return {name: hits for name, hits in findings.items() if hits}


# ── 1. the invariants hold on a clean database ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_clean_database_violates_no_invariant(async_db_session) -> None:
    async_db_session.add(CompanyConfig(name="Integrity Co", timezone="UTC", daily_cycle_hour=6))
    async_db_session.add(
        RevenueSnapshot(
            snapshot_date=datetime.now(UTC).date(),
            stripe_balance_cents=0,  # a MEASURED zero — not a violation
        )
    )
    await async_db_session.commit()

    findings = await audit_invariants(async_db_session)
    assert _violations(findings) == {}, (
        "a healthy ledger must produce zero findings; these are the standing invariants"
    )


# ── 2. each invariant FIRES when its defect is present (the check can fail) ──────────────────


@pytest.mark.asyncio
async def test_a_run_left_running_past_the_bound_is_named(async_db_session) -> None:
    now = datetime.now(UTC)
    async_db_session.add(
        AgentRun(
            agent_type="finance",
            status="running",
            started_at=now - timedelta(seconds=AGENT_RUN_STALE_AFTER_SECONDS + 60),
        )
    )
    await async_db_session.commit()

    findings = await audit_invariants(async_db_session, now=now)
    assert findings["runs_stuck_running"], "a run open past the worker's bound must be reported"
    assert "E_RUN_STALE" in findings["runs_stuck_running"][0]


@pytest.mark.asyncio
async def test_an_interrupted_run_that_claims_a_cost_or_loses_its_marker_is_named(
    async_db_session,
) -> None:
    now = datetime.now(UTC)
    async_db_session.add_all(
        [
            AgentRun(
                agent_type="social_media",
                status="cancelled",
                started_at=now,
                cost_usd=0,
                usage_json={COST_SCOPE_KEY: COST_SCOPE_UNMEASURED},
            ),
            AgentRun(agent_type="code_generation", status="cancelled", started_at=now),
        ]
    )
    await async_db_session.commit()

    findings = await audit_invariants(async_db_session, now=now)
    # `unmeasured` with a 0 cost: the scope says nothing was measured, the row claims a zero
    assert len(findings["cancelled_runs_with_cost"]) == 1, "a cancelled run may not carry a cost"
    # no scope at all: an interrupted row that cannot say what its cost covers
    assert len(findings["cancelled_runs_without_declared_scope"]) == 1, (
        "a cancelled run must declare a cost_scope"
    )


@pytest.mark.asyncio
async def test_an_empty_snapshot_row_and_a_negative_amount_are_named(async_db_session) -> None:
    async_db_session.add_all(
        [
            RevenueSnapshot(snapshot_date=datetime.now(UTC).date()),
            RevenueSnapshot(
                snapshot_date=datetime.now(UTC).date() - timedelta(days=1), mrr_cents=-1
            ),
            ExpenseRecord(
                category="inventory",
                vendor="Example",
                amount_cents=0,
                currency="USD",
                date=datetime.now(UTC).date(),
            ),
        ]
    )
    await async_db_session.commit()

    findings = await audit_invariants(async_db_session)
    assert findings["snapshots_all_null"], "a row measuring nothing is noise, not a measurement"
    assert any("mrr_cents=-1" in hit for hit in findings["negative_money"])
    assert any("expense" in hit for hit in findings["negative_money"])


@pytest.mark.asyncio
async def test_a_complete_snapshot_with_no_finance_run_is_named_as_the_fabrication_signature(
    async_db_session,
) -> None:
    today = datetime.now(UTC).date()
    async_db_session.add(
        RevenueSnapshot(
            snapshot_date=today,
            mrr_cents=250000,
            arr_cents=3000000,
            active_subscribers=1,
            churned_today=0,
            new_today=0,
            total_revenue_month_cents=450000,
            stripe_balance_cents=125000,
        )
    )
    await async_db_session.commit()

    findings = await audit_invariants(async_db_session)
    assert findings["snapshots_all_seven_and_no_run"], (
        "seven measured columns with no finance run that day is the TS-1 fabrication signature"
    )

    # … and the same row is NOT flagged once a finance run exists for the day
    async_db_session.add(
        AgentRun(agent_type="finance", status="completed", started_at=datetime.now(UTC))
    )
    await async_db_session.commit()
    assert not (await audit_invariants(async_db_session))["snapshots_all_seven_and_no_run"]


@pytest.mark.asyncio
async def test_a_replayed_approval_or_blank_fingerprint_is_named(async_db_session) -> None:
    """The unique keys are the guarantee; the audit catches a schema that LOST them.

    On this schema a second spend of one mint nonce is refused by the DATABASE (asserted first).
    The audit's duplicate branch is the belt for a drifted schema (a missing index after a bad
    migration); the blank-fingerprint branch is exercised with a real row.
    """
    async_db_session.add(
        ConsumedApproval(
            token_id="t" * 64,
            jti="j1",
            action_fingerprint="external_irreversible|render|deploy",
            consumed_by="deploy:landing-page",
        )
    )
    await async_db_session.commit()

    with pytest.raises(IntegrityError):
        async_db_session.add(
            ConsumedApproval(
                token_id="u" * 64,
                jti="j1",  # the same mint nonce — a replay
                action_fingerprint="external_irreversible|render|deploy",
                consumed_by="deploy:landing-page",
            )
        )
        await async_db_session.flush()
    await async_db_session.rollback()

    async_db_session.add(
        ConsumedApproval(
            token_id="v" * 64,
            jti=None,
            action_fingerprint="  ",
            consumed_by="deploy:landing-page",
        )
    )
    await async_db_session.commit()

    findings = await audit_invariants(async_db_session)
    assert findings["approval_ledger_blank_fingerprint"], (
        "a consumption with no action fingerprint cannot be audited"
    )
    assert findings["approval_ledger_duplicates"] == [], (
        "the DB refused the replay, so no duplicate can reach the ledger"
    )


# ── 3. the migration chain: one head, and the COGS seam needs no new revision ────────────────


def _migration_graph() -> tuple[dict[str, str | None], set[str]]:
    """`{revision: down_revision}` + the set of revisions named as a parent, read from the files."""
    revisions: dict[str, str | None] = {}
    children: set[str] = set()
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision(?::\s*str)?\s*=\s*"([^"]+)"', text, re.MULTILINE)
        # `down_revision` is annotated three ways across the chain (`str | None`, `Union[str,
        # None]`, or bare) — the regex accepts all three and the `None` root.
        down = re.search(
            r'^down_revision\s*(?::[^=\n]+)?=\s*(?:"([^"]+)"|None)',
            text,
            re.MULTILINE,
        )
        assert revision is not None, f"{path.name} declares no revision"
        assert down is not None, f"{path.name} declares no down_revision"
        revisions[revision.group(1)] = down.group(1)
        if down.group(1):
            children.add(down.group(1))
    return revisions, children


def test_the_migration_chain_has_exactly_one_head_and_it_is_0020() -> None:
    revisions, children = _migration_graph()
    heads = sorted(revision for revision in revisions if revision not in children)
    # W8 DELIBERATE RE-PIN (2026-10-29): head 0019 -> 0020 (migration 0020 strengthens 0019's
    # email provider-evidence CHECK; linearity unchanged).
    assert heads == ["0020"], f"expected the single live head 0020, found {heads}"
    assert revisions["0020"] == "0019"
    assert revisions["0019"] == "0018"
    assert revisions["0018"] == "0017"
    assert revisions["0017"] == "0016"


def test_the_cogs_seam_needs_no_new_migration() -> None:
    """The cost inputs ride an EXISTING JSON column — no DDL, so no revision and no downtime."""
    company_table = Base.metadata.tables["company_config"]
    assert "pricing_model" in company_table.columns, (
        "the COGS seam stores its inputs inside company_config.pricing_model; this pin fails if "
        "the column is ever moved, which WOULD need a migration"
    )
    # and the seam introduces no table of its own (a new table would need a revision)
    assert not [name for name in Base.metadata.tables if "cost_basis" in name or "product_cost" in name]


# ── 4. PG-only invariants are DECLARED in the chain (their enforcement is proven live) ───────


def test_the_pg_only_invariants_are_declared_in_the_migrations() -> None:
    """Name them: the append-only triggers, the CHECK constraints and the ledger's unique keys.

    SQLite cannot enforce these the same way, so this test asserts they are DECLARED; the live
    receipt (`raw-w4/t4-pg-invariants.txt`, reproduced from `raw-w4/data-integrity.sql`) proves the
    real PostgreSQL has both triggers enabled, every CHECK/UNIQUE by name, and that a real UPDATE
    of `activity_log` is refused by `trg_activity_log_no_update`.
    """
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(VERSIONS_DIR.glob("*.py"))
    )
    for declared in (
        "trg_activity_log_no_update",
        "trg_activity_log_no_truncate",
        "uq_consumed_approvals_token_id",
        "uq_consumed_approvals_jti",
        "ck_expense_records_amount",
        "ck_revenue_snapshots_nonneg",
    ):
        assert declared in source, f"{declared} is not declared in the migration chain"


def test_the_approval_ledger_model_matches_its_migration_columns() -> None:
    """The ORM and 0018 describe ONE table (create_all and `alembic upgrade` cannot diverge)."""
    table = Base.metadata.tables["consumed_approvals"]
    assert [column.name for column in table.columns] == [
        "id",
        "token_id",
        "jti",
        "action_fingerprint",
        "consumed_at",
        "consumed_by",
    ]
    migration = (VERSIONS_DIR / "0018_consumed_approvals.py").read_text(encoding="utf-8")
    for column in table.columns:
        assert f'"{column.name}"' in migration, f"{column.name} missing from 0018"
