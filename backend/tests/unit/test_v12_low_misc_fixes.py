"""V12/W-C LOW triage — misc fixes: F15b (task_routes for the three newest modules),
F17 (chat money rendering through Decimal), F23 (catalog service-key validation + ops cache
clear), F24 (provenance known-unknown).

RED-first: every leg here failed on the pre-fix tree (lane raw/red-*.txt; re-runnable via raw/redleg.sh).
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.core.errors import E_CONFIG, PolsiaError


# ── F15b — the three newest modules are routed ──────────────────────────────────────────────
def test_f15b_task_routes_cover_the_three_newest_modules():
    from celery_app import celery_config

    routes = celery_config.task_routes
    assert routes["celery_app.tasks.cadence.*"] == {"queue": "scheduler"}
    assert routes["celery_app.tasks.followups.*"] == {"queue": "agents"}
    assert routes["celery_app.tasks.dunning.*"] == {"queue": "agents"}


# ── F17 — chat money rendering stays exact past 2^53 cents ──────────────────────────────────
def test_f17_chat_money_rendering_is_exact():
    from app.services.chat_service import _money_cents, _render_finance

    exact = "$1,000,000,000,000,000.01"
    assert _money_cents(10**17 + 1) == exact
    assert exact in _render_finance({"mrr_cents": 10**17 + 1})
    assert _money_cents(None) == "not measured"  # unmeasured stays honest


# ── F23 — a malformed catalog row refuses at LOAD (by name), and ops can clear the cache ────
def test_f23_malformed_catalog_row_refuses_at_load_and_ops_clear_the_cache(tmp_path, monkeypatch):
    import app.services.pricing_service as pricing

    rows = [{"service": f"svc-{i}", "date": "2026-01-01"} for i in range(50)]
    del rows[7]["service"]
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(pricing, "CATALOG_PATH", catalog)
    pricing.load_catalog.cache_clear()
    try:
        with pytest.raises(PolsiaError) as excinfo:
            pricing.load_catalog()
        assert excinfo.value.code == E_CONFIG
        assert "service" in excinfo.value.message
    finally:
        pricing.load_catalog.cache_clear()

    clear = getattr(pricing, "clear_catalog_cache", None)
    assert callable(clear), "ops needs a cache-clear for a catalog fixed on disk"
    clear()


# ── F24 — an unparseable newest date is a KNOWN unknown, not "no dates at all" ──────────────
def test_f24_unparseable_newest_date_is_reported_as_a_known_unknown(tmp_path, monkeypatch):
    import app.services.pricing_service as pricing

    rows = [{"service": f"svc-{i}", "date": "2099-01-01"} for i in range(49)]
    rows.append({"service": "svc-x", "date": "not-a-date"})
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(pricing, "CATALOG_PATH", catalog)
    pricing.load_catalog.cache_clear()
    try:
        prov = pricing.catalog_provenance(today=date(2026, 1, 1))
        assert prov["catalog_as_of"] == "not-a-date", (
            "the newest date IS known (it is just unparseable) — reporting null erases it"
        )
        assert prov["staleness_days"] is None and prov["stale"] is None
        assert prov.get("reason")
    finally:
        pricing.load_catalog.cache_clear()
