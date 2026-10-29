"""WAVE 8 — data truth under failure: the email send ledger, the invoice refusal path, the
persisted run-context, and the migration-enforced evidence rule.

WHY THIS FILE EXISTS.  Wave 7 closed the email lifecycle's fabrication paths at the WRITER level
(the agent claims `sent` only on a provider 2xx/id; the writer refuses a contradictory claim; the
DB refuses `sent` without a provider id).  Wave 8 asks the next question: what happens when the
path FAILS, and what can a path that BYPASSES the services still do?  Four measured answers live
here:

1. **0019's DB rule was bypassable by direct SQL** (live probes, 2026-10-29): `sendgrid_id=''`
   satisfied `IS NOT NULL`, and a `sent` row needed no `sent_at`.  Migration **0020** strengthens the
   SAME constraint to `sendgrid_id IS NOT NULL AND trim(sendgrid_id) <> '' AND sent_at IS NOT NULL`
   on PostgreSQL, and `models/outreach.py` renders the identical predicate in `create_all` so the
   SQLite test engine enforces it too.  The first group of tests is that rule, in both directions
   (a fabricated row refused; a real one accepted; a draft needing no evidence).
2. **The writer commits nothing** — a run's send rows must be discarded when its transaction rolls
   back ("one commit per run": the worker owns the commit).  A writer that committed internally
   would leave a ledger row for a run whose own settlement failed.
3. **A provider refusal leaves the invoice exactly as it was** — no `sent` flip, no `invoice_sent`
   audit row, no row count change.  A refusal is not a partial success.
4. **Third-party consent addresses are not persisted** — the dispatch-time suppression list (the
   addresses of every unsubscribed prospect) stays in the run's in-memory context only; the stored
   `agent_runs.input_context` carries a COUNT, so the export member that ships `input_context` no
   longer leaks addresses around the manifest's "prospects not included" declaration.  The
   persisted shape also fails CLOSED if anyone tries to replay it as a live preflight.

Every check mirrors a live audit: `truthseeker/raw-w8/w8-failure-mode-audit.sql` (FM1..FM15) and
`w8-integrity-13.txt`.  No network, no provider, no credential.
"""
from __future__ import annotations

import io
import json
import pathlib
import sqlite3
import zipfile
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.database import utcnow
from app.core.errors import E_CONFIG, E_INTEGRATION, MeridianError
from app.models.outreach import EmailLog, Prospect
from app.models.report import ActivityLog
from app.models.task import AgentRun
from app.services.channel_service import channel_sends_snapshot
from app.services.outreach_service import record_sent_emails
from app.services.task_service import COMPLIANCE_CONTEXT_KEY, create_agent_run

BACKEND = pathlib.Path(__file__).resolve().parents[2]


# ── helpers ───────────────────────────────────────────────────────────────────────────────────


async def _count(db, model) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


def _create_all_ddl(tmp_path: pathlib.Path) -> list[tuple[str, str, str]]:
    """> every object's (type, name, sql) from a real `create_all` on SQLite — the test engine's
    schema, not a hand-typed string."""
    from sqlalchemy import create_engine

    import app.models  # noqa: F401 — registers every table
    from app.core.database import Base

    db_path = tmp_path / "w8_ddl.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    con = sqlite3.connect(db_path.as_posix())
    try:
        return con.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master ORDER BY name"
        ).fetchall()
    finally:
        con.close()


def _email_logs_ddl(rows: list[tuple[str, str, str]]) -> str:
    matches = [sql for kind, name, sql in rows if kind == "table" and name == "email_logs"]
    assert matches, "create_all did not render an email_logs table"
    return matches[0]


async def _add_prospect(db, address: str = "w8@example.com") -> Prospect:
    prospect = Prospect(email=address)
    db.add(prospect)
    await db.flush()
    return prospect


async def _insert_email_log(db, **values):
    """One raw INSERT into email_logs — the DIRECT path a service cannot intercept."""
    columns = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    return await db.execute(
        text(f"INSERT INTO email_logs ({columns}) VALUES ({params})"), values
    )


# ── 1. migration 0020: the evidence rule, on the test engine ──────────────────────────────────


def test_the_email_log_constraint_is_the_strengthened_predicate(tmp_path) -> None:
    """The create_all DDL carries 0020's rule by NAME and by PREDICATE (both engines)."""
    ddl = _email_logs_ddl(_create_all_ddl(tmp_path))
    assert "ck_email_logs_sent_requires_provider_id" in ddl
    assert "trim(sendgrid_id) <> ''" in ddl, ddl
    assert "sent_at IS NOT NULL" in ddl, ddl
    # `btrim` is PostgreSQL-only; the shared model must stay portable, so the predicate uses
    # `trim` (which both engines implement).
    assert "btrim" not in ddl


