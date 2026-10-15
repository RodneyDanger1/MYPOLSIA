"""R33 — the ads_management run artifacts reach `ad_campaigns`/`ad_metrics` END TO END.

The audited gap: the ads agent RUNS in production (beat `ads-stripe-sync`, every 6h —
`agent_tasks.py:646-700`) and its output schema documents `campaigns[]`/`metrics[]` as "feeding
`ad_campaigns` / `ad_metrics`" (`app/agents/ads_management/agent.py:6-7`), but
`_persist_run_artifacts` had no branch for it — the payload was silently discarded and both tables
stayed empty (README "Known gaps").  These tests drive the REAL chain on the file-backed
`queue_db`:

  * the REAL worker body (`_execute_agent_task` via the `execute_body` fixture),
  * the REAL `AdsManagementAgent` + REAL `AdsOutput` validation (the mock CLI payload is a JSON
    envelope on the repo's own `CLAUDE_CLI_MOCK_RESPONSE` seam — the payload is NOT hand-built
    into the worker, so the field names under test are the ones the schema really emits),
  * the REAL writer (`_persist_run_artifacts` -> `ads_service.record_ads_run`) and the REAL read
    path (`list_campaigns`/`list_metrics`).

RED legs (receipt, `raw/redleg/`): (a) with the `ads_management` branch dropped, the happy-path
test fails on the NAMED absence ("no `persistence` report" — the payload was discarded); (b) with
the dedupe reads neutralized, the idempotency test fails — the re-run double-inserts.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

CAMPAIGN = {
    "platform": "google_ads",
    "external_id": "g-100",
    "name": "Search Brand",
    "status": "active",
    "daily_budget_usd": 25.0,
    "total_spent_usd": 12.5,
}
METRIC_D1 = {  # 2026-10-12 — spend 12.5, revenue unreported by the schema (see the roas pin)
    "campaign_external_id": "g-100",
    "date": "2026-10-12",
    "impressions": 100,
    "clicks": 5,
    "conversions": 1,
    "spend_usd": 12.5,
    "ctr": 0.05,
    "cpc": 2.5,
    "roas": 2.0,
}
METRIC_D2 = {
    "campaign_external_id": "g-100",
    "date": "2026-10-13",
    "impressions": 40,
    "clicks": 2,
    "conversions": 0,
    "spend_usd": 2.5,
    "ctr": 0.05,
    "cpc": 1.25,
    "roas": 1.0,
}


def _ads_run(queue_db, payload: dict[str, Any], *, title: str = "Sync ad metrics") -> dict[str, Any]:
    """One ads_management task through the real worker (payload via the CLI mock seam)."""
    return queue_db.create_task(agent_type="ads_management", title=title)


def _arm_mock_cli(monkeypatch, payload: dict[str, Any]) -> None:
    """The repo's own mock seam: the agent must validate this payload against AdsOutput itself."""
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE", json.dumps({"result": json.dumps(payload)})
    )


def _latest_run_output(queue_db) -> dict[str, Any]:
    """The last run row's stored output, read from the FILE (stdlib driver, no ORM)."""
    rows = queue_db.query("SELECT output FROM agent_runs ORDER BY id DESC LIMIT 1")
    if not rows or not rows[0]["output"]:
        return {}
    return json.loads(rows[0]["output"])


def _read_via_services(queue_db) -> tuple[list[Any], list[Any]]:
    """`list_campaigns` + `list_metrics` — the production read path — on the queue database."""

    async def _read():
        from app.services.ads_service import list_campaigns, list_metrics

        async with queue_db.factory() as db:
            campaigns = await list_campaigns(db)
            metrics = await list_metrics(db, campaigns[0].id) if campaigns else []
            return campaigns, metrics

    return queue_db.run(_read())


# ── 1. the happy path: rows exist, the reads serve them, the engine owns the ratios ──────────


