"""The 50-row pricing catalog (V9 B2): loaded ONCE, searched, and read by exact service name.

The dataset is the frozen conversion of R3's research table
(`polsia-work/build/ui-reverse/u6-research/biz-economics/RESEARCH.md`, 50 rows, fetch date
2026-10-14) — see `backend/app/data/pricing_catalog.json`.  Prices are market-rate CLAIMS with their
source URL and fetch type (F = full page fetched, S = search-excerpt retrieval; the S rows stay S
and are never upgraded).  Nothing here is LLM-invented: every planning action reads these rows.

IT IS A DATED SNAPSHOT, NOT LIVE PRICING.  Market rates fluctuate; this file does not track them.
Every catalog-derived response therefore carries computed provenance — `catalog_as_of` (the newest
row date, null when no row states one), `staleness_days` (today minus that date, null when
unknown) and `stale` (age at/over `CATALOG_STALENESS_THRESHOLD_DAYS`, null when unknown) — so no
surface presents the snapshot as current truth, and refreshing it is a deliberate re-generation
from the research file, never an edit in place.

Tax constants (S1 #6, PLAYBOOK §1.5/§1.6): the persistent disclaimer line, the Minnesota
digital-goods-vs-services note, the federal safe-harbor note, and the quarterly estimated-tax
date schedule.  These are text/notes, never measurements.
"""
from __future__ import annotations

import functools
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from app.core.errors import E_CONFIG, ERROR_HINTS, PolsiaError
from app.core.timeutil import today_in

#: The frozen dataset (bare list of 50 row objects; shape pinned by test_v9_catalog.py).
# V9 settle (V9-D7): package data, NOT under /app/data — that path is the backend_data runtime
# volume, which shadows whatever the image ships there (the catalog 503'd live until relocated).
CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "pricing_catalog.json"

#: How old the dated snapshot may get before catalog-derived responses call it stale (days).
CATALOG_STALENESS_THRESHOLD_DAYS = 180

#: The one persistent line every tax-bearing surface carries (plan §9 B2(f), S1 #6).
TAX_DISCLAIMER = "Not tax advice — verify with MN Revenue."

SALES_TAX_NOTE = (
    "Sales tax (Minnesota): digital goods are taxable as digital products (courses, downloads, "
    "prewritten software); subscriptions that only use online-hosted software and custom software "
    "fees are not; services are generally outside Minnesota sales tax unless specifically "
    "enumerated — confirm your mix with MN Revenue before collecting. Remote sellers: register "
    "after 200+ retail sales or $100,000+ into Minnesota over 12 months; an operator based in "
    "Minnesota registers from the start."
)

SAFE_HARBOR_NOTE = (
    "Federal estimated-tax safe harbor: owe under $1,000 after withholding/credits, or pay at "
    "least 90% of this year's tax or 100% of last year's (whichever is smaller) to avoid the "
    "underpayment penalty (IRS)."
)

#: IRS estimated-tax periods: Jan 1–Mar 31 -> Apr 15; Apr 1–May 31 -> Jun 15;
#: Jun 1–Aug 31 -> Sep 15; Sep 1–Dec 31 -> Jan 15 (PLAYBOOK §1.5).
_QUARTERLY_TAX_DAY = ((1, 15), (4, 15), (6, 15), (9, 15))

_MIN_TOKEN = re.compile(r"[a-z0-9]{3,}")


@functools.lru_cache(maxsize=1)
def load_catalog() -> tuple[dict[str, Any], ...]:
    """The catalog, loaded ONCE per process (lru_cache): the same tuple object every call.

    A missing/unreadable/malformed file is a named `E_CONFIG` refusal — this dataset is an input
    the planning views cannot silently run without (an empty list would fabricate a zero-row
    catalog).  Row-count drift (>0 but != 50) refuses the same way: the frozen contract is
    exactly 50 rows, and serving a partial catalog would quietly change every average it feeds.
    """
    try:
        rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PolsiaError(
            E_CONFIG,
            f"pricing catalog is unreadable at {CATALOG_PATH}: {exc}",
            how_to_fix=ERROR_HINTS[E_CONFIG],
            context={"path": str(CATALOG_PATH)},
        ) from exc
    except ValueError as exc:
        raise PolsiaError(
            E_CONFIG,
            f"pricing catalog is not valid JSON: {exc}",
            how_to_fix=ERROR_HINTS[E_CONFIG],
            context={"path": str(CATALOG_PATH)},
        ) from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise PolsiaError(
            E_CONFIG,
            "pricing catalog must be a JSON list of row objects",
            how_to_fix=ERROR_HINTS[E_CONFIG],
            context={"path": str(CATALOG_PATH)},
        )
    if len(rows) != 50:
        raise PolsiaError(
            E_CONFIG,
            f"pricing catalog must carry exactly 50 rows, found {len(rows)}",
            how_to_fix=ERROR_HINTS[E_CONFIG],
            context={"path": str(CATALOG_PATH), "rows": len(rows)},
        )
    bad_rows = [
        index
        for index, row in enumerate(rows)
        if not isinstance(row.get("service"), str) or not str(row["service"]).strip()
    ]
    if bad_rows:
        # F23 (V12/W-C): `search()`/`get()` index `row["service"]` — a row missing it raised a
        # bare KeyError mid-request; refuse at LOAD, naming the rows, like every other
        # malformed-catalog state.
        raise PolsiaError(
            E_CONFIG,
            f"pricing catalog rows must carry a non-empty 'service' (malformed rows: {bad_rows[:5]})",
            how_to_fix=ERROR_HINTS[E_CONFIG],
            context={"path": str(CATALOG_PATH), "rows": bad_rows[:5]},
        )
    return tuple(rows)