@pytest.mark.asyncio
async def test_a_sent_row_requires_a_non_blank_provider_id(async_db_session) -> None:
    """`''`, whitespace and NULL are ALL 'no provider id' (0019 accepted the empty string)."""
    db = async_db_session
    prospect = await _add_prospect(db)
    for bad in ("", "   ", None):
        async with db.begin_nested():
            with pytest.raises(IntegrityError):
                await _insert_email_log(
                    db,
                    prospect_id=prospect.id,
                    subject="w8",
                    sendgrid_id=bad,
                    status="sent",
                    sent_at=datetime.now(UTC),
                )
    assert await _count(db, EmailLog) == 0, "no fabricated send may survive"


@pytest.mark.asyncio
async def test_a_sent_row_requires_the_instant_it_was_recorded(async_db_session) -> None:
    """A send with no `sent_at` is invisible to the email cap's window — the DB refuses it."""
    db = async_db_session
    prospect = await _add_prospect(db)
    async with db.begin_nested():
        with pytest.raises(IntegrityError):
            await _insert_email_log(
                db,
                prospect_id=prospect.id,
                subject="w8",
                sendgrid_id="sg-w8-no-instant",
                status="sent",
                sent_at=None,
            )
    assert await _count(db, EmailLog) == 0


@pytest.mark.asyncio
async def test_a_fully_evidenced_send_is_accepted_and_a_draft_needs_no_evidence(
    async_db_session,
) -> None:
    """The rule only refuses a CLAIMED send: drafts (0019's default) still need nothing."""
    db = async_db_session
    prospect = await _add_prospect(db)
    await _insert_email_log(
        db,
        prospect_id=prospect.id,
        subject="real",
        sendgrid_id="sg-w8-good",
        status="sent",
        sent_at=datetime.now(UTC),
    )
    await _insert_email_log(db, prospect_id=prospect.id, subject="draft")  # no status/id/time
    await db.flush()
    rows = (await db.execute(select(EmailLog).order_by(EmailLog.id))).scalars().all()
    assert [row.status for row in rows] == ["sent", "draft"], (
        "0019's `draft` default must still hold for a row that states no status"
    )
    assert rows[1].sendgrid_id is None and rows[1].sent_at is None


@pytest.mark.asyncio
async def test_a_direct_insert_cannot_move_the_email_cap(async_db_session) -> None:
    """The cap reads `status='sent'` rows inside a `sent_at` window — only evidence can move it."""
    db = async_db_session
    await _add_prospect(db)
    before = await channel_sends_snapshot(db, ["email"], now=utcnow())
    assert before == {"email": {"today": 0, "hour": 0}}

    async with db.begin_nested():
        with pytest.raises(IntegrityError):
            await _insert_email_log(
                db,
                prospect_id=(await db.execute(select(Prospect.id))).scalar_one(),
                subject="fake",
                sendgrid_id="",
                status="sent",
                sent_at=utcnow(),
            )

    after = await channel_sends_snapshot(db, ["email"], now=utcnow())
    assert after == {"email": {"today": 0, "hour": 0}}, (
        "a refused direct INSERT must leave the cap where it was (measured, not assumed)"
    )


# ── 2. one commit per run: the send writer commits nothing ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_send_row_is_discarded_when_the_run_transaction_rolls_back(async_db_session) -> None:
    """`record_sent_emails` FLUSHES only — the worker owns the commit.

    A writer that committed internally would leave an `email_logs` row behind after the run's own
    settlement transaction failed, which is exactly the "the ledger says the send happened, the run
    says it never finished" split this wave audits for.
    """
    db = async_db_session
    result = {
        "emails": [
            {
                "prospect_email": "rollback@example.com",
                "subject": "hello",
                "body": "body",
                "status": "sent",
                "sendgrid_id": "sg-w8-rollback",
                "provider_status": 202,
            }
        ]
    }
    report = await record_sent_emails(db, result)
    assert report["ok"] is True and report["emails_written"] == 1
    assert await _count(db, EmailLog) == 1, "the row must be visible INSIDE the open transaction"

    await db.rollback()

    assert await _count(db, EmailLog) == 0, (
        "the send writer committed its own row: a rolled-back run left evidence it never wrote"
    )
    assert await _count(db, Prospect) == 0, "the prospect created for the send must roll back too"


