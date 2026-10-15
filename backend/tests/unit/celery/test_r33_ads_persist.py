"""R33 — `ads_service.record_ads_run` at the writer level: natural keys, skips, rescues, money.

The e2e file (`tests/unit/test_r33_ads_persist_e2e.py`) drives the whole worker+agent chain; this
file pins the WRITER's own contract on the file-backed `queue_db`, where the interesting cases
need planted database state:

  * natural-key dedupe — campaigns by provider id (`platform`+`external_id`), with the
    `(platform, name)` fallback when the payload reported none; metrics by `(campaign_id, date)`,
    the key `uq_ad_metrics_campaign_date` guards (alembic `0002:157-158`);
  * the RACED unique violation — the real unique index is installed, the pre-check is made to
    miss once (the raced read is the double; the DB constraint and the savepoint rescue are
    real), and the batch must survive with the duplicate counted, not misattributed;
  * a constraint the natural key cannot explain — skipped BY NAME (`E_DB`), never reported as a
    duplicate;
  * every malformed shape → one named skip, `ok=False`, first skip as `error`, batch continues;
  * money crosses only through `task_service._as_column_decimal` (A-T17) — floats and Decimals
    both land scale-4 exact, and `revenue_usd` (which `AdMetricRow` does not carry today) is
    honored exactly when a payload does report it;
  * an unreadable database is a batch-fatal NAMED refusal in `error`, never a raise.

RED leg (b) of the receipt plants here: neutralize `_find_existing_campaign`/`_find_metric_id`
(return None) and the dedupe tests double-insert.
"""
from __future__ import annotations

import inspect
from decimal import Decimal
from typing import Any

import pytest

from app.services import ads_service


def _campaign(**overrides: Any) -> dict[str, Any]:
    row = {
        "platform": "google_ads",
        "external_id": "g-100",
        "name": "Search Brand",
        "status": "active",
        "daily_budget_usd": 25.0,
        "total_spent_usd": 12.5,
    }
    row.update(overrides)
    return row


def _metric(**overrides: Any) -> dict[str, Any]:
    row = {
        "campaign_external_id": "g-100",
        "date": "2026-10-12",
        "impressions": 100,
        "clicks": 5,
        "conversions": 1,
        "spend_usd": 12.5,
    }
    row.update(overrides)
    return row


def _write(queue_db, payload: dict[str, Any], *, commit: bool = True) -> dict[str, Any]:
    """One writer call on a real session, committed like the worker's transaction."""

    async def _go() -> dict[str, Any]:
        async with queue_db.factory() as db:
            report = await ads_service.record_ads_run(db, payload)
            if commit:
                await db.commit()
            return report

    return queue_db.run(_go())


def _read(queue_db) -> tuple[list[Any], list[Any]]:
    """The production read path (`list_campaigns`/`list_metrics`) on the same database."""

    async def _go():
        async with queue_db.factory() as db:
            campaigns = await ads_service.list_campaigns(db)
            metrics = await ads_service.list_metrics(db, campaigns[0].id) if campaigns else []
            return campaigns, metrics

    return queue_db.run(_go())


def _install_unique_index(queue_db, name: str, table_columns: str) -> None:
    """A REAL unique index — the shape the migrated schema ships (0002:157-158 pins the name)."""

    async def _go() -> None:
        from sqlalchemy import text

        async with queue_db.factory() as db:
            await db.execute(text(f"CREATE UNIQUE INDEX {name} ON {table_columns}"))
            await db.commit()

    queue_db.run(_go())


def _assert_single_skip(report: dict[str, Any], fragment: str) -> dict[str, Any]:
    skips = report.get("skipped_malformed")
    assert skips is not None and len(skips) == 1, f"expected exactly one named skip: {report}"
    skip = skips[0]
    assert fragment in skip["message"], skip
    assert report["ok"] is False
    assert report["error"] == {"code": skip["code"], "message": skip["message"]}
    return skip


# ── the happy path + money exactness (A-T17) ─────────────────────────────────


