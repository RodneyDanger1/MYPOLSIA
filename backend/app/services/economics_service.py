"""Economics context service — HONEST macro context for money/planning surfaces.

NOT a live-prices oracle: no public API publishes live service-market prices; this service
provides MACRO context ONLY — annual US inflation, the monthly US CPI index, and ECB-reference
FX rates — each section carrying its exact source URL and its own fetched_at timestamp.

The three keyless sources (all curl-probed from the build box on 2026-10-14; raw excerpts in
`polsia-work/build/ui-reverse/wave-v9/b7-economics/raw/`):

  * World Bank API v2, indicator FP.CPI.TOTL.ZG (annual US inflation, %) — a two-element
    `[meta, rows]` array; rows arrive newest-first; **the newest year's value can be null**
    (2025 is null today).  Served as `value: null` + reason — NEVER coerced to 0, never swapped
    for an older year (the pinned behaviour, test 2).
  * FRED public CSV, series CPIAUCSL (monthly US CPI-U index, SA) — rows ascending; the LATEST
    observation row is served; a malformed latest row is a NAMED failure, not a guess.
  * frankfurter.dev v1 (ECB reference rates, ~16:00 CET working days) — the api.frankfurter.app
    host 301-redirects, so the `.dev/v1` URL below is the one that answers directly.

Nothing here demands an API key by design; a source that ever starts demanding one is a refusal
by name, not a key configured into this module.

Honesty contract (wire shape frozen in `api/v1/economics.py`):

  * every section is either populated `{..., source, fetched_at}` or `null` with a
    `<section>_reason` string that IS literally `"<ExceptionClass>: <message>"`;
  * a source failure never crashes the response and never touches sibling sections;
  * a cache hit returns the ORIGINAL payload INCLUDING its fetched_at — the stamp is provenance,
    not decoration, and is never refreshed to look newer;
  * a null is always allowed and always explained.

Cache reasoning (the brief asks for it recorded): ONE module dict, plain assignments made
between awaits — no lock.  The app runs a single asyncio event loop per worker: CPython dict
writes are atomic under the GIL, so no torn entry is possible; the only race (two concurrent
first-callers both fetching, the later store winning) leaves two HONEST payloads racing, each
stamped with its own fetched_at.  An asyncio.Lock would serialize every request behind one slow
source and buy nothing this data can lose.  TTL = 6h (module constant; config.py untouched):
annual/monthly/daily cadences mean ≤6h-old data labeled with its own fetched_at is context, not
staleness.  FAILURES ARE NOT CACHED — a null section is retried on the next call, so a recovered
source shows up immediately instead of being hidden for six hours.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: The in-process TTL for a fetched section payload (frozen by the brief: 6 hours).
CACHE_TTL_SECONDS = 6 * 60 * 60

#: Per-source network timeout (frozen by the brief: 10 seconds each).
SOURCE_TIMEOUT_SECONDS = 10.0

WORLD_BANK_INFLATION_URL = (
    "https://api.worldbank.org/v2/country/USA/indicator/FP.CPI.TOTL.ZG?format=json&per_page=5"
)
FRED_CPI_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL"
FRANKFURTER_LATEST_URL = "https://api.frankfurter.dev/v1/latest?base=USD"

#: The small fixed FX set the contract serves (filtered from frankfurter's ~30 rates).
FX_CURRENCIES = ("EUR", "GBP", "CAD", "AUD")

#: section name -> (stored_at, payload).  Payloads keep their ORIGINAL fetched_at string.
_CACHE: dict[str, tuple[datetime, dict[str, Any]]] = {}


class SourceFetchError(Exception):
    """A source call that could not produce a payload — the reason string names this class."""


class MalformedSourcePayload(Exception):
    """A source answered outside the documented shape — named, never guessed around."""


def now_utc() -> datetime:
    """The service clock: ONE source of truth (tests monkeypatch THIS name for a fake clock)."""
    return datetime.now(UTC)


async def _get(url: str, *, accept: str) -> httpx.Response:
    """One GET with the frozen 10s timeout, phrased in the house vocabulary.

    Same pattern as `secret_store.verify_deepseek_key`: the timeout and transport failures are
    re-raised as THIS module's named error carrying the real cause in the message; a non-200 is
    its own named failure.  The original exception travels in the `__cause__` chain.
    """
    try:
        async with httpx.AsyncClient(timeout=SOURCE_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers={"Accept": accept})
    except httpx.TimeoutException as exc:
        raise SourceFetchError(
            f"timed out after {SOURCE_TIMEOUT_SECONDS:g}s calling GET {url}"
        ) from exc
    except httpx.HTTPError as exc:
        raise SourceFetchError(f"could not reach {url}: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise SourceFetchError(f"HTTP {response.status_code} from GET {url}")
    return response


def _parse_world_bank(payload: Any) -> dict[str, Any]:
    """`[meta, rows]` -> the NEWEST year's row, exactly as reported (null included).

    The newest year is selected by max(date), not by position — the API happens to answer
    newest-first, but trust the value, not the ordering.  A null newest value is returned as
    `value: None` with a reason; an older year is deliberately NOT substituted (test 2).
    """
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
        raise MalformedSourcePayload("World Bank payload is not the documented [meta, rows] array")
    dated: list[tuple[int, dict[str, Any]]] = []
    for row in payload[1]:
        if not isinstance(row, dict):
            continue
        try:
            year = int(str(row.get("date")))
        except ValueError:
            continue
        dated.append((year, row))
    if not dated:
        raise MalformedSourcePayload("World Bank payload carries no dated rows")
    year, newest = max(dated, key=lambda item: item[0])
    value = newest.get("value")
    if value is None:
        return {
            "value": None,
            "period": str(year),
            "reason": (
                f"World Bank reports no value for {year} — the newest year is not yet reported; "
                "no older year is substituted (never 0)"
            ),
        }
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise MalformedSourcePayload(
            f"World Bank value for {year} is {value!r}, not a finite number or null"
        )
    return {"value": float(value), "period": str(year), "reason": None}


def _parse_fred_csv(text: str) -> dict[str, Any]:
    """The FRED CSV -> the LATEST observation, or a named failure — never an older row.

    Rows are ascending, so the latest observation is the last data row.  A latest row that is
    not a dated numeric observation (garbage value, FRED's '.' marker, a truncated line) fails
    honestly instead of silently serving a stale-but-parseable earlier row.
    """
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if not rows:
        raise MalformedSourcePayload("FRED CSV is empty")
    header = [cell.strip() for cell in rows[0]]
    if header != ["observation_date", "CPIAUCSL"]:
        raise MalformedSourcePayload(
            f"FRED CSV header is {rows[0]!r}, expected ['observation_date', 'CPIAUCSL']"
        )
    if len(rows) < 2:
        raise MalformedSourcePayload("FRED CSV has a header but no data rows")
    latest = rows[-1]
    observation_date = latest[0].strip() if latest else ""
    if len(latest) < 2 or not observation_date:
        raise MalformedSourcePayload(f"FRED CSV latest row {latest!r} is not a dated observation")
    raw_value = latest[1].strip()
    try:
        index_value = float(raw_value)
    except ValueError as exc:
        raise MalformedSourcePayload(
            f"FRED CSV latest row {latest!r}: {raw_value!r} is not a number"
        ) from exc
    if not math.isfinite(index_value):
        raise MalformedSourcePayload(
            f"FRED CSV latest row {latest!r}: {raw_value!r} is not finite"
        )
    return {
        "index_value": index_value,
        "observation_date": observation_date,
        "reason": None,
    }


def _parse_frankfurter(payload: Any) -> dict[str, Any]:
    """The frankfurter document -> the contracted USD-based rate set (a filtered subset).

    Currencies the response omitted stay omitted from `rates` and are NAMED in `reason` — the
    consumer sees fewer keys and why, never a substituted or invented rate.
    """
    if not isinstance(payload, dict) or payload.get("base") != "USD":
        raise MalformedSourcePayload("frankfurter payload is not a USD-based rates document")
    date = payload.get("date")
    rates_document = payload.get("rates")
    if not isinstance(date, str) or not date or not isinstance(rates_document, dict):
        raise MalformedSourcePayload("frankfurter payload carries no date/rates")
    rates: dict[str, float] = {}
    for currency in FX_CURRENCIES:
        value = rates_document.get(currency)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            rates[currency] = float(value)
    if not rates:
        raise MalformedSourcePayload(
            f"frankfurter payload carries none of the contracted currencies {list(FX_CURRENCIES)}"
        )
    missing = [currency for currency in FX_CURRENCIES if currency not in rates]
    return {
        "usd_base": True,  # enforced above: base == "USD"
        "date": date,
        "rates": rates,
        "reason": f"frankfurter response omitted: {', '.join(missing)}" if missing else None,
    }


async def _fetch_inflation() -> dict[str, Any]:
    """World Bank annual US inflation — the newest reported year, honestly null when null."""
    response = await _get(WORLD_BANK_INFLATION_URL, accept="application/json")
    parsed = _parse_world_bank(response.json())
    return {**parsed, "source": WORLD_BANK_INFLATION_URL, "fetched_at": now_utc().isoformat()}


async def _fetch_fred_cpi() -> dict[str, Any]:
    """FRED monthly US CPI index (CPIAUCSL) — the latest observation row."""
    response = await _get(FRED_CPI_CSV_URL, accept="text/csv")
    parsed = _parse_fred_csv(response.text)
    return {**parsed, "source": FRED_CPI_CSV_URL, "fetched_at": now_utc().isoformat()}


async def _fetch_frankfurter() -> dict[str, Any]:
    """frankfurter.dev USD-base rates — the contracted currency subset."""
    response = await _get(FRANKFURTER_LATEST_URL, accept="application/json")
    parsed = _parse_frankfurter(response.json())
    return {**parsed, "source": FRANKFURTER_LATEST_URL, "fetched_at": now_utc().isoformat()}


_SECTION_FETCHERS: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {
    "inflation": _fetch_inflation,
    "fx": _fetch_frankfurter,
    "cpi": _fetch_fred_cpi,
}


def _cached_payload(section: str) -> dict[str, Any] | None:
    """The stored payload while STRICTLY inside the TTL, else None (at the boundary: expired)."""
    entry = _CACHE.get(section)
    if entry is None:
        return None
    stored_at, payload = entry
    if (now_utc() - stored_at).total_seconds() < CACHE_TTL_SECONDS:
        return payload
    return None


async def _serve_section(
    section: str, fetch: Callable[[], Awaitable[dict[str, Any]]]
) -> tuple[str, dict[str, Any] | None, str | None]:
    """One section: cached payload, or a fresh fetch, or (None, "ExceptionClass: message")."""
    cached = _cached_payload(section)
    if cached is not None:
        return section, cached, None
    try:
        payload = await fetch()
    except Exception as exc:  # per-source isolation: every failure is NAMED in the reason
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning("economics source %r unavailable: %s", section, reason)
        return section, None, reason
    _CACHE[section] = (now_utc(), payload)
    return section, payload, None


async def get_context() -> dict[str, Any]:
    """The full honest macro context: three independent sections, fetched concurrently.

    `generated_at` is THIS response's clock; each section keeps its own fetched_at provenance.
    A section that fails is null + `<section>_reason`; its siblings serve normally.
    """
    sections = list(
        await asyncio.gather(
            *(_serve_section(name, fetch) for name, fetch in _SECTION_FETCHERS.items())
        )
    )
    context: dict[str, Any] = {"generated_at": now_utc().isoformat()}
    for name, payload, reason in sections:
        context[name] = payload
        context[f"{name}_reason"] = reason
    return context


__all__ = [
    "CACHE_TTL_SECONDS",
    "FRANKFURTER_LATEST_URL",
    "FRED_CPI_CSV_URL",
    "FX_CURRENCIES",
    "SOURCE_TIMEOUT_SECONDS",
    "WORLD_BANK_INFLATION_URL",
    "MalformedSourcePayload",
    "SourceFetchError",
    "get_context",
    "now_utc",
]