@pytest.mark.asyncio
async def test_a_rejected_provider_answer_writes_nothing_even_directly(async_db_session) -> None:
    """The writer's acceptance gate, restated as a ledger invariant (wave-7 RED-B)."""
    db = async_db_session
    report = await record_sent_emails(
        db,
        {
            "emails": [
                {
                    "prospect_email": "rejected@example.com",
                    "subject": "hello",
                    "status": "sent",
                    "sendgrid_id": "sg-w8-rejected",
                    "provider_status": 400,
                }
            ]
        },
    )
    assert report["ok"] is False
    assert report["emails_written"] == 0
    assert report["rejected_provider_status"][0]["provider_status"] == 400
    await db.flush()
    assert await _count(db, EmailLog) == 0
    assert await _count(db, Prospect) == 0, "a rejected send may not even create the prospect"


# ── 3. a refusal leaves the invoice untouched ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_provider_refusal_leaves_the_invoice_exactly_as_it_was(
    async_db_session, monkeypatch, mock_redis
) -> None:
    """The invoice stays a DRAFT, no `invoice_sent` ledger row exists, no row count moved."""
    from app.agents import integrations
    from app.config import settings
    from app.models.company import CompanyConfig
    from app.models.invoice import Invoice, InvoiceStatus
    from app.services import invoice_service

    db = async_db_session
    monkeypatch.setattr(settings, "sandbox_mode", False)
    db.add(
        CompanyConfig(
            name="ACME",
            sender_legal_name="ACME LLC",
            sender_postal_address="1 Main St, Testville",
            website_url="https://acme.example",
        )
    )
    await db.flush()
    calls: list[dict] = []

    def refusing_send(*, to: str, subject: str, body: str) -> dict:
        calls.append({"to": to, "subject": subject, "body": body})
        return {"ok": False, "error": {"code": "E_INTEGRATION", "message": "provider 401"}}

    monkeypatch.setattr(integrations, "send_email", refusing_send)

    invoice = await invoice_service.create_invoice(
        db,
        issue_date=datetime.now(UTC).date(),
        bill_to={"name": "ACME", "email": "billing@acme.example"},
        payment_link="https://buy.stripe.com/test_w8",
        total=Decimal("12.00"),
    )
    await db.commit()
    invoice_id = invoice.id

    with pytest.raises(MeridianError) as excinfo:
        await invoice_service.send_invoice(db, invoice_id)
    assert excinfo.value.code == E_INTEGRATION
    assert len(calls) == 1, "the provider WAS reached; the refusal is its answer"

    fresh = await db.get(Invoice, invoice_id)
    assert fresh is not None
    assert fresh.status is InvoiceStatus.DRAFT, "a refused send must not flip the invoice"
    sent_rows = (
        await db.execute(select(ActivityLog).where(ActivityLog.action == "invoice_sent"))
    ).scalars().all()
    assert sent_rows == [], "no invoice_sent ledger row may exist for a refused send"
    assert await _count(db, Invoice) == 1


# ── 4. third-party consent state is not persisted or exported ────────────────────────────────


def _run_context_with_suppression() -> tuple[dict, str]:
    address = "opted-out-person@example.com"
    context = {
        "company": {"id": 1, "name": "ACME"},
        COMPLIANCE_CONTEXT_KEY: {
            "send_time_verified": True,
            "company": {
                "sender_legal_name": "ACME LLC",
                "sender_postal_address": "1 Main St, Testville",
                "website_url": "https://acme.example",
            },
            "suppressed": [address],
        },
    }
    return context, address


def test_the_persisted_context_keeps_the_count_not_the_addresses() -> None:
    """The sanitizer is pure, copy-on-write, and redacts ONLY the consent list."""
    from app.services.task_service import sanitize_persisted_context

    context, address = _run_context_with_suppression()
    persisted = sanitize_persisted_context(context)

    assert persisted is not context
    assert persisted[COMPLIANCE_CONTEXT_KEY] is not context[COMPLIANCE_CONTEXT_KEY]
    block = persisted[COMPLIANCE_CONTEXT_KEY]
    assert block["suppressed"] == {
        "redacted": (
            "third-party consent addresses are not persisted: the dispatch-time suppression "
            "list lives in the run's in-memory context only"
        ),
        "count": 1,
    }
    assert address not in json.dumps(persisted)
    assert block["send_time_verified"] is True
    # the CALLER's context (what the agent receives) still carries the real list
    assert context[COMPLIANCE_CONTEXT_KEY]["suppressed"] == [address]


