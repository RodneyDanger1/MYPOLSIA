"""Frozen schema contract for `app.models` — the in-repo guard for the SQLAlchemy 2.0 migration.

Every value below is pinned to the ORM `create_all` output (the D-models receipt's
`probes/ddl_before.columns.tsv`: 17 tables / 182 columns / 6 unique constraints / 6 explicit
indexes). The `Column(...)` -> `Mapped[...]` + `mapped_column(...)` migration was zero-drift, so a
failure here means the DB contract changed — which belongs in a new `backend/alembic/versions/`
revision, not in a typing change.

RE-PINNED DELIBERATELY (T17, 2026-10-12): the four money columns moved `FLOAT` ->
`NUMERIC(14, 4)` in `backend/alembic/versions/0003_money_exact.py` (A-T17) — exactly the
"new alembic revision" this docstring demands.  Old canonical sha256
`4bf064cd928d663566d6a05d8e681c2d57e1cdf285ae6a3b43d23d17beacc04e` (pre-T17) ->
`0ed82ebbca2603d4431fa36f4e25b3bf5d1349b8d4525ec075967cc5912900e8`.  Only the four money
rows below changed (`daily_budget_usd`, `total_spent_usd`, `spend_usd`, `cost_usd`); every
other row is byte-identical to the D-models pin.

RE-PINNED DELIBERATELY (R17, 2026-10-13): the customer-support inbox store —
`support_inbox_items` — was added by `backend/alembic/versions/0004_support_inbox.py` (the
consumer shipped in RD12 had no producer; the RD12 receipt §7 names this table as the
missing schema decision).  17 tables -> 18; the new table's rows, its single-column unique
(`provider_message_id` — the ingest idempotency key) and its two indexes are appended
below.  Old canonical sha256 `0ed82ebbca2603d4431fa36f4e25b3bf5d1349b8d4525ec075967cc5912900e8`
-> `8c9842eeb2f9e6d028a4f08d9c6b709626a66906561585056cdbdeb14a61dc79`.  Every pre-R17 row is
byte-identical; the migration's CREATE TABLE is byte-equal to this ORM `create_all`
(probe: `build/R17-inbox/raw/probe_ddl_parity.txt`).

RE-PINNED DELIBERATELY (R31, 2026-10-13): `ad_metrics` was moved **onto** the shipped DDL.  The
ORM declared `ctr/cpc/roas` as writable `FLOAT` columns and did not map `revenue_usd` at all,
while migrations `0002`/`0003` hold the three ratios `NUMERIC(14, 6)` `GENERATED ALWAYS AS (...)`
and `revenue_usd NUMERIC(14, 4) DEFAULT '0' NOT NULL` — so the `create_all` fixture accepted an
insert the migrated schema rejects (R26 F2: `cannot INSERT into generated column "ctr"`).  This is
NOT a new DB contract and deliberately NOT a new alembic revision: it is the ORM fixture
converging on the schema the chain already ships.  Row changes: `ctr/cpc/roas` are now
`('NUMERIC(14, 6)', True, '', 'COMPUTED: <expr>', '', False)` (the fourth field renders the
generation expression — a generated column has no `server_default.arg`), and `revenue_usd`
`('NUMERIC(14, 4)', False, '0', '0', '', False)` is inserted after `spend_usd` (the migrated
column order).  Old canonical sha256
`8c9842eeb2f9e6d028a4f08d9c6b709626a66906561585056cdbdeb14a61dc79` ->
`43a976cec632bd66f97b3cae9819a8b7343ed65d3ea24c0eec567e48de6bc1f9` (measured by this file's own
`test_create_all_ddl_sha256_is_frozen`).

RE-PINNED DELIBERATELY (PHASE-3 fixer-2, 2026-10-13; L2-03 + L2-08): the ORM now maps the three
partial uniques the chain already/now ships, so `create_all` and the migrations describe ONE
schema (L2-03's gap: `agent_runs.attempt` + `ux_agent_runs_task_attempt` were migration-only,
making the retry key inert; L2-08: the run-artifact dedupes got their real keys in the new
`0005_natural_keys_and_check_validation.py`).  Row changes: `agent_runs` gains
`('attempt', 'INTEGER', True, '', '', 'ux_agent_runs_task_attempt', False)` after `usage_json` and
`task_id`'s index field gains the same name; `social_posts.platform` / `tweet_id` and
`email_logs.sendgrid_id` gain their index names (the single-column `sendgrid_id` therefore reads
`unique=True`).  Index set: + `('ux_agent_runs_task_attempt', ('task_id','attempt'), True)`,
+ `('uq_social_posts_platform_tweet_id', ('platform','tweet_id'), True)`,
+ `('uq_email_logs_sendgrid_id', ('sendgrid_id',), True)`.  Old canonical sha256
`43a976cec632bd66f97b3cae9819a8b7343ed65d3ea24c0eec567e48de6bc1f9` ->
`41deab2c2bf803959b7a75886c7499508c4379574c3579852c356e0a90c706ce` (measured by this file's own
`test_create_all_ddl_sha256_is_frozen`).

RE-PINNED DELIBERATELY (W1 tickets T03 + T19, 2026-10-13; D2 + D18): TWO deliberate schema moves,
both carried by the SAME new revision `0006_statuses_and_keys.py` — exactly the "new alembic
revision" this docstring demands.  (1) T03: `tasks.status` is escalated to
`Enum(TaskStatus, native_enum=False, create_constraint=True, name="ck_tasks_status")`
(the DDL type string stays `VARCHAR(50)`, so the column row is byte-identical) and
`create_all` now renders the column's named CHECK, `CONSTRAINT ck_tasks_status CHECK (status IN
('pending','in_progress','completed','failed','cancelled','awaiting_approval','rejected'))` —
inside the `tasks` CREATE TABLE SQL, which is what changes the hash.  (2) T19: `ad_campaigns`
gains the partial unique `uq_ad_campaigns_platform_external_id` on `(platform, external_id)
WHERE external_id IS NOT NULL` (mapped so `create_all` and migration 0006 describe one schema,
the 0005 precedent above) — the two rows' `indexes` field gains the name and the index set gains
`('uq_ad_campaigns_platform_external_id', ('platform','external_id'), True)`.  No other row
changed.  Old canonical sha256
`41deab2c2bf803959b7a75886c7499508c4379574c3579852c356e0a90c706ce` ->
`c1135b452c323bab11f2ea835cd28a82066c9b73bdb46d7647403d6a624c4e8e` (measured by this file's own
`test_create_all_ddl_sha256_is_frozen`).

RE-PINNED DELIBERATELY (docker live pass, 2026-10-13; PostgreSQL compatibility): the three
`ad_metrics` GENERATED expressions moved from `round(CAST(x AS REAL) / y, 6)` to
`round(CAST(CAST(x AS REAL) / y AS NUMERIC), 6)` — the division stays REAL (SQLite needs it; an
inner NUMERIC cast would integer-divide there) while the round INPUT is NUMERIC (PostgreSQL has no
`round(double precision, int)`; the old form upgraded every SQLite DB and died on the first real
PostgreSQL one — found live in the Docker pass, alongside the legacy-store test-isolation fix in
`test_fix5_secret_store_default_path.py`).  Old canonical sha256
`c1135b452c323bab11f2ea835cd28a82066c9b73bdb46d7647403d6a624c4e8e` ->
`e27736fb375a19e401e589b80a25b410e03ef3b4185c403adfa946bfdde5cb79` (measured by this file's own
`test_create_all_ddl_sha256_is_frozen`).

RE-PINNED DELIBERATELY (wave U5, 2026-10-14; P3 schema lane): the night-task/notification schema
landed in `backend/alembic/versions/0007_auto_mode_night_notifications.py` — exactly the "new
alembic revision" this docstring demands.  TWO deliberate moves: (1) the NEW table
`auto_mode_sessions` (P2's frozen Auto Mode window; 18 tables -> 19) with its
`ix_auto_mode_sessions_started_at` index; (2) `company_config` gains EIGHT columns (P3 night task +
P5 notifications): `night_task_enabled` BOOLEAN NOT NULL DEFAULT false, `night_task_limit` INTEGER
NOT NULL DEFAULT 3, `night_task_hour` INTEGER NULL, `notification_email` VARCHAR(255) NULL, and the
four `notify_on_*` BOOLEAN NOT NULL DEFAULT false toggles — all appended after `daily_cycle_hour`
(the ORM/migration column order).  No other row changed.  Old canonical sha256
`e27736fb375a19e401e589b80a25b410e03ef3b4185c403adfa946bfdde5cb79` ->
RE-PINNED DELIBERATELY (V8 seat c2-schema-wiring, 2026-10-14; plan §7 C2): wave V8's ONE
migration `backend/alembic/versions/0008_chat_schedules_invoices.py` adds THREE tables (19 tables
-> 22): `chat_messages` — C1's conversation store (`role` VARCHAR(50) with the named
`ck_chat_messages_role` CHECK over owner|polsia, `intent` JSON nullable, `task_id` FK -> tasks.id
nullable, `created_at` NOT NULL default now()); `task_schedules` — the cadence store (`cadence`
VARCHAR(50) with the named `ck_task_schedules_cadence` CHECK over once|daily|weekdays|weekly,
`hour` NOT NULL, `weekday` nullable, `enabled` BOOLEAN NOT NULL server default false,
`next_run_at` / `last_run_at` nullable); and `invoices` (V9 consumes; no router before V9 — V8-D2)
— `number` UNIQUE (a NEW single-column unique), money NUMERIC(12, 2) all-nullable per A-T17, the
named `ck_invoices_status` CHECK over draft|sent|paid|void|overdue, `line_items` JSON v0.
`company_config` gains THIRTEEN columns appended after `notify_on_run_requeued`:
`cycle_report_email` (BOOLEAN false), six NOT NULL text fields with server default ''
(`sender_legal_name`, `sender_postal_address`, `owner_skills`, `owner_goal`, `risk_tolerance`,
`market_locale`), `owner_hours_per_week` INTEGER NULL, `owner_budget_usd` NUMERIC(10, 2) NULL,
`channels_ready` JSON NOT NULL server default '[]', `confirmed_idea` JSON NULL, `checklist_state`
JSON NOT NULL server default '{}', `dispatch_paused` BOOLEAN false (C5's pause feature).  No index
set moved (none of the three tables carries one).  Canonical sha256 chain (each value measured by
this file's own `test_create_all_ddl_sha256_is_frozen`):
`26cd6897a5d77abf5d90cc03c764fa0ea4201f899d56e495ce2dbb2986a14e58` ->
`002b7d24591799231e289879b863ef071b3986629646186fb52eb1551bea310e` ->
`f44986f65f98ce3c82632d39456f248ca55162566fc32924f28ee98fda913106` (S1-04, wave v14: the
fabricated `invoices.tax` default 0 — python AND server — removed; see migration 0009).

RE-PINNED DELIBERATELY (V22/L1, 2026-10-19): `agent_runs.tools_used` — the DeepSeek envelope's
tool evidence (the V21 finding: produced but never persisted) — is now a real column, added by
`backend/alembic/versions/0010_agent_run_tools_used.py`.  JSON (JSONB on PostgreSQL), NULLABLE
with NO default: NULL = never recorded, `[]` = recorded and no tool ran.  Appended after
`usage_json` (the ORM/migration column order).  One column row added; no other row changed.  Old
canonical sha256 `f44986f65f98ce3c82632d39456f248ca55162566fc32924f28ee98fda913106` ->
`c17dbc5dd6861fb3dc0c45b330c40611b9c8b06c83a59d5f3ebdfd6e6d19c3a1` (measured by this file's own
`test_create_all_ddl_sha256_is_frozen`).

RE-PINNED DELIBERATELY (V23/L3, 2026-10-19): `company_config.human_hourly_rate` — the owner's
human hourly rate (COMPLETENESS §7 item 11: the Finance margin was permanently unmeasurable
because `build_planning_view` hard-coded `human_rate = None` and no column existed) — is now a
real column, added by `backend/alembic/versions/0011_company_human_rate.py`.  FLOAT (DOUBLE
PRECISION on PostgreSQL), NULLABLE with NO default: NULL = no rate was ever set (margin stays
null WITH its named reason), a stored `>= 0` value is the rate the Finance page writes.  Appended
after `dispatch_paused` (the ORM/migration column order).  One column row added; no other row
changed.  Old canonical sha256 `c17dbc5dd6861fb3dc0c45b330c40611b9c8b06c83a59d5f3ebdfd6e6d19c3a1`
-> `7e0e9f85e54e4cc019067bab323eebf373076eb30a542b3316c53d36eaade848` (measured by this file's own
`test_create_all_ddl_sha256_is_frozen`).

RE-PINNED DELIBERATELY (V25/L4, 2026-10-19): `ad_metrics.revenue_usd` is now NULLABLE with NO
default, and `ad_metrics.roas` is GENERATED with the measured-only rule — both carried by
`backend/alembic/versions/0014_ads_nullable_revenue.py`.  Rationale (the V24/L4 deviation this
closes): the pre-0014 `NUMERIC(14, 4) DEFAULT '0' NOT NULL` made an unmeasured revenue and a
measured `$0` the same stored byte, so the API had to serve every zero as `null`.  Row changes:
`revenue_usd` `('NUMERIC(14, 4)', True, '', '', '', False)` (nullable, python/server default
gone) and `roas`'s COMPUTED expression gains `revenue_usd IS NOT NULL AND` and drops `ELSE 0`
(NULL unless both inputs are measured; zero spend is not a denominator).  No other row changed.
Old canonical sha256 `7e0e9f85e54e4cc019067bab323eebf373076eb30a542b3316c53d36eaade848` ->
`519a31651b505dc2cc2e11325200fed0e2aebd95d5b470bb4b25db3cbfba8eda` (measured by this file's own
`test_create_all_ddl_sha256_is_frozen`).

RE-PINNED DELIBERATELY (V25/L1, 2026-10-19): `task_attachments` — a task's image attachments
for vision input (Polsia reads screenshots; V25 PLAN item 1) — is a NEW table added by
`backend/alembic/versions/0013_task_attachments.py`.  Eight columns, all NOT NULL except the
nullable `created_at` with its `now()` server default; `task_id` is a FK -> `tasks.id`
`ON DELETE CASCADE`; `ix_task_attachments_task_id` is the one explicit index.  `stored_name`
is the server-generated `<uuid4>.<ext>` locator (the operator's `filename` is display-only), so
the byte path can never be built from user input.  22 tables -> 23 (the table list
had already grown to 22 by V24; this revision adds the attachment table).  No existing row changed.
Old canonical sha256 `519a31651b505dc2cc2e11325200fed0e2aebd95d5b470bb4b25db3cbfba8eda` ->
`2beb3eb9011c11945ff143143cdeb2ece422bdb5b5de33e64c1d2b18998696e9` (measured by this file's own
`test_create_all_ddl_sha256_is_frozen`).

RE-PINNED DELIBERATELY (V26/L6, 2026-10-19): `ad_metrics.spend_usd` is now NULLABLE with NO
default, carried by `backend/alembic/versions/0015_ads_spend_nullable.py`.  Rationale: the column
kept the pre-0014 `DEFAULT '0'`, so a writer that omitted spend stored a measured `$0`; 0014's
revenue fix left spend as the last conflated money field.  Row change: `spend_usd`
`('NUMERIC(14, 4)', True, '', '', '', False)`.  No other row changed.  Old canonical sha256
`2beb3eb9011c11945ff143143cdeb2ece422bdb5b5de33e64c1d2b18998696e9` ->
`b0587cd74b57db17676325c5eceae4e50e17b9723df9bf07599b56b74cb276ff` (measured by this file's own
`test_create_all_ddl_sha256_is_frozen`).

RE-PINNED DELIBERATELY (V27/L4, 2026-10-19): `vision_descriptions` — the vision cache/history table
(`backend/alembic/versions/0016_vision_descriptions.py`) — is a NEW table with eight columns: the
attachment FK (ON DELETE CASCADE), the task FK (ON DELETE CASCADE), the bytes' sha256, the model's
description, the model id, the measured token count (nullable), and `created_at` with its `now()`
default. 23 tables -> 24; two explicit indexes.  No existing row changed.  Old canonical sha256
`b0587cd74b57db17676325c5eceae4e50e17b9723df9bf07599b56b74cb276ff` ->
`0fda7a89aa809db76a6cec38025986dc3dd68ff958ec2df72259940184b8af39` (measured by this file's own
`test_create_all_ddl_sha256_is_frozen`).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import UniqueConstraint, create_engine, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.database import Base
from app.models import ActivityLog, CompanyConfig, DailyReport, Task

MODELS_DIR = Path(__file__).resolve().parents[2] / "app" / "models"

DDL_SHA256 = "0fda7a89aa809db76a6cec38025986dc3dd68ff958ec2df72259940184b8af39"

# generated from probes/ddl_before.columns.tsv (pre-migration ORM create_all)
FROZEN_TABLE_NAMES = (
    'activity_log',
    'ad_campaigns',
    'ad_metrics',
    'agent_runs',
    'auto_mode_sessions',
    'chat_messages',
    'company_config',
    'competitors',
    'daily_reports',
    'email_campaigns',
    'email_logs',
    'expense_records',
    'invoices',
    'memory_entries',
    'prospects',
    'revenue_snapshots',
    'social_engagements',
    'social_posts',
    'stripe_events',
    'support_inbox_items',
    'task_attachments',
    'task_schedules',
    'tasks',
    'vision_descriptions',
)

# table -> ((column, type, nullable, python_default, server_default, indexes, unique), ...)
FROZEN_COLUMNS = {
    'activity_log': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('agent_type', 'VARCHAR(100)', False, '', '', '', False),
        ('action', 'VARCHAR(255)', False, '', '', '', False),
        ('summary', 'TEXT', False, '', '', '', False),
        ('detail', 'JSON', True, '', '', '', False),
        ('level', 'VARCHAR(20)', True, "'info'", 'info', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'ad_campaigns': (
        ('id', 'INTEGER', False, '', '', '', False),
        # T19: the two columns carry the partial unique `uq_ad_campaigns_platform_external_id`
        # (0006); a multi-column unique index does not set the per-column `unique` flag.
        ('platform', 'VARCHAR(50)', False, '', '', 'uq_ad_campaigns_platform_external_id', False),
        ('external_id', 'VARCHAR(255)', True, '', '', 'uq_ad_campaigns_platform_external_id', False),
        ('name', 'VARCHAR(512)', False, '', '', '', False),
        ('goal', 'VARCHAR(255)', True, '', '', '', False),
        ('status', 'VARCHAR(50)', True, "'active'", 'active', '', False),
        ('daily_budget_usd', 'NUMERIC(14, 4)', True, '0', '0', '', False),
        ('total_spent_usd', 'NUMERIC(14, 4)', True, '0', '0', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'ad_metrics': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('campaign_id', 'INTEGER', False, '', '', '', False),
        ('date', 'DATE', False, '', '', '', False),
        ('impressions', 'INTEGER', True, '0', '0', '', False),
        ('clicks', 'INTEGER', True, '0', '0', '', False),
        ('conversions', 'INTEGER', True, '0', '0', '', False),
        # V26/L6: nullable with NO default (alembic 0015) — NULL is "not measured", 0 is a measured zero
        ('spend_usd', 'NUMERIC(14, 4)', True, '', '', '', False),
        # V25/L4: nullable with NO default — NULL is "not measured", 0 is a measured zero
        ('revenue_usd', 'NUMERIC(14, 4)', True, '', '', '', False),
        # R31: GENERATED on both engines (`COMPUTED: <expr>` marker) — writable Floats before
        ('ctr', 'NUMERIC(14, 6)', True, '', 'COMPUTED: CASE WHEN impressions > 0 THEN round(CAST(CAST(clicks AS REAL) / impressions AS NUMERIC), 6) ELSE 0 END', '', False),
        ('cpc', 'NUMERIC(14, 6)', True, '', 'COMPUTED: CASE WHEN clicks > 0 THEN round(CAST(CAST(spend_usd AS REAL) / clicks AS NUMERIC), 6) ELSE 0 END', '', False),
        # V25/L4: the roas expression yields NULL unless revenue is measured AND spend > 0
        ('roas', 'NUMERIC(14, 6)', True, '', 'COMPUTED: CASE WHEN revenue_usd IS NOT NULL AND spend_usd > 0 THEN round(CAST(CAST(revenue_usd AS REAL) / CAST(spend_usd AS REAL) AS NUMERIC), 6) END', '', False),
    ),
    'agent_runs': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('task_id', 'INTEGER', True, '', '', 'ix_agent_runs_task_id,ux_agent_runs_task_attempt', False),
        ('agent_type', 'VARCHAR(100)', False, '', '', 'ix_agent_runs_agent_type', False),
        ('run_type', 'VARCHAR(100)', True, "'task'", 'task', '', False),
        ('status', 'VARCHAR(50)', True, "'running'", 'running', '', False),
        ('input_context', 'JSON', True, '', '', '', False),
        ('output', 'JSON', True, '', '', '', False),
        ('raw_log', 'TEXT', True, '', '', '', False),
        ('tokens_used', 'INTEGER', True, '', '', '', False),
        ('cost_usd', 'NUMERIC(14, 4)', True, '', '', '', False),
        ('duration_secs', 'FLOAT', True, '', '', '', False),
        ('started_at', 'DATETIME', True, '', 'now()', 'ix_agent_runs_started_at', False),
        ('ended_at', 'DATETIME', True, '', '', '', False),
        ('cli_session_id', 'VARCHAR(120)', True, '', '', '', False),
        ('tokens_input', 'INTEGER', True, '', '', '', False),
        ('tokens_output', 'INTEGER', True, '', '', '', False),
        ('usage_json', 'JSON', True, '', '', '', False),
        # V22/L1 (migration 0010): the runtime's tool evidence — JSON, NULLABLE, no default.
        ('tools_used', 'JSON', True, '', '', '', False),
        # PHASE-3 L2-03: the retry ledger's execution number, mapped onto the 0002 column
        ('attempt', 'INTEGER', True, '', '', 'ux_agent_runs_task_attempt', False),
    ),
    'company_config': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('name', 'VARCHAR(255)', False, '', '', '', False),
        ('mission', 'TEXT', True, '', '', '', False),
        ('vision', 'TEXT', True, '', '', '', False),
        ('description', 'TEXT', True, '', '', '', False),
        ('target_market', 'TEXT', True, '', '', '', False),
        ('value_prop', 'TEXT', True, '', '', '', False),
        ('pricing_model', 'JSON', True, '', '', '', False),
        ('goals', 'JSON', True, 'dict', '', '', False),
        ('kpis', 'JSON', True, 'dict', '', '', False),
        ('website_url', 'VARCHAR(512)', True, '', '', '', False),
        ('github_repo', 'VARCHAR(512)', True, '', '', '', False),
        ('product_type', 'VARCHAR(100)', True, '', '', '', False),
        ('industry', 'VARCHAR(100)', True, '', '', '', False),
        ('timezone', 'VARCHAR(50)', True, "'UTC'", 'UTC', '', False),
        ('daily_cycle_hour', 'INTEGER', True, '6', '6', '', False),
        # U5/0007 (P3 + P5): the night-task + notification settings; bools NOT NULL with server
        # defaults so EXISTING rows migrate materialised (the ORM `false()` element renders the
        # `false` server-default string, `DEFAULT 0` on SQLite / `DEFAULT false` on PostgreSQL).
        ('night_task_enabled', 'BOOLEAN', False, 'False', 'false', '', False),
        ('night_task_limit', 'INTEGER', False, '3', '3', '', False),
        ('night_task_hour', 'INTEGER', True, '', '', '', False),
        ('notification_email', 'VARCHAR(255)', True, '', '', '', False),
        ('notify_on_completed', 'BOOLEAN', False, 'False', 'false', '', False),
        ('notify_on_failed', 'BOOLEAN', False, 'False', 'false', '', False),
        ('notify_on_awaiting_approval', 'BOOLEAN', False, 'False', 'false', '', False),
        ('notify_on_run_requeued', 'BOOLEAN', False, 'False', 'false', '', False),
        # V8/0008 (C2): owner profile + sender identity + readiness; NOT NULL rows carry SERVER
        # defaults materialising EXISTING rows ('' / [] / {} / false), the nullable rows keep
        # NULL = "no value".
        ('cycle_report_email', 'BOOLEAN', False, 'False', 'false', '', False),
        ('sender_legal_name', 'TEXT', False, "''", '', '', False),
        ('sender_postal_address', 'TEXT', False, "''", '', '', False),
        ('owner_skills', 'TEXT', False, "''", '', '', False),
        ('owner_hours_per_week', 'INTEGER', True, '', '', '', False),
        ('owner_budget_usd', 'NUMERIC(10, 2)', True, '', '', '', False),
        ('owner_goal', 'TEXT', False, "''", '', '', False),
        ('risk_tolerance', 'TEXT', False, "''", '', '', False),
        ('channels_ready', 'JSON', False, 'list', '[]', '', False),
        ('market_locale', 'TEXT', False, "''", '', '', False),
        ('confirmed_idea', 'JSON', True, '', '', '', False),
        ('checklist_state', 'JSON', False, 'dict', '{}', '', False),
        ('dispatch_paused', 'BOOLEAN', False, 'False', 'false', '', False),
        # V23/0011 (L3): the owner's human hourly rate — NULLABLE, no default (NULL = no rate
        # was ever set; the Finance planning view keeps margin unmeasurable WITH a reason).
        ('human_hourly_rate', 'FLOAT', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
        ('updated_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'auto_mode_sessions': (
        # U5/0007 (P2's frozen table): a WINDOW, not a parent (no task FK by design).
        ('id', 'INTEGER', False, '', '', '', False),
        ('started_at', 'DATETIME', False, '', 'now()', 'ix_auto_mode_sessions_started_at', False),
        ('ends_at', 'DATETIME', False, '', '', '', False),
        ('stopped_at', 'DATETIME', True, '', '', '', False),
        ('stop_reason', 'VARCHAR(50)', True, '', '', '', False),
        ('tasks_run', 'INTEGER', False, '0', '0', '', False),
        ('spend_usd', 'NUMERIC(14, 4)', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
        ('updated_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'chat_messages': (
        # V8/0008 (C1's frozen table): the owner <-> Polsia conversation. `role` carries the
        # named ck_chat_messages_role CHECK (VARCHAR(50) rendering); `intent` is the parsed
        # exchange JSON (the pending create-task proposal rides the owner row's intent);
        # `task_id` is a nullable FK -> tasks.id (no index; most exchanges create nothing).
        ('id', 'INTEGER', False, '', '', '', False),
        ('role', 'VARCHAR(50)', False, '', '', '', False),
        ('content', 'TEXT', False, '', '', '', False),
        ('intent', 'JSON', True, '', '', '', False),
        ('task_id', 'INTEGER', True, '', '', '', False),
        ('created_at', 'DATETIME', False, '', 'now()', '', False),
    ),
    'competitors': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('name', 'VARCHAR(255)', False, '', '', '', False),
        ('website', 'VARCHAR(512)', True, '', '', '', False),
        ('pricing_info', 'JSON', True, '', '', '', False),
        ('positioning', 'TEXT', True, '', '', '', False),
        ('strengths', 'JSON', True, '', '', '', False),
        ('weaknesses', 'JSON', True, '', '', '', False),
        ('last_researched', 'DATETIME', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'daily_reports': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('report_date', 'DATE', False, '', '', '', True),
        ('morning_plan', 'TEXT', True, '', '', '', False),
        ('evening_summary', 'TEXT', True, '', '', '', False),
        ('tasks_planned', 'INTEGER', True, '0', '0', '', False),
        ('tasks_completed', 'INTEGER', True, '0', '0', '', False),
        ('tasks_failed', 'INTEGER', True, '0', '0', '', False),
        ('metrics_snapshot', 'JSON', True, '', '', '', False),
        ('insights', 'JSON', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
        ('updated_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'email_campaigns': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('name', 'VARCHAR(255)', False, '', '', '', False),
        ('goal', 'TEXT', True, '', '', '', False),
        ('target_segment', 'VARCHAR(255)', True, '', '', '', False),
        ('status', 'VARCHAR(50)', True, "'active'", 'active', '', False),
        ('total_sent', 'INTEGER', True, '0', '0', '', False),
        ('total_opened', 'INTEGER', True, '0', '0', '', False),
        ('total_replied', 'INTEGER', True, '0', '0', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'email_logs': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('prospect_id', 'INTEGER', False, '', '', '', False),
        ('campaign_id', 'INTEGER', True, '', '', '', False),
        ('sequence_step', 'INTEGER', True, '1', '1', '', False),
        ('subject', 'VARCHAR(512)', True, '', '', '', False),
        ('body', 'TEXT', True, '', '', '', False),
        # PHASE-3 L2-08: migration 0005's partial unique (unnamed-column autoindex is not used
        # because the index is partial), mapped so create_all carries the same natural key
        ('sendgrid_id', 'VARCHAR(255)', True, '', '', 'uq_email_logs_sendgrid_id', True),
        ('status', 'VARCHAR(50)', True, "'sent'", 'sent', '', False),
        ('sent_at', 'DATETIME', True, '', '', '', False),
        ('opened_at', 'DATETIME', True, '', '', '', False),
        ('replied_at', 'DATETIME', True, '', '', '', False),
        ('follow_up_due', 'DATETIME', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'expense_records': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('category', 'VARCHAR(100)', False, '', '', '', False),
        ('vendor', 'VARCHAR(255)', False, '', '', '', False),
        ('amount_cents', 'INTEGER', False, '', '', '', False),
        ('currency', 'VARCHAR(10)', True, "'usd'", 'usd', '', False),
        ('description', 'TEXT', True, '', '', '', False),
        ('date', 'DATE', False, '', '', '', False),
        ('external_ref', 'VARCHAR(255)', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'invoices': (
        # V8/0008 (V9 consumes; no router before V9): number UNIQUE (single-column constraint);
        # money NUMERIC(12, 2) ALL nullable (A-T17: NULL when unmeasured); `status` carries the
        # named ck_invoices_status CHECK; line_items is the JSON v0 itemization.
        ('id', 'INTEGER', False, '', '', '', False),
        ('number', 'TEXT', False, '', '', '', True),
        ('bill_to', 'JSON', True, '', '', '', False),
        ('issue_date', 'DATE', False, '', '', '', False),
        ('due_date', 'DATE', False, '', '', '', False),
        ('net_days', 'INTEGER', False, '30', '30', '', False),
        ('currency', 'TEXT', False, "'USD'", 'USD', '', False),
        ('subtotal', 'NUMERIC(12, 2)', True, '', '', '', False),
        # S1-04 (wave v14): NO default of any kind — the fabricated `0` (python default +
        # server default) is gone; an unmeasured tax stays NULL like subtotal/total.
        ('tax', 'NUMERIC(12, 2)', True, '', '', '', False),
        ('total', 'NUMERIC(12, 2)', True, '', '', '', False),
        ('status', 'VARCHAR(50)', False, '', '', '', False),
        ('payment_link', 'TEXT', True, '', '', '', False),
        ('paid_at', 'DATETIME', True, '', '', '', False),
        ('late_fee_note', 'TEXT', True, '', '', '', False),
        ('line_items', 'JSON', True, '', '', '', False),
    ),
    'memory_entries': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('category', 'VARCHAR(100)', False, '', '', '', False),
        ('title', 'VARCHAR(512)', False, '', '', '', False),
        ('content', 'TEXT', False, '', '', '', False),
        ('source', 'VARCHAR(100)', True, '', '', '', False),
        ('tags', 'JSON', True, '', '', '', False),
        ('chroma_id', 'VARCHAR(255)', True, '', '', '', True),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'prospects': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('email', 'VARCHAR(255)', False, '', '', '', True),
        ('first_name', 'VARCHAR(100)', True, '', '', '', False),
        ('last_name', 'VARCHAR(100)', True, '', '', '', False),
        ('company', 'VARCHAR(255)', True, '', '', '', False),
        ('title', 'VARCHAR(255)', True, '', '', '', False),
        ('source', 'VARCHAR(100)', True, '', '', '', False),
        ('status', 'VARCHAR(50)', True, "'new'", 'new', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'revenue_snapshots': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('snapshot_date', 'DATE', False, '', '', '', True),
        ('mrr_cents', 'INTEGER', True, '0', '0', '', False),
        ('arr_cents', 'INTEGER', True, '0', '0', '', False),
        ('active_subscribers', 'INTEGER', True, '0', '0', '', False),
        ('churned_today', 'INTEGER', True, '0', '0', '', False),
        ('new_today', 'INTEGER', True, '0', '0', '', False),
        ('total_revenue_month_cents', 'INTEGER', True, '0', '0', '', False),
        ('stripe_balance_cents', 'INTEGER', True, '0', '0', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'social_engagements': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('mention_id', 'VARCHAR(100)', True, '', '', '', True),
        ('author_handle', 'VARCHAR(100)', True, '', '', '', False),
        ('content', 'TEXT', True, '', '', '', False),
        ('our_reply', 'TEXT', True, '', '', '', False),
        ('reply_id', 'VARCHAR(100)', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'social_posts': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('platform', 'VARCHAR(50)', True, "'twitter'", 'twitter', 'uq_social_posts_platform_tweet_id', False),
        ('content', 'TEXT', False, '', '', '', False),
        ('status', 'VARCHAR(50)', True, "'draft'", 'draft', '', False),
        ('tweet_id', 'VARCHAR(100)', True, '', '', 'uq_social_posts_platform_tweet_id', False),
        ('scheduled_for', 'DATETIME', True, '', '', '', False),
        ('published_at', 'DATETIME', True, '', '', '', False),
        ('engagement', 'JSON', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'stripe_events': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('stripe_event_id', 'VARCHAR(255)', False, '', '', '', True),
        ('event_type', 'VARCHAR(255)', False, '', '', '', False),
        ('customer_id', 'VARCHAR(255)', True, '', '', '', False),
        ('amount_cents', 'INTEGER', True, '', '', '', False),
        ('currency', 'VARCHAR(10)', True, '', '', '', False),
        ('status', 'VARCHAR(50)', True, "'processed'", 'processed', '', False),
        ('raw_payload', 'JSON', True, '', '', '', False),
        ('processed_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'support_inbox_items': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('provider_message_id', 'VARCHAR(255)', False, '', '', '', True),
        ('from_address', 'VARCHAR(255)', False, '', '', '', False),
        ('subject', 'VARCHAR(512)', False, '', '', '', False),
        ('body', 'TEXT', False, '', '', '', False),
        ('status', 'VARCHAR(50)', False, "'pending'", 'pending', 'ix_support_inbox_items_status', False),
        ('received_at', 'DATETIME', True, '', '', '', False),
        ('task_id', 'INTEGER', True, '', '', 'ix_support_inbox_items_task_id', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'task_attachments': (
        # V25/L1 (migration 0013): image attachments. `stored_name` is the server-generated
        # `<uuid4>.<ext>` locator; `filename` is operator display text and never a path.
        ('id', 'INTEGER', False, '', '', '', False),
        ('task_id', 'INTEGER', False, '', '', 'ix_task_attachments_task_id', False),
        ('filename', 'VARCHAR(255)', False, '', '', '', False),
        ('stored_name', 'VARCHAR(128)', False, '', '', '', False),
        ('mime', 'VARCHAR(50)', False, '', '', '', False),
        ('size_bytes', 'INTEGER', False, '', '', '', False),
        ('sha256', 'VARCHAR(64)', False, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'task_schedules': (
        # V8/0008 (C2's frozen table): owner-defined recurring prompts. `cadence` carries the
        # named ck_task_schedules_cadence CHECK; `hour` company-local 0-23; `weekday` NULL for
        # the cadences that do not use it; `enabled` NOT NULL default FALSE (pause = false);
        # next_run_at/last_run_at are the tick's due/ran instants; children are JUST-IN-TIME.
        ('id', 'INTEGER', False, '', '', '', False),
        ('name', 'TEXT', False, '', '', '', False),
        ('prompt', 'TEXT', False, '', '', '', False),
        ('cadence', 'VARCHAR(50)', False, '', '', '', False),
        ('hour', 'INTEGER', False, '', '', '', False),
        ('weekday', 'INTEGER', True, '', '', '', False),
        ('enabled', 'BOOLEAN', False, 'False', 'false', '', False),
        ('next_run_at', 'DATETIME', True, '', '', '', False),
        ('last_run_at', 'DATETIME', True, '', '', '', False),
        ('created_at', 'DATETIME', False, '', 'now()', '', False),
    ),
    'tasks': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('title', 'VARCHAR(512)', False, '', '', '', False),
        ('description', 'TEXT', True, '', '', '', False),
        ('agent_type', 'VARCHAR(100)', False, '', '', '', False),
        ('priority', 'INTEGER', True, '3', '3', 'ix_tasks_pending_queue', False),
        ('status', 'VARCHAR(50)', True, "'pending'", 'pending', 'ix_tasks_pending_queue', False),
        ('source', 'VARCHAR(100)', True, "'orchestrator'", 'orchestrator', '', False),
        ('scheduled_date', 'DATETIME', True, '', '', 'ix_tasks_pending_queue', False),
        ('result_summary', 'TEXT', True, '', '', '', False),
        ('error_message', 'TEXT', True, '', '', '', False),
        ('metadata', 'JSON', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', 'ix_tasks_created_at', False),
        ('updated_at', 'DATETIME', True, '', 'now()', '', False),
        ('claimed_at', 'DATETIME', True, '', '', '', False),
        ('claimed_by', 'VARCHAR(120)', True, '', '', '', False),
        ('heartbeat_at', 'DATETIME', True, '', '', '', False),
        ('attempts', 'INTEGER', True, '0', '0', '', False),
        ('dedupe_key', 'VARCHAR(255)', True, '', '', 'ix_tasks_dedupe_key', True),
    ),
    # V27/L4 (0016): the vision description memory — one row per model answer, keyed by the
    # attachment bytes' sha256 + model for the cache and by task_id for the history read.
    'vision_descriptions': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('attachment_id', 'INTEGER', False, '', '', '', False),
        ('task_id', 'INTEGER', False, '', '', 'ix_vision_descriptions_task_id', False),
        ('sha256', 'VARCHAR(64)', False, '', '', 'ix_vision_descriptions_sha256', False),
        ('description', 'TEXT', False, '', '', '', False),
        ('model', 'VARCHAR(120)', True, '', '', '', False),
        ('tokens', 'INTEGER', True, '', '', '', False),
        ('created_at', 'DATETIME', False, '', 'now()', '', False),
    ),
}

FROZEN_UNIQUE_CONSTRAINTS = {
    'daily_reports.report_date',
    # V8/0008: the invoice identity key (single-column unique on the new table).
    'invoices.number',
    'memory_entries.chroma_id',
    'prospects.email',
    'revenue_snapshots.snapshot_date',
    'social_engagements.mention_id',
    'stripe_events.stripe_event_id',
    'support_inbox_items.provider_message_id',
}

# (name, columns, unique)
FROZEN_INDEXES = {
    ('ix_agent_runs_agent_type', ('agent_type',), False),
    ('ix_agent_runs_started_at', ('started_at',), False),
    ('ix_agent_runs_task_id', ('task_id',), False),
    # U5/0007 (P2): the Auto Mode window's covering read ("sessions covering instant T").
    ('ix_auto_mode_sessions_started_at', ('started_at',), False),
    ('ix_support_inbox_items_status', ('status',), False),
    ('ix_support_inbox_items_task_id', ('task_id',), False),
    # V25/L1 (0013): the attachments read (list + the per-task cap aggregate).
    ('ix_task_attachments_task_id', ('task_id',), False),
    # V27/L4 (0016): the vision memory reads — cache lookup (sha256) and task history (task_id).
    ('ix_vision_descriptions_sha256', ('sha256',), False),
    ('ix_vision_descriptions_task_id', ('task_id',), False),
    ('ix_tasks_created_at', ('created_at',), False),
    ('ix_tasks_dedupe_key', ('dedupe_key',), True),
    ('ix_tasks_pending_queue', ('status', 'priority', 'scheduled_date'), False),
    # PHASE-3: the three partial uniques mapped onto the shipped chain (L2-03 + L2-08)
    ('uq_email_logs_sendgrid_id', ('sendgrid_id',), True),
    ('uq_social_posts_platform_tweet_id', ('platform', 'tweet_id'), True),
    ('ux_agent_runs_task_attempt', ('task_id', 'attempt'), True),
    # T19/D18: the ad-campaign identity key (migration 0006)
    ('uq_ad_campaigns_platform_external_id', ('platform', 'external_id'), True),
}


def _render_python_default(arg: object) -> str:
    """Deterministic rendering: callables by __qualname__ (no memory address), literals by repr."""
    if callable(arg):
        return getattr(arg, "__qualname__", type(arg).__name__)
    return repr(arg)


def _orm_rows() -> dict[str, list[tuple[str, str, bool, str, str, str, bool]]]:
    """The per-column table exactly as probes/dump_schema.py derives it."""
    out: dict[str, list[tuple[str, str, bool, str, str, str, bool]]] = {}
    for tname in sorted(Base.metadata.tables):
        table = Base.metadata.tables[tname]
        index_names_by_col: dict[str, list[str]] = {c.name: [] for c in table.columns}
        unique_idx_cols: set[str] = set()
        for ix in sorted(table.indexes, key=lambda i: i.name or ""):
            col_names = [c.name for c in ix.columns]
            for cn in col_names:
                index_names_by_col.setdefault(cn, []).append(ix.name or "")
            if ix.unique and len(col_names) == 1:
                unique_idx_cols.add(col_names[0])
        unique_constraint_cols: set[str] = set()
        for con in table.constraints:
            if isinstance(con, UniqueConstraint):
                con_cols = [c.name for c in con.columns]
                if len(con_cols) == 1:
                    unique_constraint_cols.add(con_cols[0])
        rows = []
        for col in table.columns:
            py_default = "" if col.default is None else _render_python_default(col.default.arg)
            if col.computed is not None:
                # A generated column has no `server_default.arg` (measured: `Computed` carries no
                # `.arg`), so its definition IS the generation expression — rendered with an
                # explicit marker so a writable column can never be mistaken for a computed one.
                sv_default = f"COMPUTED: {col.computed.sqltext}"
            else:
                sv_default = "" if col.server_default is None else str(col.server_default.arg)
            unique = bool(
                col.unique or col.name in unique_idx_cols or col.name in unique_constraint_cols
            )
            idx = ",".join(sorted(index_names_by_col.get(col.name, [])))
            rows.append(
                (col.name, str(col.type), bool(col.nullable), py_default, sv_default, idx, unique)
            )
        out[tname] = rows
    return out


def test_table_set_is_frozen() -> None:
    assert sorted(Base.metadata.tables) == sorted(FROZEN_TABLE_NAMES)


def test_per_column_rows_are_frozen() -> None:
    actual = _orm_rows()
    assert set(actual) == set(FROZEN_COLUMNS)
    for table in sorted(FROZEN_COLUMNS):
        expected_rows = list(FROZEN_COLUMNS[table])
        assert actual[table] == expected_rows, (
            f"{table}: column contract drifted\n"
            f"  expected: {expected_rows}\n"
            f"  actual  : {actual[table]}"
        )


def test_single_column_uniques_are_frozen() -> None:
    found: set[str] = set()
    for tname, table in Base.metadata.tables.items():
        for con in table.constraints:
            if isinstance(con, UniqueConstraint) and len(con.columns) == 1:
                found.add(f"{tname}.{list(con.columns)[0].name}")
    assert found == FROZEN_UNIQUE_CONSTRAINTS


def test_explicit_indexes_are_frozen() -> None:
    found = {
        (ix.name, tuple(c.name for c in ix.columns), bool(ix.unique))
        for table in Base.metadata.tables.values()
        for ix in table.indexes
    }
    assert found == FROZEN_INDEXES


def test_create_all_ddl_sha256_is_frozen(tmp_path: Path) -> None:
    """The full create_all DDL (tables + indexes, whitespace-exact) hashed as a constant."""
    db_path = tmp_path / "ddl_check.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        conn.close()
    payload = json.dumps(
        [{"type": t, "name": n, "sql": s} for t, n, s in rows],
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert digest == DDL_SHA256, f"create_all DDL drifted: {digest} != {DDL_SHA256}"


def test_metadata_column_keeps_its_db_name() -> None:
    """DB column stays `metadata`; the Python attribute stays `metadata_json` and is mapped."""
    col = Task.__table__.c["metadata"]
    assert col.name == "metadata"
    assert Task.metadata_json.property.columns[0] is col
    assert isinstance(Task.metadata_json, InstrumentedAttribute)
    assert "metadata" not in vars(Task)


def test_legacy_column_spelling_is_gone() -> None:
    """No module under app/models/ may use the legacy `Column(...)` spelling any more.

    This is what lets mypy see an instance attribute (its value) where the class attribute
    (the mapped object) used to be typed `Column[T]` for every consumer.
    """
    legacy_call = re.compile(r"\bColumn\(")
    legacy_import = re.compile(r"^\s*(?:from|import)\s+[^\n]*\bColumn\b", re.MULTILINE)
    offenders = []
    for path in sorted(MODELS_DIR.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        if legacy_call.search(src) or legacy_import.search(src):
            offenders.append(path.name)
    assert offenders == [], f"legacy Column( spelling still present in: {offenders}"


def test_sqlite_roundtrip_reads_values_not_columns(tmp_path: Path) -> None:
    """Instance access returns VALUES (and python defaults apply); class access stays a mapped attr."""
    engine = create_engine(f"sqlite:///{tmp_path / 'roundtrip.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = Task(title="t1", agent_type="orchestrator", scheduled_date=datetime(2026, 10, 11, 6))
        session.add(task)
        session.add(
            ActivityLog(agent_type="orchestrator", action="x", summary="s", detail={"a": 1})
        )
        session.add(DailyReport(report_date=date(2026, 10, 11), insights=["i1"]))
        session.add(CompanyConfig(name="Co", timezone="UTC"))
        session.commit()

        got = session.execute(select(Task)).scalar_one()
        assert isinstance(got.id, int) and got.id == 1
        assert got.title == "t1"
        assert got.status == "pending" and got.attempts == 0  # python-side defaults applied
        assert got.metadata_json is None

        activity = session.execute(select(ActivityLog)).scalar_one()
        assert activity.level == "info"  # python-side default applied
        assert activity.detail == {"a": 1}  # JSON column round-trips

        report = session.execute(select(DailyReport)).scalar_one()
        assert report.report_date == date(2026, 10, 11)
        assert report.insights == ["i1"]

    assert isinstance(Task.id, InstrumentedAttribute)  # class access = the mapped attribute
    assert isinstance(Task.metadata_json, InstrumentedAttribute)
