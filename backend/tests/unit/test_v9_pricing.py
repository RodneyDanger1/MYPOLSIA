"""V9 B2: `pricing_service` — the catalog loaded once, searched, and read by exact service name.

Plus the tax constants the planning/cash-block outputs carry (S1 #6): the persistent
"Not tax advice — verify with MN Revenue." line, and the quarterly estimated-tax date schedule
(IRS periods: Jan 1–Mar 31 -> Apr 15; Apr 1–May 31 -> Jun 15; Jun 1–Aug 31 -> Sep 15;
Sep 1–Dec 31 -> Jan 15 — PLAYBOOK §1.5).
"""
from __future__ import annotations

from datetime import date

from app.services import pricing_service


def test_search_matches_service_names_case_insensitively() -> None:
    rows = pricing_service.search("seo")
    assert rows, "the catalog carries SEO rows"
    assert all("seo" in str(row["service"]).lower() for row in rows)


def test_search_respects_the_limit() -> None:
    assert len(pricing_service.search("seo", limit=2)) == 2


def test_empty_query_browses_the_catalog() -> None:
    rows = pricing_service.search("")
    assert len(rows) == 50
    assert len(pricing_service.search("", limit=5)) == 5


def test_get_is_exact_and_case_insensitive() -> None:
    row = pricing_service.get("local seo — single location")
    assert row is not None
    assert row["price_low"] == 500 and row["price_high"] == 1200


def test_get_unknown_service_returns_none() -> None:
    assert pricing_service.get("no such service in this catalog") is None


def test_catalog_loads_once() -> None:
    assert pricing_service.load_catalog() is pricing_service.load_catalog()


def test_tax_constants_carry_the_persistent_mn_line() -> None:
    assert pricing_service.TAX_DISCLAIMER == "Not tax advice — verify with MN Revenue."
    assert "Minnesota" in pricing_service.SALES_TAX_NOTE or "MN" in pricing_service.SALES_TAX_NOTE
    assert "90%" in pricing_service.SAFE_HARBOR_NOTE


def test_next_quarterly_tax_dates_is_deterministic() -> None:
    dates = pricing_service.next_quarterly_tax_dates(date(2026, 10, 14))
    assert dates == [
        date(2027, 1, 15),
        date(2027, 4, 15),
        date(2027, 6, 15),
        date(2027, 9, 15),
    ]
    assert pricing_service.next_quarterly_tax_dates(date(2026, 4, 15))[0] == date(2026, 6, 15)


def test_idea_row_match_is_deterministic_and_honest() -> None:
    exact = pricing_service.match_idea_row("SEO — small business")
    assert exact is not None and exact["hours"] == {"low": 15, "high": 25}
    assert pricing_service.match_idea_row("") is None
    assert pricing_service.match_idea_row("zzz no overlap") is None


def test_catalog_provenance_is_computed_and_dated_not_live(monkeypatch) -> None:
    """The catalog is a DATED SNAPSHOT: age + staleness are computed against the 180-day
    threshold, and an unknown as_of answers nulls — never a freshness claim."""
    from datetime import date

    assert pricing_service.CATALOG_STALENESS_THRESHOLD_DAYS == 180
    fresh = pricing_service.catalog_provenance(date(2026, 10, 14))
    assert fresh == {"catalog_as_of": "2026-10-14", "staleness_days": 0, "stale": False}
    old = pricing_service.catalog_provenance(date(2027, 4, 14))  # 182 days after the fetch date
    assert old["staleness_days"] == 182
    assert old["stale"] is True

    monkeypatch.setattr(pricing_service, "load_catalog", lambda: ({"service": "x", "date": None},))
    assert pricing_service.catalog_provenance() == {
        "catalog_as_of": None,
        "staleness_days": None,
        "stale": None,
    }