def test_the_sanitizer_leaves_every_other_context_untouched() -> None:
    from app.services.task_service import sanitize_persisted_context

    plain = {"company": {"id": 1}, "kpis": {"mrr": 0}}
    assert sanitize_persisted_context(plain) == plain
    # a non-list suppression value is not a list to redact; nothing is invented
    weird = {COMPLIANCE_CONTEXT_KEY: {"suppressed": "not-a-list"}}
    assert sanitize_persisted_context(weird) == weird
    # absent / non-dict contexts survive
    assert sanitize_persisted_context(None) is None
    assert sanitize_persisted_context({}) == {}


def test_the_context_key_spelling_matches_the_agent_seam() -> None:
    """The producer's literal and this module's literal cannot drift silently."""
    from app.agents.email_outreach.compliance_send import PREFLIGHT_KEY

    assert COMPLIANCE_CONTEXT_KEY == PREFLIGHT_KEY == "compliance"
    from app.services.task_service import SUPPRESSION_CONTEXT_KEY

    assert SUPPRESSION_CONTEXT_KEY == "suppressed"


@pytest.mark.asyncio
async def test_create_agent_run_persists_the_count_not_the_addresses(async_db_session) -> None:
    db = async_db_session
    context, address = _run_context_with_suppression()
    run = await create_agent_run(db, "email_outreach", task_id=None, input_context=context)
    await db.commit()

    stored = await db.get(AgentRun, run.id)
    assert stored is not None
    blob = json.dumps(stored.input_context)
    assert address not in blob, "a consent address was persisted into agent_runs.input_context"
    stored_block = stored.input_context[COMPLIANCE_CONTEXT_KEY]
    assert stored_block["suppressed"]["count"] == 1
    # the live context the agent gets is unchanged by the write
    assert context[COMPLIANCE_CONTEXT_KEY]["suppressed"] == [address]


@pytest.mark.asyncio
async def test_the_persisted_preflight_cannot_be_replayed_as_a_live_one(
    async_db_session, monkeypatch
) -> None:
    """A stored preflight's redacted list must fail CLOSED, never read as "nothing suppressed"."""
    from app.agents import integrations
    from app.config import settings
    from app.services.email_compliance import send_compliant_email
    from app.services.task_service import sanitize_persisted_context

    monkeypatch.setattr(settings, "approval_secret", "w8-test-secret")
    called: list[dict] = []
    monkeypatch.setattr(
        integrations,
        "send_email",
        lambda **kwargs: called.append(kwargs) or {"ok": True, "result": {"status_code": 202}},
    )

    context, _address = _run_context_with_suppression()
    stored_block = dict(sanitize_persisted_context(context)[COMPLIANCE_CONTEXT_KEY])
    with pytest.raises(MeridianError) as excinfo:
        await send_compliant_email(
            None,
            to="someone@example.com",
            subject="hi",
            body="body",
            preflight=stored_block,
        )
    assert excinfo.value.code == E_CONFIG, (
        "a persisted (redacted) suppression block must be refused by the shape floor"
    )
    assert called == [], "the provider must never be reached with an unreadable suppression list"


@pytest.mark.asyncio
async def test_the_data_export_carries_no_suppression_address(
    async_db_session, api_client, auth_headers
) -> None:
    """The export member that ships `input_context` must not carry the consent list either."""
    db = async_db_session
    context, address = _run_context_with_suppression()
    await create_agent_run(db, "email_outreach", task_id=None, input_context=context)
    await db.commit()

    response = await api_client.get("/api/v1/data/export", headers=auth_headers)
    assert response.status_code == 200, response.text
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    # `agent_runs` is a CSV member (the manifest's split), so the input_context cell is
    # JSON-encoded inside one quoted CSV cell.
    assert "agent_runs.csv" in names
    member = archive.read("agent_runs.csv").decode("utf-8")
    assert address not in member, "the export shipped a third-party consent address"
    assert "redacted" in member and "count" in member, "the consent COUNT should survive as provenance"
    # belt-and-braces: the address appears in NO member of the archive
    for name in names:
        if name == "MANIFEST.json":
            continue
        assert address not in archive.read(name).decode("utf-8", errors="replace"), name


# ── 5. the live migration chain carries the rule to a fresh SQLite database ──────────────────


def _sqlite_chain(tmp_path, monkeypatch):
    from alembic import config as alembic_config
    from app.config import settings

    db_path = tmp_path / "w8_chain.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, db_path