def test_a_completed_ads_run_persists_its_campaigns_and_metrics(monkeypatch, queue_db, execute_body):
    _arm_mock_cli(monkeypatch, {
        "summary": "read 1 campaign / 2 metric days from Google",
        "campaigns": [CAMPAIGN],
        "metrics": [METRIC_D1, METRIC_D2],
    })

    outcome = execute_body(_ads_run(queue_db, "sync"))
    assert outcome["status"] == "completed"

    # RED leg (a) names itself FIRST: with the branch dropped there is no report at all
    persistence = _latest_run_output(queue_db).get("persistence")
    assert persistence is not None, (
        "the ads_management persistence branch is absent: the run's campaigns[]/metrics[] payload "
        "was discarded (no `persistence` report on the run output)"
    )
    assert persistence["ok"] is True
    assert (persistence["campaigns_written"], persistence["metrics_written"]) == (1, 2)
    assert persistence["duplicates_skipped"] == 0
    assert queue_db.count("activity_log", "action = ?", ("run_artifacts_not_persisted",)) == 0

    # rows on disk, one commit with the run
    assert queue_db.count("ad_campaigns") == 1
    assert queue_db.count("ad_metrics") == 2

    # the production read path serves them, deterministically
    campaigns, metrics = _read_via_services(queue_db)
    assert [c.name for c in campaigns] == ["Search Brand"]
    campaign = campaigns[0]
    assert (campaign.platform, campaign.external_id, campaign.status) == (
        "google_ads", "g-100", "active",
    )
    # money is exact at rest (Decimal, scale 4) — never a binary float
    assert campaign.daily_budget_usd == Decimal("25.0000")
    assert campaign.total_spent_usd == Decimal("12.5000")
    assert isinstance(campaign.daily_budget_usd, Decimal)

    assert [m.date.isoformat() for m in metrics] == ["2026-10-13", "2026-10-12"]  # date DESC
    newest, oldest = metrics
    # the ENGINE computed the ratios (GENERATED columns) from the reported raw inputs
    assert oldest.ctr == Decimal("0.050000"), "5 clicks / 100 impressions"
    assert oldest.cpc == Decimal("2.500000"), "12.5000 spend / 5 clicks"
    assert newest.ctr == Decimal("0.050000"), "2 / 40"
    assert newest.cpc == Decimal("1.250000"), "2.5000 / 2"
    assert oldest.spend_usd == Decimal("12.5000") and newest.spend_usd == Decimal("2.5000")
    assert oldest.roas == Decimal("0.000000"), (
        "revenue unreported -> the derived roas is the column's 0 (see the pin at the bottom)"
    )


# ── 2. idempotency: the 6-hourly beat re-runs the SAME payload ────────────────────────────────


def test_rerunning_the_same_payload_writes_no_duplicate_rows(monkeypatch, queue_db, execute_body):
    """RED leg (b): with the dedupe reads neutralized, the counts below climb."""
    payload = {
        "summary": "read 1 campaign / 2 metric days from Google",
        "campaigns": [CAMPAIGN],
        "metrics": [METRIC_D1, METRIC_D2],
    }
    _arm_mock_cli(monkeypatch, payload)
    assert execute_body(_ads_run(queue_db, "first"))["status"] == "completed"
    (first_campaign,), first_metrics = _read_via_services(queue_db)

    _arm_mock_cli(monkeypatch, payload)
    assert execute_body(_ads_run(queue_db, "second"))["status"] == "completed"

    # nothing was inserted a second time
    assert queue_db.count("ad_campaigns") == 1
    assert queue_db.count("ad_metrics") == 2
    persistence = _latest_run_output(queue_db)["persistence"]
    assert persistence["ok"] is True, "a duplicate is the steady state of a 6h beat, not a failure"
    assert (persistence["campaigns_written"], persistence["metrics_written"]) == (0, 0)
    assert persistence["duplicates_skipped"] == 3  # 1 campaign (provider id) + 2 metrics (date key)
    assert "skipped_malformed" not in persistence

    # the SAME rows were reused (ids unchanged), not recreated
    campaigns, metrics = _read_via_services(queue_db)
    assert campaigns[0].id == first_campaign.id
    assert [m.id for m in metrics] == [m.id for m in first_metrics]
    assert queue_db.count("activity_log", "action = ?", ("run_artifacts_not_persisted",)) == 0


# ── 3. malformed entries: skipped BY NAME; the rest of the payload is still persisted ─────────


def test_malformed_entries_are_skipped_by_name_and_the_rest_is_persisted(
    monkeypatch, queue_db, execute_body
):
    _arm_mock_cli(monkeypatch, {
        "summary": "one bad campaign and one bad metric day on the wire",
        "campaigns": [
            CAMPAIGN,
            {"platform": "meta_ads", "external_id": "m-1", "name": "Meta Bad",
             "status": "womble", "daily_budget_usd": 5.0, "total_spent_usd": 1.0},
        ],
        "metrics": [
            {"campaign_external_id": "g-100", "date": "10/12/2026", "impressions": 9,
             "clicks": 1, "conversions": 0, "spend_usd": 1.0, "ctr": 0.1, "cpc": 1.0, "roas": 0.0},
            METRIC_D2,
        ],
    })

    outcome = execute_body(_ads_run(queue_db, "partial"))
    assert outcome["status"] == "completed", "the run stays alive; only the items are skipped"

    # the good half landed, on the run's own transaction
    assert queue_db.count("ad_campaigns") == 1
    assert queue_db.count("ad_metrics") == 1

    persistence = _latest_run_output(queue_db)["persistence"]
    assert persistence["ok"] is False
    assert persistence["campaigns_written"] == 1
    assert persistence["metrics_written"] == 1
    skips = persistence["skipped_malformed"]
    assert [(s["kind"], s["index"]) for s in skips] == [("campaign", 1), ("metric", 0)]
    assert all(s["code"] == "E_VALIDATION" and s["message"] for s in skips)
    assert "ck_ad_campaigns_status" in skips[0]["message"]     # create_campaign's named refusal
    assert "ISO date" in skips[1]["message"]
    assert persistence["error"] == {"code": skips[0]["code"], "message": skips[0]["message"]}
    # the worker's existing warning path fired exactly once, naming the first skip
    rows = queue_db.query(
        "SELECT summary, level FROM activity_log WHERE action = 'run_artifacts_not_persisted'"
    )
    assert len(rows) == 1 and rows[0]["level"] == "warning"
    assert "E_VALIDATION" in rows[0]["summary"]