def test_both_tables_are_written_and_the_engine_computes_the_ratios(queue_db):
    report = _write(queue_db, {"campaigns": [_campaign()], "metrics": [
        _metric(revenue_usd=Decimal("12.3456")),
    ]})
    assert report == {
        "ok": True, "campaigns_written": 1, "metrics_written": 1, "duplicates_skipped": 0,
        "error": None,
    }
    campaigns, metrics = _read(queue_db)
    assert (campaigns[0].platform, campaigns[0].external_id) == ("google_ads", "g-100")
    assert campaigns[0].daily_budget_usd == Decimal("25.0000")
    assert campaigns[0].total_spent_usd == Decimal("12.5000")
    metric = metrics[0]
    # ctr/cpc/roas are GENERATED — the writer never sets them; the engine computed all three
    assert metric.ctr == Decimal("0.050000")        # 5 / 100
    assert metric.cpc == Decimal("2.500000")        # 12.5000 / 5
    assert metric.roas == Decimal("0.987648")       # 12.3456 / 12.5000 -> round(..., 6)
    assert metric.spend_usd == Decimal("12.5000")
    assert metric.revenue_usd == Decimal("12.3456"), "a reported Decimal survives exactly"
    assert all(isinstance(v, Decimal) for v in (
        metric.spend_usd, metric.revenue_usd, metric.ctr, metric.cpc, metric.roas,
    ))


def test_unreported_revenue_leaves_the_not_null_default_it_never_fabricates(queue_db):
    report = _write(queue_db, {"campaigns": [_campaign()], "metrics": [_metric()]})
    assert report["ok"] is True
    _, metrics = _read(queue_db)
    assert metrics[0].revenue_usd == Decimal("0.0000"), "the column's default, written by nobody"
    assert metrics[0].roas == Decimal("0.000000")


# ── idempotency: the natural keys ────────────────────────────────────────────


def test_rerunning_the_same_payload_writes_nothing_new(queue_db):
    payload = {"campaigns": [_campaign()], "metrics": [_metric(), _metric(date="2026-10-13")]}
    first = _write(queue_db, payload)
    assert (first["campaigns_written"], first["metrics_written"]) == (1, 2)

    second = _write(queue_db, payload)
    assert second == {
        "ok": True, "campaigns_written": 0, "metrics_written": 0, "duplicates_skipped": 3,
        "error": None,
    }
    assert queue_db.count("ad_campaigns") == 1
    assert queue_db.count("ad_metrics") == 2


def test_a_duplicate_metric_inside_one_payload_is_counted_not_written(queue_db):
    report = _write(queue_db, {"campaigns": [_campaign()], "metrics": [
        _metric(), _metric(impressions=1, clicks=0, spend_usd=0.0),
    ]})
    assert report["metrics_written"] == 1 and report["duplicates_skipped"] == 1
    assert queue_db.count("ad_metrics") == 1


def test_a_campaign_without_a_provider_id_dedupes_on_platform_and_name(queue_db):
    payload = {"campaigns": [{"platform": "meta_ads", "name": "No Id Camp"}], "metrics": []}
    first = _write(queue_db, payload)
    second = _write(queue_db, payload)
    assert first["campaigns_written"] == 1
    assert second["campaigns_written"] == 0 and second["duplicates_skipped"] == 1
    assert queue_db.count("ad_campaigns") == 1


def test_a_metric_resolves_a_campaign_written_by_an_earlier_run(queue_db):
    """The agent reads metrics for campaigns that already exist — the DB-resolution branch."""
    _write(queue_db, {"campaigns": [_campaign()], "metrics": []})
    report = _write(queue_db, {"campaigns": [], "metrics": [_metric()]})
    assert report["metrics_written"] == 1 and report["ok"] is True
    assert queue_db.count("ad_campaigns") == 1 and queue_db.count("ad_metrics") == 1


# ── the raced unique violation: savepoint rescue, then the natural key decides ────────────────


def test_a_raced_unique_violation_is_rescued_by_the_natural_key(queue_db, monkeypatch):
    _install_unique_index(
        queue_db, "uq_ad_metrics_campaign_date", "ad_metrics (campaign_id, date)"
    )
    _write(queue_db, {"campaigns": [_campaign()], "metrics": [_metric(date="2026-10-12")]})

    real = ads_service._find_metric_id
    calls = {"n": 0}

    async def miss_once(db, campaign_id, day):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the raced read: another ads run committed between check and insert
        return await real(db, campaign_id, day)

    monkeypatch.setattr(ads_service, "_find_metric_id", miss_once)
    report = _write(queue_db, {"campaigns": [], "metrics": [
        _metric(date="2026-10-12"),  # trips the real unique index; the rescue re-resolves
        _metric(date="2026-10-13"),  # proves the savepoint left the session healthy
    ]})

    assert report["ok"] is True, report
    assert report["metrics_written"] == 1 and report["duplicates_skipped"] == 1
    assert "skipped_malformed" not in report, "a raced duplicate is a duplicate, not a failure"
    assert queue_db.count("ad_metrics") == 2