def test_the_migration_chain_installs_and_reverses_the_evidence_rule(tmp_path, monkeypatch) -> None:
    """upgrade head → 0020 with the CHECK in the table DDL; downgrade 0019 → gone; re-upgrade → back."""
    from alembic import command

    cfg, db_path = _sqlite_chain(tmp_path, monkeypatch)
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0020"
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='email_logs'"
        ).fetchone()[0]
        assert "ck_email_logs_sent_requires_provider_id" in ddl
        assert "trim(sendgrid_id) <> ''" in ddl and "sent_at IS NOT NULL" in ddl
        assert sorted(
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='email_logs' "
                "AND sql IS NOT NULL"
            )
        ) == ["ix_email_logs_follow_up_due", "ix_email_logs_prospect_sent_at",
              "uq_email_logs_sendgrid_id"]
        con.execute("INSERT INTO prospects (email, status) VALUES ('chain@example.com', 'new')")
        pid = con.execute("SELECT id FROM prospects").fetchone()[0]
        for bad_id, bad_at in (("", "2026-10-29 00:00:00"), ("sg-x", None), (None, "2026-10-29")):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO email_logs (prospect_id, subject, sendgrid_id, status, sent_at) "
                    "VALUES (?, 'p', ?, 'sent', ?)",
                    (pid, bad_id, bad_at),
                )
        con.execute(
            "INSERT INTO email_logs (prospect_id, subject, sendgrid_id, status, sent_at) "
            "VALUES (?, 'p', 'sg-chain-good', 'sent', '2026-10-29 00:00:00')",
            (pid,),
        )
    finally:
        con.close()

    command.downgrade(cfg, "0019")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0019"
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='email_logs'"
        ).fetchone()[0]
        assert "ck_email_logs_sent_requires_provider_id" not in ddl, (
            "the downgrade must restore 0019's SQLite shape (no CHECK in the table definition)"
        )
    finally:
        con.close()

    command.upgrade(cfg, "head")
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0020"
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='email_logs'"
        ).fetchone()[0]
        assert "ck_email_logs_sent_requires_provider_id" in ddl
    finally:
        con.close()


# ── 5b. the same revision rendered offline (`--sql`, no connection) ───────────────────────────


_PG_OFFLINE_URL = "postgresql+asyncpg://meridian:meridian_password@localhost:5432/meridian"


def _offline_cfg(url: str):
    from alembic import config as alembic_config

    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_the_evidence_rule_renders_as_offline_sql(monkeypatch, capsys) -> None:
    """`alembic upgrade 0019:0020 --sql` must RENDER the strengthened ALTER, not crash.

    WAVE 8 measured defect: the first draft ran the legacy-row pre-check on every PostgreSQL
    upgrade, and in `--sql` mode `op.get_bind()` is a MockConnection with no `exec_driver_sql`
    — the offline render died with `AttributeError: 'MockConnection' object has no attribute
    'exec_driver_sql'`, so a deployment could not produce the SQL for this revision at all.
    Offline mode cannot read data: it must skip the pre-check BY NAME and emit the ALTERs.
    """
    from alembic import command
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", _PG_OFFLINE_URL)
    command.upgrade(_offline_cfg(_PG_OFFLINE_URL), "0019:0020", sql=True)
    out = capsys.readouterr().out
    assert "the legacy-row pre-check cannot run without a connection" in out, (
        "offline mode must SAY the data pre-check was skipped, not silently skip it"
    )
    assert "ALTER TABLE email_logs DROP CONSTRAINT IF EXISTS" in out
    assert (
        "ALTER TABLE email_logs ADD CONSTRAINT ck_email_logs_sent_requires_provider_id "
        "CHECK (status <> 'sent' OR (sendgrid_id IS NOT NULL AND trim(sendgrid_id) <> '' "
        "AND sent_at IS NOT NULL))"
    ) in out, "the rendered upgrade must carry 0020's strengthened predicate verbatim"
    assert "UPDATE alembic_version SET version_num='0020'" in out


def test_the_evidence_rule_reverses_as_offline_sql(monkeypatch, capsys) -> None:
    """`alembic downgrade 0020:0019 --sql` must render 0019's predicate back — no data read."""
    from alembic import command
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", _PG_OFFLINE_URL)
    command.downgrade(_offline_cfg(_PG_OFFLINE_URL), "0020:0019", sql=True)
    out = capsys.readouterr().out
    assert (
        "ALTER TABLE email_logs ADD CONSTRAINT ck_email_logs_sent_requires_provider_id "
        "CHECK (status <> 'sent' OR sendgrid_id IS NOT NULL)"
    ) in out
    assert "trim(sendgrid_id)" not in out, "the downgrade must restore 0019, not keep 0020"
    assert "UPDATE alembic_version SET version_num='0019'" in out