# ── 4. the empty payload is the honest zero, not an error ─────────────────────────────────────


def test_an_empty_payload_reports_measured_zeros_not_an_error(monkeypatch, queue_db, execute_body, mock_chroma):
    _arm_mock_cli(monkeypatch, {"summary": "nothing to sync this cycle", "campaigns": [], "metrics": []})

    outcome = execute_body(_ads_run(queue_db, "empty"))
    assert outcome["status"] == "completed"

    persistence = _latest_run_output(queue_db)["persistence"]
    # V15/4-05b re-pin (r-repair unit): the memory write leg rides the same report.  The vector
    # store is armed (mock_chroma) so the run's memory stores DETERMINISTICALLY; the original
    # zero-report assertion is preserved by popping that key first.
    memory = persistence.pop("memory")
    assert persistence == {
        "ok": True, "campaigns_written": 0, "metrics_written": 0, "duplicates_skipped": 0,
        "error": None,
    }
    assert memory["ok"] is True, memory
    assert queue_db.count("memory_entries") == 1
    assert queue_db.count("ad_campaigns") == 0 and queue_db.count("ad_metrics") == 0
    assert queue_db.count("activity_log", "action = ?", ("run_artifacts_not_persisted",)) == 0


# ── 5. T09/D17: the schema NOW carries revenue_usd, so the persisted roas is computed ────────


def test_reported_revenue_survives_validation_and_drives_the_engine_computed_roas(
    monkeypatch, queue_db, execute_body
):
    """T09/D17 pin — REPAIRED, old→new recorded.

    OLD (R33's residual pin, now closed): `AdMetricRow` had no `revenue_usd`, Pydantic dropped
    the unknown key, the writer persisted the column's 0 default and the engine-computed `roas`
    read 0 — asserted as `revenue_usd == 0` / `roas == 0` with the note "when the schema gains
    the field, this pin flips".

    NEW (D17): the field exists, so the agent's reported revenue survives validation, reaches
    `record_ads_run`'s money loop and lands in the `Numeric(14,4)` column; `roas` is a GENERATED
    column (`models/ads.py`), so the engine computes it from that revenue — the value a model
    reports for `roas` remains a display figure the row does not store.

    The second metric has NO `revenue_usd` key: not-reported stays the column's own default
    (a measured 0 the writer never fabricates), which is the F-11 rule this pin keeps.
    """
    from app.schemas.agent_outputs import AdMetricRow

    assert "revenue_usd" in AdMetricRow.model_fields, (
        "D17 added revenue_usd to AdMetricRow; this pin asserts the persisted consequences"
    )

    _arm_mock_cli(monkeypatch, {
        "summary": "two metric days: one with reported revenue, one without",
        "campaigns": [CAMPAIGN],
        "metrics": [dict(METRIC_D1, revenue_usd=30.0), METRIC_D2],
    })
    assert execute_body(_ads_run(queue_db, "roas"))["status"] == "completed"

    _, metrics = _read_via_services(queue_db)
    by_date = {metric.date.isoformat(): metric for metric in metrics}
    reported = by_date["2026-10-12"]
    unreported = by_date["2026-10-13"]

    assert reported.revenue_usd == Decimal("30.0000"), "the reported revenue crosses to the column"
    assert reported.roas == Decimal("2.400000"), "the engine computes roas from 30 / 12.5"
    assert unreported.revenue_usd == Decimal("0.0000"), "not reported -> the column default"
    assert unreported.roas == Decimal("0.000000"), "no revenue input -> the engine's 0"

    # …and the run's own report now carries the key the schema declares (it used to be dropped)
    stored_metric = _latest_run_output(queue_db)["metrics"][0]
    assert stored_metric["revenue_usd"] == 30.0
