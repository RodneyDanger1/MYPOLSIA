"""V22 honesty pins (W2/D1, D2, D9) — fabricated values must not exist anywhere in the seed or the
chat-history total.

- **D1:** the seed used to write zero-valued KPIs (`mrr_usd: 0`, `active_customers: 0`, …) that the
  dashboard rendered as MEASUREMENTS. Zero is a measurement; "never measured" is not zero. The seed
  now writes `{}` and the UI renders "—".
- **D2:** the seed used to write `website_url="https://yourcompany.com"` — a placeholder that
  SATISFIED the email-compliance footer gate, so a live (SANDBOX_MODE=false) send would print
  privacy/unsubscribe links on a domain the owner does not own. It is now `""`, so the gate refuses
  BY NAME listing `website_url` as missing.
- **D9:** `list_history` used `int(total or 0)` — a fabricated 0 if the count query ever returned no
  row. It now refuses `E_DB` by name, mirroring `run_service.count_runs`.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.core.errors import E_CONFIG, E_DB, PolsiaError
from app.services import chat_service, email_compliance

SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_company.py"


def _seed_module():
    """Load the seed script as a module (it is a script, not part of a package)."""
    spec = importlib.util.spec_from_file_location("seed_company_v22", SEED_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_seed_writes_no_kpi_that_nobody_measured():
    company = _seed_module().default_company()

    assert company.kpis == {}, (
        "a zero KPI is a measurement the owner never made; the seed must leave the map empty so the "
        "UI renders the honest dash"
    )


def test_the_seed_website_is_empty_so_compliance_refuses_by_name_instead_of_shipping_a_placeholder():
    company = _seed_module().default_company()

    assert company.website_url == "", "a placeholder domain must never satisfy the compliance gate"

    with pytest.raises(PolsiaError) as excinfo:
        email_compliance.footer_for_company(company)

    error = excinfo.value
    assert error.code == E_CONFIG
    assert "website_url" in error.context["missing"], (
        "the refusal must name the missing website_url so the owner knows exactly what to set"
    )


@pytest.mark.asyncio
async def test_chat_history_total_of_none_refuses_by_name_not_a_fabricated_zero(monkeypatch):
    async def _no_rows(*args, **kwargs):
        return []

    async def _no_total(*args, **kwargs):
        return None  # the driver broke its own contract: SELECT count(*) always yields one row

    monkeypatch.setattr(chat_service, "fetch_scalars", _no_rows)
    monkeypatch.setattr(chat_service, "fetch_scalar", _no_total)

    with pytest.raises(PolsiaError) as excinfo:
        await chat_service.list_history(None)

    error = excinfo.value
    assert error.code == E_DB
    assert "chat history total" in str(error.context)