def test_the_sqlite_leg_refuses_offline_by_name(tmp_path, monkeypatch) -> None:
    """The SQLite leg reads the LIVE `sqlite_master` text; `--sql` must refuse by name."""
    from alembic import command

    cfg, _db_path = _sqlite_chain(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="cannot run in --sql"):
        command.upgrade(cfg, "0019:0020", sql=True)


# ── 6. the audit's own arithmetic (the live SQL's SQLite twins) ──────────────────────────────


@pytest.mark.asyncio
async def test_a_finished_run_that_measured_tokens_but_no_cost_is_visible(async_db_session) -> None:
    """FM7/FM15: the spend floor's denominator is a COUNT of unpriced finished runs.

    The floor must be computable from the ledger alone: a finished run with no `cost_usd` is
    "unmeasured" (NULL), never 0 — and the count is what makes the day's total honest.
    """
    db = async_db_session
    now = datetime.now(UTC)
    db.add_all(
        [
            AgentRun(agent_type="finance", status="completed", cost_usd=Decimal("0.5000"),
                     started_at=now, ended_at=now, tokens_input=10, tokens_output=5),
            AgentRun(agent_type="email_outreach", status="completed", cost_usd=None,
                     started_at=now, ended_at=now, tokens_input=10, tokens_output=5),
            AgentRun(agent_type="email_outreach", status="failed", cost_usd=None,
                     started_at=now, ended_at=now),
        ]
    )
    await db.flush()
    priced = int(
        (
            await db.execute(
                select(func.count()).select_from(AgentRun).where(AgentRun.cost_usd.is_not(None))
            )
        ).scalar_one()
    )
    unpriced = int(
        (
            await db.execute(
                select(func.count()).select_from(AgentRun).where(AgentRun.cost_usd.is_(None))
            )
        ).scalar_one()
    )
    measured = (
        await db.execute(select(func.coalesce(func.sum(AgentRun.cost_usd), 0)))
    ).scalar_one()
    assert (priced, unpriced) == (1, 2)
    assert Decimal(str(measured)) == Decimal("0.5000"), (
        "the measured floor is the SUM of priced rows only — never a total for the day"
    )
    tokens_but_no_cost = int(
        (
            await db.execute(
                select(func.count())
                .select_from(AgentRun)
                .where(
                    AgentRun.cost_usd.is_(None),
                    (func.coalesce(AgentRun.tokens_input, 0)
                     + func.coalesce(AgentRun.tokens_output, 0)) > 0,
                )
            )
        ).scalar_one()
    )
    assert tokens_but_no_cost == 1, "FM7 must see a measured token count with no cost"


@pytest.mark.asyncio
async def test_a_failed_run_keeps_its_reason_and_never_looks_completed(async_db_session) -> None:
    """FM5: a failed run's reason is readable; a completed run has no failure text."""
    db = async_db_session
    now = datetime.now(UTC)
    failed = AgentRun(agent_type="email_outreach", status="failed", started_at=now, ended_at=now,
                      raw_log="E_INTEGRATION: provider 500")
    completed = AgentRun(agent_type="finance", status="completed", started_at=now, ended_at=now)
    db.add_all([failed, completed])
    await db.flush()
    rows = (await db.execute(select(AgentRun).order_by(AgentRun.id))).scalars().all()
    assert rows[0].status == "failed" and rows[0].raw_log
    assert rows[1].status == "completed" and not rows[1].raw_log


@pytest.mark.asyncio
async def test_the_rollback_window_does_not_create_an_orphan_task_claim(async_db_session) -> None:
    """A rolled-back artifact write leaves no prospect/email row behind for a later audit to find."""
    db = async_db_session
    prospect = await _add_prospect(db, "orphan@example.com")
    await _insert_email_log(
        db,
        prospect_id=prospect.id,
        subject="orphan",
        sendgrid_id="sg-w8-orphan",
        status="sent",
        sent_at=utcnow(),
    )
    await db.rollback()
    assert await _count(db, EmailLog) == 0
    assert await _count(db, Prospect) == 0
    # the audit's FM8 ("a sent row no run accounts for") therefore cannot fire on a rolled-back write
    assert await _count(db, AgentRun) == 0