def test_a_constraint_the_natural_key_cannot_explain_is_skipped_by_name(queue_db, monkeypatch):
    """A unique violation the `(campaign_id, date)` re-read cannot account for is NOT a duplicate.

    Planted as a real (deliberately unrelated) unique index — the writer's re-resolve must find
    no natural-key row, so it refuses the item BY NAME and the batch continues after it.
    """
    _install_unique_index(
        queue_db, "uq_probe_campaign_impressions", "ad_metrics (campaign_id, impressions)"
    )
    _write(queue_db, {"campaigns": [_campaign()], "metrics": [
        _metric(date="2026-10-12", impressions=100),
    ]})

    real = ads_service._find_metric_id
    calls = {"n": 0}

    async def miss_once(db, campaign_id, day):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the contested row is on a different day: the natural key cannot see it
        return await real(db, campaign_id, day)

    monkeypatch.setattr(ads_service, "_find_metric_id", miss_once)
    report = _write(queue_db, {"campaigns": [], "metrics": [
        _metric(date="2026-10-13", impressions=100),  # different day, same impressions: trips
        _metric(date="2026-10-14", impressions=5),    # the batch continues past the skip
    ]})

    skip = _assert_single_skip(report, "database constraint")
    assert skip["kind"] == "metric" and skip["code"] == "E_DB"
    assert report["metrics_written"] == 1
    assert queue_db.count("ad_metrics") == 2, "only the unrelated index was tripped"


# ── ambiguity: a provider id two rows claim cannot be resolved honestly ───────────────────────


def test_a_metric_for_a_provider_id_two_rows_claim_is_skipped_by_name(queue_db):
    report = _write(queue_db, {"campaigns": [
        _campaign(platform="google_ads", external_id="shared-1", name="G"),
        _campaign(platform="meta_ads", external_id="shared-1", name="M"),
    ], "metrics": [_metric(campaign_external_id="shared-1")]})

    assert report["campaigns_written"] == 2, "both campaigns are real rows"
    assert report["metrics_written"] == 0
    skip = _assert_single_skip(report, "matches several")
    assert skip["kind"] == "metric"


def test_a_metric_for_a_db_provider_id_two_rows_claim_is_skipped_by_name(queue_db):
    async def _seed() -> None:
        from app.models.ads import AdCampaign

        async with queue_db.factory() as db:
            db.add(AdCampaign(platform="google_ads", name="one", external_id="dup-1"))
            db.add(AdCampaign(platform="meta_ads", name="two", external_id="dup-1"))
            await db.commit()

    queue_db.run(_seed())
    report = _write(queue_db, {"campaigns": [], "metrics": [
        _metric(campaign_external_id="dup-1"),
    ]})
    skip = _assert_single_skip(report, "matches several")
    assert skip["kind"] == "metric" and report["metrics_written"] == 0


# ── every malformed shape: one named skip, ok=False, the rest still persisted ─────────────────


