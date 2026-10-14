"""V9 B2 (V9-D5): the 50-row pricing catalog — a DATED SNAPSHOT of R3's research table.

It is NOT live pricing: market rates fluctuate and this dataset does not track them.  Pinned here:
the row count (exactly 50), the frozen field set per row, F/S labels EXACTLY as the research file
labels them (its own footnote split: 41 F / 9 S), the V9-D5 price-parse rules (avg X -> X/X;
ranges -> low/high; multi-tier rows -> the FIRST stated band; an unparseable row keeps raw text in
an additive `price_text` with null numerics), hours ONLY where the row's prose states workload
hours (rows 4 and 5), and the file's own fetch date — never invented.
"""
from __future__ import annotations

import json
from pathlib import Path

# V9 settle (V9-D7): the catalog relocated into the package (app/data) — /app/data is the runtime
# volume in the container and the repo-root data/ is dockerignored, so the plan's original path
# could never ship. The tests assert the SERVICE's file, so they read the same constant.
CATALOG_PATH = Path(__file__).resolve().parents[2] / "app" / "data" / "pricing_catalog.json"

FROZEN_FIELDS = ("service", "price_low", "price_high", "model", "hours", "source_url", "fetch_type", "date")
ADDITIVE_FIELDS = {"price_text", "source_label", "taxable_hint"}

# The research footnote's own S split (RESEARCH.md line 109) — by service name, not by position.
S_SERVICES = {
    "Social media — premium full-service program",
    "Social media — beginner package",
    "Sales page copy (direct response)",
    "Copywriting retainer",
    "Bookkeeping — fully autonomous AI (Synthetic)",
    "Virtual assistant — managed service",
    "AI voice agent (Rosie)",
    "Done-for-you sales funnel build",
    "Amazon FBA account management",
}


def _rows() -> list[dict[str, object]]:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, list), "the catalog is a bare list of rows (len == row count)"
    return data


def test_catalog_is_exactly_50_rows() -> None:
    assert len(_rows()) == 50


def test_rows_carry_the_frozen_fields_and_no_others() -> None:
    for index, row in enumerate(_rows(), start=1):
        assert set(row) <= set(FROZEN_FIELDS) | ADDITIVE_FIELDS, f"row {index} has unexpected fields: {set(row)}"
        assert set(FROZEN_FIELDS) <= set(row), f"row {index} is missing frozen fields: {set(FROZEN_FIELDS) - set(row)}"
        assert row["service"] and isinstance(row["service"], str)
        assert row["model"] and isinstance(row["model"], str)
        assert row["fetch_type"] in {"F", "S"}
        assert isinstance(row["source_label"], str) and row["source_label"]


def test_fetch_labels_are_exact_f_or_s_with_the_research_files_own_split() -> None:
    rows = _rows()
    f_count = sum(1 for row in rows if row["fetch_type"] == "F")
    s_rows = {row["service"] for row in rows if row["fetch_type"] == "S"}
    assert (f_count, len(s_rows)) == (41, 9), "the research footnote's own split is 41 F / 9 S"
    assert s_rows == S_SERVICES, f"S-label drift: {s_rows ^ S_SERVICES}"


def test_price_bands_parse_or_carry_raw_text() -> None:
    for index, row in enumerate(_rows(), start=1):
        low, high = row["price_low"], row["price_high"]
        assert (low is None) == (high is None), f"row {index}: half-parsed price band"
        if low is None:
            assert row.get("price_text"), f"row {index}: null numerics without raw price text"
        else:
            assert 0 <= low <= high, f"row {index}: impossible band {low!r}..{high!r}"


def test_hours_only_where_the_prose_states_them() -> None:
    rows = _rows()
    by_service = {str(row["service"]): row for row in rows}
    assert by_service["SEO — small business"]["hours"] == {"low": 15, "high": 25}
    assert by_service["SEO — mid-market / advanced"]["hours"] == {"low": 25, "high": 60}
    stated = {str(row["service"]) for row in rows if row["hours"] is not None}
    assert stated == {"SEO — small business", "SEO — mid-market / advanced"}, "hours invented somewhere"


def test_every_row_dates_to_the_research_files_fetch_date() -> None:
    assert {row["date"] for row in _rows()} == {"2026-10-14"}


def test_source_urls_are_urls_and_the_raw_label_is_preserved() -> None:
    for index, row in enumerate(_rows(), start=1):
        assert str(row["source_url"]).startswith("http"), f"row {index}: source_url is not a URL"
        assert str(row["source_url"]) in str(row["source_label"]), f"row {index}: label lost the URL"


def test_taxable_hints_name_the_mn_digital_goods_vs_services_distinction() -> None:
    hints = {row["taxable_hint"] for row in _rows()}
    assert hints <= {"service", "digital_good"}
    by_service = {str(row["service"]): row for row in _rows()}
    assert by_service["AI logo maker (self-serve)"]["taxable_hint"] == "digital_good"