def clear_catalog_cache() -> None:
    """Drop the process-lifetime catalog cache (F23/V12-W-C) so ops can re-read a fixed file
    without a restart; the next `load_catalog()` re-reads from disk and re-validates."""
    load_catalog.cache_clear()


def search(query: str | None, limit: int = 50) -> list[dict[str, Any]]:
    """Catalog rows whose `service` contains `query` (case-insensitive), capped at `limit`.

    Prefix matches rank first, then file order — deterministic, no fuzzy scoring that could
    hand back a row the query never named.  An empty/absent query BROWSEs the whole dated
    snapshot in file order (the default limit covers all 50 rows; an explicit smaller limit is
    honoured).
    """
    rows = load_catalog()
    text = (query or "").strip().lower()
    if not text:
        return [dict(row) for row in rows[:limit]]
    matches = [(index, row) for index, row in enumerate(rows) if text in str(row["service"]).lower()]
    matches.sort(key=lambda item: (0 if str(item[1]["service"]).lower().startswith(text) else 1, item[0]))
    return [dict(row) for _, row in matches[:limit]]


def catalog_provenance(today: date | None = None) -> dict[str, Any]:
    """`{catalog_as_of, staleness_days, stale}` — COMPUTED per call, never stored or hand-edited.

    `catalog_as_of` is the newest row date in the snapshot (null when no row states one);
    `staleness_days` is today minus that date (null when the date is unknown); `stale` is True
    once the snapshot is at or over `CATALOG_STALENESS_THRESHOLD_DAYS` old — and null, not False,
    when the age cannot be computed (an unknown age is not a freshness claim).
    """
    rows = load_catalog()
    dates = sorted(
        {str(row["date"]) for row in rows if isinstance(row.get("date"), str) and row.get("date")}
    )
    as_of = dates[-1] if dates else None
    if as_of is None:
        return {"catalog_as_of": None, "staleness_days": None, "stale": None}
    day = today if today is not None else today_in()
    try:
        age = (day - date.fromisoformat(as_of)).days
    except ValueError:
        # F24 (V12/W-C): the newest date IS known — it is just unparseable; reporting
        # catalog_as_of=None erased it identically to a snapshot with NO dates at all.
        return {
            "catalog_as_of": as_of,
            "staleness_days": None,
            "stale": None,
            "reason": f"catalog_as_of {as_of!r} is not parseable as a date",
        }
    return {
        "catalog_as_of": as_of,
        "staleness_days": age,
        "stale": age >= CATALOG_STALENESS_THRESHOLD_DAYS,
    }


def get(service: str) -> dict[str, Any] | None:
    """The row with this exact service name (case-insensitive), or `None` — never a guess."""
    if not service:
        return None
    text = service.strip().lower()
    for row in load_catalog():
        if str(row["service"]).lower() == text:
            return dict(row)
    return None


def match_idea_row(idea_name: str | None) -> dict[str, Any] | None:
    """The single catalog row an idea name resolves to, or `None` when it does not resolve.

    Order: exact name -> first row whose service CONTAINS the idea name -> best token overlap
    (first row wins ties).  Deterministic and traceable: the caller reports which row matched, so
    an hours figure is always attributable to a named row — never an average of several.
    """
    text = (idea_name or "").strip().lower()
    if not text:
        return None
    rows = load_catalog()
    for row in rows:
        if str(row["service"]).lower() == text:
            return dict(row)
    for row in rows:
        if text in str(row["service"]).lower():
            return dict(row)
    wanted = set(_MIN_TOKEN.findall(text))
    best: dict[str, Any] | None = None
    best_overlap = 0
    for row in rows:
        overlap = len(wanted & set(_MIN_TOKEN.findall(str(row["service"]).lower())))
        if overlap > best_overlap:
            best, best_overlap = dict(row), overlap
    return best if best_overlap >= 1 else None


def taxable_hint(service: str) -> str | None:
    """The row's MN product-type hint (`service` | `digital_good`), or `None` when unknown."""
    row = get(service)
    if row is None:
        return None
    hint = row.get("taxable_hint")
    return str(hint) if isinstance(hint, str) else None


def next_quarterly_tax_dates(today: date) -> list[date]:
    """The next 4 quarterly estimated-tax due dates after `today` (IRS schedule, PLAYBOOK §1.5)."""
    candidates = [
        date(year, month, day)
        for year in (today.year, today.year + 1)
        for month, day in _QUARTERLY_TAX_DAY
        if date(year, month, day) > today
    ]
    candidates.sort()
    return candidates[:4]


__all__ = [
    "CATALOG_PATH",
    "CATALOG_STALENESS_THRESHOLD_DAYS",
    "SAFE_HARBOR_NOTE",
    "SALES_TAX_NOTE",
    "TAX_DISCLAIMER",
    "catalog_provenance",
    "get",
    "load_catalog",
    "match_idea_row",
    "next_quarterly_tax_dates",
    "search",
    "taxable_hint",
]