@pytest.mark.parametrize(
    ("case_id", "payload", "fragment", "expected"),
    [
        ("campaign-not-object", {"campaigns": ["x"], "metrics": []},
         "campaign entry is not an object", (0, 0)),
        ("campaign-blank-name", {"campaigns": [_campaign(name="   ")], "metrics": []},
         "no usable platform/name", (0, 0)),
        ("campaign-blank-platform", {"campaigns": [_campaign(platform="")], "metrics": []},
         "no usable platform/name", (0, 0)),
        ("campaign-int-external-id", {"campaigns": [_campaign(external_id=100)], "metrics": []},
         "is not a string", (0, 0)),
        ("campaign-bad-status", {"campaigns": [_campaign(status="womble")], "metrics": []},
         "ck_ad_campaigns_status", (0, 0)),
        ("campaign-bad-money", {"campaigns": [_campaign(daily_budget_usd="not-money")], "metrics": []},
         "is not numeric money", (0, 0)),
        ("metric-not-object", {"campaigns": [_campaign()], "metrics": ["x"]},
         "metric entry is not an object", (1, 0)),
        ("metric-no-ref", {"campaigns": [_campaign()],
                           "metrics": [_metric(campaign_external_id="  ")]},
         "no campaign_external_id", (1, 0)),
        ("metric-unknown-campaign", {"campaigns": [_campaign()],
                                     "metrics": [_metric(campaign_external_id="g-nope")]},
         "not in this run's campaigns[] and not in ad_campaigns", (1, 0)),
        ("metric-bad-date", {"campaigns": [_campaign()], "metrics": [_metric(date="10/12/2026")]},
         "ISO date", (1, 0)),
        ("metric-clicks-gt-impressions", {"campaigns": [_campaign()],
                                          "metrics": [_metric(clicks=500)]},
         "ck_ad_metrics_clicks_le_impressions", (1, 0)),
        ("metric-negative", {"campaigns": [_campaign()], "metrics": [_metric(impressions=-1)]},
         "ck_ad_metrics_nonneg", (1, 0)),
        ("metric-not-int", {"campaigns": [_campaign()], "metrics": [_metric(impressions="3")]},
         "is not an integer", (1, 0)),
        ("metric-bool", {"campaigns": [_campaign()], "metrics": [_metric(clicks=True)]},
         "is not an integer", (1, 0)),
        ("metric-bad-spend", {"campaigns": [_campaign()], "metrics": [_metric(spend_usd="nope")]},
         "is not numeric money", (1, 0)),
        ("metric-negative-spend", {"campaigns": [_campaign()], "metrics": [_metric(spend_usd=-1.5)]},
         "ck_ad_metrics_nonneg", (1, 0)),
        ("metric-bad-revenue", {"campaigns": [_campaign()],
                                "metrics": [_metric(revenue_usd=["x"])]},
         "is not numeric money", (1, 0)),
        ("bad-first-metric-then-good", {"campaigns": [_campaign()], "metrics": [
            _metric(date="nope"), _metric(date="2026-10-13"),
        ]}, "ISO date", (1, 1)),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_a_malformed_entry_is_skipped_by_name_and_the_batch_continues(
    queue_db, case_id, payload, fragment, expected
):
    report = _write(queue_db, payload)
    _assert_single_skip(report, fragment)
    assert (report["campaigns_written"], report["metrics_written"]) == expected, case_id
    assert queue_db.count("ad_campaigns") == expected[0]
    assert queue_db.count("ad_metrics") == expected[1]
    assert report["duplicates_skipped"] == 0


# ── batch-fatal failures are named refusals in the report, never raises ───────────────────────


def test_an_unreadable_table_is_a_batch_fatal_named_refusal(queue_db):
    async def _drop() -> None:
        from sqlalchemy import text

        async with queue_db.factory() as db:
            await db.execute(text("DROP TABLE ad_campaigns"))
            await db.commit()

    queue_db.run(_drop())
    report = _write(queue_db, {"campaigns": [_campaign()], "metrics": []}, commit=False)
    assert report["ok"] is False
    assert report["error"]["code"] == "E_DB"
    assert "could not read ad campaign" in report["error"]["message"]


def test_an_unexpected_failure_is_reported_never_raised(queue_db):
    """The worker contract is a report: a driver-level surprise comes back in `error`."""
    report = queue_db.run(ads_service.record_ads_run(
        object(),  # a session with no execute(): the generic-Exception arm
        {"campaigns": [{"platform": "google_ads", "name": "boom"}], "metrics": []},
    ))
    assert report["ok"] is False
    assert report["error"]["code"] == "E_DB"
    assert "could not persist ads run artifacts" in report["error"]["message"]


# ── the dispatcher's contract + the deliberately unexported writer ────────────────────────────


def test_the_dispatcher_returns_the_honest_zero_for_an_empty_payload(queue_db):
    """No db is even touched: an empty payload IS the schema's own shape, and it is a measured
    zero — a report, not an absence, and not an error.

    V15 re-pin (r-repair unit, 4-05b): the memory write leg rides the SAME report — an empty
    payload carries no summary, so the leg SKIPS BY NAME (`E_VALIDATION`, a memory is never
    invented) beside the zero; the original assertion is preserved by popping that key first.
    """
    from celery_app.tasks.agent_tasks import _persist_run_artifacts

    report = queue_db.run(_persist_run_artifacts(None, "ads_management", {}))
    memory = report.pop("memory")
    assert report == {
        "ok": True, "campaigns_written": 0, "metrics_written": 0, "duplicates_skipped": 0,
        "error": None,
    }
    assert memory["ok"] is False
    assert memory["error"]["code"] == "E_VALIDATION"


def test_record_ads_run_stays_outside_the_pinned_public_surface():
    """`__all__` is the pinned four-name contract (test_r26_ads_sweep.py:147-156); the worker
    dispatch imports the writer by name — the `task_service._as_column_decimal` arrangement."""
    assert inspect.iscoroutinefunction(ads_service.record_ads_run)
    assert "record_ads_run" not in ads_service.__all__
    assert ads_service.__all__ == [
        "create_campaign", "get_campaign", "list_campaigns", "list_metrics",
    ]
