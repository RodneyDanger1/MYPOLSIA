"""V18/L1 — the five backend defects carried out of V17/L3, each closed RED-first.

Every test is a PIN named after the defect it closes; the section header states what the base
commit (27a353e, clean) did wrong and what the fix makes true.  Double boundaries are the ones
the repo's existing suites already use (the provider edge `integrations.send_email`, the
compliance footer, the sandbox flag); the daily-report interleaving is forced at the READ seam
that defines the TOCTOU window — the insert path under test is the production one.

| item | defect | pin |
|---|---|---|
| L3-05 | a no-op `{"enabled": true}` re-armed a pending due instant | `test_a_no_op_enable_*` |
| L3-09 | daily-report check-then-insert race | `test_daily_report_*` |
| L3-07 | one non-`PolsiaError` aborts the whole dunning tick | `test_one_invoice_raising_*` |
| L3-06 | `GET /memory?q=` skipped the documented `E_VALIDATION` | `test_empty_query_*` |
| L3-08 | ads helpers: delete-vs-DEBT assessment | `test_ads_helpers_*` |
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

DUE = date(2026, 11, 10)
LINK = "https://buy.stripe.com/test_v18l1"


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


# ── L3-05 · a no-op enable must not re-arm a pending due instant ──────────────────────────────


async def test_a_no_op_enable_leaves_a_pending_due_instant_untouched(async_db_session):
    """Base: `set(changes) & _SCHEDULE_SEMANTICS_FIELDS` includes `enabled`, so re-sending
    `{"enabled": true}` on an ALREADY-enabled row recomputed `next_run_at` from the wall clock
    and discarded an instant that was already due (today's run skipped).  A no-op save must
    leave the instant byte-identical."""
    from app.services.schedule_service import create_schedule, update_schedule

    db = async_db_session
    now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    row = await create_schedule(
        db, name="due already", prompt="do it", cadence="daily", hour=8, enabled=True
    )
    due = now - timedelta(minutes=5)
    row.next_run_at = due  # the tick has not claimed it yet: this instant is still owed
    await db.flush()

    updated = await update_schedule(db, row.id, {"enabled": True}, now=now)

    assert updated.next_run_at == due, "a no-op enable discarded the pending due instant"
    assert updated.next_run_at < now


async def test_a_no_op_enable_does_not_rearm_a_consumed_once(async_db_session):
    """The same no-op rule on a spent `once` row (enabled, `next_run_at` cleared by the tick):
    re-sending `enabled: true` must not fabricate a new occurrence — the F4 rule and the L3-05
    no-op rule agree here, and both disagree with the base behavior (which re-armed)."""
    from app.services.schedule_service import create_schedule, update_schedule

    db = async_db_session
    row = await create_schedule(
        db, name="spent", prompt="once", cadence="once", hour=6, enabled=True
    )
    row.next_run_at = None  # exactly what claim_due_schedules writes for a spent once
    await db.flush()

    updated = await update_schedule(
        db, row.id, {"enabled": True}, now=datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    )

    assert updated.next_run_at is None, "a no-op enable re-armed a consumed one-shot"


async def test_enabling_a_paused_row_still_recomputes_from_the_wall_clock(async_db_session):
    """The other leg of the pause rule (docstring `schedule_service.py:19-21`): a REAL
    false->true transition arms from the caller's wall clock, strictly after `now`."""
    from app.core.timeutil import company_tz
    from app.services.schedule_service import compute_next_run, create_schedule, update_schedule

    db = async_db_session
    now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    row = await create_schedule(
        db, name="paused", prompt="later", cadence="daily", hour=8, enabled=False
    )
    assert row.next_run_at is None

    updated = await update_schedule(db, row.id, {"enabled": True}, now=now)

    assert updated.enabled is True
    assert updated.next_run_at == compute_next_run(
        updated.cadence, updated.hour, updated.weekday, now=now, tz=company_tz()
    )
    assert updated.next_run_at > now


# ── L3-09 · the daily report's check-then-insert must survive a lost race ─────────────────────


def test_daily_report_double_insert_is_refused_by_the_database(queue_db):
    """The DB-level guarantee the recovery relies on: `daily_reports.report_date` is UNIQUE
    (model + `0001_initial_schema.py:249`), so a second row for the same day cannot exist."""
    from app.models.report import DailyReport

    async def _go() -> None:
        async with queue_db.factory() as db:
            db.add(DailyReport(report_date=DUE))
            await db.flush()
            await db.commit()

    queue_db.run(_go())

    async def _second() -> None:
        from app.models.report import DailyReport as Model

        async with queue_db.factory() as db:
            db.add(Model(report_date=DUE))
            with pytest.raises(IntegrityError):
                await db.flush()
            await db.rollback()

    queue_db.run(_second())
    assert queue_db.count("daily_reports", "report_date = ?", (DUE.isoformat(),)) == 1


def test_get_or_create_recovers_when_two_writers_pass_the_same_check(queue_db, monkeypatch):
    """Base: both writers read `None`, the winner inserts, and the loser's `flush()` raises
    IntegrityError out of the task.  The fix wraps the insert in a savepoint and re-reads the
    winner's row.  The interleaving is forced at the existence READ (returning `None` exactly
    once reproduces "this writer's check ran before the winner committed"); the rest is real."""
    from app.services import report_service

    async def _go() -> tuple[int, int, int]:
        async with queue_db.factory() as winner_db:
            winner = await report_service.get_or_create_daily_report(winner_db, DUE)
            await winner_db.commit()
            winner_id = int(winner.id)

        real_read = report_service.get_daily_report
        seen = {"n": 0}

        async def read_as_if_before_the_commit(db, report_date):
            seen["n"] += 1
            if seen["n"] == 1:
                return None  # this writer's check: the row did not exist yet
            return await real_read(db, report_date)

        monkeypatch.setattr(report_service, "get_daily_report", read_as_if_before_the_commit)
        async with queue_db.factory() as loser_db:
            got = await report_service.get_or_create_daily_report(loser_db, DUE)
            await loser_db.commit()
            return winner_id, int(got.id), seen["n"]

    winner_id, got_id, reads = queue_db.run(_go())

    assert got_id == winner_id, "the losing writer must answer with the winner's row"
    assert reads >= 2, "the recovery must re-read the row the DB refused to duplicate"
    assert queue_db.count("daily_reports", "report_date = ?", (DUE.isoformat(),)) == 1


# ── L3-07 · one uncoded exception must not abort the dunning ladder ───────────────────────────


def _seed_invoice(queue_db, *, number: str) -> int:
    from app.models.invoice import Invoice, InvoiceStatus

    async def _go() -> int:
        async with queue_db.factory() as db:
            invoice = Invoice(
                number=number,
                status=InvoiceStatus.SENT,
                issue_date=DUE - timedelta(days=30),
                due_date=DUE,
                net_days=30,
                currency="USD",
                total=Decimal("250.00"),
                payment_link=LINK,
                bill_to={"name": "ACME", "email": "billing@acme.example"},
            )
            db.add(invoice)
            await db.flush()
            invoice_id = int(invoice.id)
            await db.commit()
            return invoice_id

    return queue_db.run(_go())


@pytest.fixture
def dunning_mail(monkeypatch):
    """The provider edge only (the V9 suite's `mail_seam` boundary).  `explode_on` makes the
    send raise a NON-`PolsiaError`, the exact class `_dun_one` did not catch."""
    from app.agents import integrations
    from app.config import settings
    from app.services import invoice_service

    monkeypatch.setattr(settings, "sandbox_mode", False)

    async def footer(db) -> str:
        return "ACME LLC · 1 Main St, Testville · unsubscribe: reply STOP\nAI assistance disclosure: on."

    monkeypatch.setattr(invoice_service, "_compliance_footer", footer)

    calls: list[dict] = []
    state = {"explode_on": None}

    def fake_send(*, to: str, subject: str, body: str) -> dict:
        calls.append({"to": to, "subject": subject, "body": body})
        if state["explode_on"] and state["explode_on"] in subject:
            raise RuntimeError("transport exploded mid-handshake")
        return {"ok": True, "result": {"status_code": 202, "message_id": "sg-v18-l1"}}

    monkeypatch.setattr(integrations, "send_email", fake_send)
    return calls, state


def test_one_invoice_raising_an_uncoded_error_does_not_abort_the_tick(queue_db, mock_redis, dunning_mail):
    from celery_app.tasks import dunning

    calls, state = dunning_mail
    first_id = _seed_invoice(queue_db, number="INV-2026-801")
    second_id = _seed_invoice(queue_db, number="INV-2026-802")
    state["explode_on"] = "INV-2026-801"

    result = queue_db.run(dunning._run_dunning(now=_at(DUE)))

    # the tick finished and processed the NEXT invoice (base: RuntimeError escaped the tick)
    assert result["status"] == "completed"
    assert [entry["invoice_id"] for entry in result["sent"]] == [second_id]
    # the named failure for THAT step, with the exception class in the message
    (failure,) = result["errors"]
    assert failure["invoice_id"] == first_id
    assert failure["step"] == "due"
    assert failure["code"] == dunning.DUNNING_STEP_FAILED_CODE
    assert "RuntimeError" in failure["message"]
    # nothing was claimed as delivered for the failed invoice; its claim stays PENDING
    assert queue_db.count("activity_log", "action = 'invoice_dunning' AND detail LIKE ?", (f'%"invoice_id": {first_id}%',)) == 0
    claim = queue_db.query(
        "SELECT detail FROM activity_log WHERE action = 'invoice_dunning_claim' AND detail LIKE ?",
        (f'%"invoice_id": {first_id}%',),
    )
    assert len(claim) == 1 and json.loads(claim[0]["detail"])["state"] == "pending"
    # a durable named-failure row exists (never silent)
    errors = queue_db.query("SELECT detail FROM activity_log WHERE action = 'invoice_dunning_error'")
    assert len(errors) == 1 and json.loads(errors[0]["detail"])["invoice_id"] == first_id

    # the NEXT tick: the pending claim consumes the step (at-most-once), the sent invoice's
    # ledger does too -> nothing re-sends, and the crash window is named.
    again = queue_db.run(dunning._run_dunning(now=_at(DUE)))
    assert again["sent"] == [] and again["refused"] == []
    assert [c["invoice_id"] for c in again["unfinalized_claims"]] == [first_id]
    assert len(calls) == 2, "one provider call per invoice, never a retry of the failed step"


# ── L3-06 · `GET /memory?q=` must honour the documented empty-query refusal ───────────────────


async def test_empty_query_is_the_named_validation_refusal(api_client, auth_headers):
    """The module docstring (`memory.py:3-7`) names `E_VALIDATION` on an empty query, and
    `memory_service.search_memory` raises exactly that — but the route's falsy `if q:` sent
    `q=""` down the UNFILTERED list branch.  A supplied-but-empty query is a refusal by name;
    only an ABSENT `q` is the plain list."""
    resp = await api_client.get("/api/v1/memory?q=", headers=auth_headers)

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_VALIDATION"
    assert resp.headers.get("X-Polsia-Error-Code") == "E_VALIDATION"


async def test_absent_query_is_still_the_plain_unfiltered_list(api_client, auth_headers):
    """The complement: no `q` parameter at all is the documented list, not a refusal."""
    resp = await api_client.get("/api/v1/memory", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json() == []


# ── L3-08 · ads helpers: assessment pin (DEBT, not an unreferenced deletion) ─────────────────

BACKEND = __file__.rsplit("tests", 1)[0]


def test_ads_helpers_are_reachable_from_production_paths():
    """The V17 card said "zero importers outside their own module/tests".  Measured at HEAD:
    `create_campaign` IS the writer `record_ads_run` calls, and `record_ads_run` is imported by
    the queue worker — so it is load-bearing, not dead.  `get_campaign` serves `list_metrics`.
    `list_metrics` is the only helper with no production consumer (the route reads
    `list_campaigns`) -> DEBT documented in the V18 L1 receipt; deleting it would be a product
    call, not dead-code removal."""
    import inspect

    from app.services import ads_service

    source = inspect.getsource(ads_service)
    assert "created = await create_campaign(" in source, "record_ads_run's writer"
    assert "if await get_campaign(db, campaign_id) is None:" in source, "list_metrics' guard"

    agent_tasks = (BACKEND + "celery_app/tasks/agent_tasks.py").replace("\\", "/")
    with open(agent_tasks, encoding="utf-8") as handle:
        worker = handle.read()
    assert "from app.services.ads_service import record_ads_run" in worker

    with open(BACKEND + "app/api/v1/ads.py", encoding="utf-8") as handle:
        route = handle.read()
    assert "from app.services.ads_service import list_campaigns" in route
    assert "list_metrics" not in route, "if this changes, update the DEBT note in the receipt"
