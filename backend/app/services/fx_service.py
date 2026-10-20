"""Frankfurter v2 currency conversion for DISPLAY (V29/L4) — keyless, free, no quota.

Why this module exists next to `economics_service`: that service is **async** and serves a
SECTION-cached macro context (`/economics/context`), while the V29 tool runners are **sync** by
contract (`tool_registry.execute_tool` calls a runner directly).  A sync adapter is therefore a
second, deliberately separate read path — not a duplicate: it caches per (base, quote) PAIR, it
serves one conversion, and it never touches the context endpoint's shape.  Do not merge the two
in this wave.

Source (live-probed from the build box, 2026-10-20)::

    GET https://api.frankfurter.dev/v2/rate/EUR/USD
    -> {"date":"2026-10-20","base":"EUR","quote":"USD","rate":1.1236}

Keyless, open source, no quotas (https://frankfurter.dev/llms.txt).  An UNKNOWN currency answers
**HTTP 422** `{"status":422,"message":"invalid currency: ZZZ"}` — measured, not assumed; the plan
sketched a 404.  Both statuses are the SAME caller error here (`E_VALIDATION`): the caller typed
the code, so the caller is told, and the provider is never blamed for a typo.

Honesty rules, in code:

* a code must match `[A-Z]{3}` (case-insensitively normalised) and an amount must be a finite,
  non-negative number — refused as `E_VALIDATION` BEFORE any socket opens;
* the identity pair (`EUR` -> `EUR`) answers `rate: 1.0`, `source: "identity"`, `date: null` and
  makes no network call: there is no market for converting a currency into itself;
* the provider's own pair is checked against the request (`base`/`quote`) and a zero, negative,
  non-numeric or missing rate is `E_UPSTREAM` — a conversion is never invented from a bad rate;
* the cache TTL is 6 h and holds at most 64 pairs; a HIT keeps the ORIGINAL `fetched_at` and the
  market `date` (provenance is never restamped), and failures are NEVER cached (the next call
  retries a recovered source immediately);
* cents are quantised with `ROUND_HALF_UP` on `Decimal`, so `1234.56 EUR @ 1.1236` is exactly
  `1387.15` — no binary-float rounding drift in a displayed money figure.

CONVERSION IS DISPLAY-ONLY.  Nothing here writes an invoice: `invoice_service` stores the
amount and its currency verbatim, and this module only answers "what would that be worth".
"""
from __future__ import annotations

import math
import re
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from app.core.errors import E_UPSTREAM, E_VALIDATION, PolsiaError
from app.core.timeutil import now_utc

#: The v2 pair endpoint (live-verified shape above; the v1 `/latest` document is NOT used here).
RATE_URL = "https://api.frankfurter.dev/v2/rate/{base}/{quote}"

#: Per-call network timeout: a display lookup must never hold a request open longer than this.
TIMEOUT_S = 8.0

#: In-process TTL for one pair's rate (the brief's 6 hours — ECB publishes once per working day).
CACHE_TTL_S = 6 * 3600.0

#: At most this many (base, quote) pairs are kept; the oldest entry is dropped past the cap.
CACHE_MAX_ENTRIES = 64

#: ISO 4217 codes as the contract spells them: exactly three ASCII letters, nothing else.
_CODE_RE = re.compile(r"[A-Z]{3}")

#: (base, quote) -> (monotonic store instant, rate payload).  Process-global like its siblings.
_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

#: The socket seam tests monkeypatch (`_CLIENT = <class with .get>`); product code never does.
_CLIENT = httpx.Client


def _code(value: object, field: str) -> str:
    """One ISO currency code, normalised — or the named `E_VALIDATION` refusal."""
    text = value.strip().upper() if isinstance(value, str) else ""
    if not _CODE_RE.fullmatch(text):
        raise PolsiaError(
            E_VALIDATION,
            f"{field} must be a 3-letter ISO currency code (got {value!r})",
            how_to_fix="use a code like USD, EUR, GBP, JPY",
            context={"field": field},
        )
    return text


def _amount(value: object) -> float:
    """A finite, non-negative amount — or the named `E_VALIDATION` refusal.

    `bool` is excluded on purpose: `True` is an `int` in Python and would otherwise convert as
    `1.0`.  NaN/inf are excluded because neither can be money, and `Decimal('nan')` would raise
    out of the money path instead of refusing in the house vocabulary.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise PolsiaError(
            E_VALIDATION,
            f"amount must be a non-negative finite number (got {value!r})",
            how_to_fix="pass amount >= 0, e.g. 1234.56",
            context={"field": "amount"},
        )
    return float(value)


def _cents(amount: float, rate: float) -> float:
    """`amount x rate` quantised to cents, half-up on Decimal (never binary-float rounding)."""
    converted = (Decimal(str(amount)) * Decimal(str(rate))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(converted)


def _fetch(frm: str, to: str) -> dict[str, Any]:
    """One rate read for `frm`/`to`; every failure is named, and nothing is cached here."""
    url = RATE_URL.format(base=frm, quote=to)
    try:
        with _CLIENT(timeout=TIMEOUT_S) as client:
            response = client.get(url)
    except httpx.TimeoutException as exc:
        raise PolsiaError(
            E_UPSTREAM,
            f"fx_convert: timed out after {TIMEOUT_S:g}s calling {url}",
            how_to_fix="retry once; if it persists the rate source is slow",
            context={"from": frm, "to": to},
        ) from exc
    except httpx.HTTPError as exc:
        raise PolsiaError(
            E_UPSTREAM,
            f"fx_convert: transport failure: {type(exc).__name__}",
            how_to_fix="check egress and retry once",
            context={"from": frm, "to": to},
        ) from exc

    if response.status_code in (404, 422):
        # 422 is what v2 ACTUALLY answers for an unknown currency (measured 2026-10-20); 404 is
        # kept for the endpoint's documented miss shape.  Both mean the same caller error.
        raise PolsiaError(
            E_VALIDATION,
            f"fx_convert: the rate source does not know the currency pair {frm}/{to}",
            how_to_fix="check the ISO 4217 codes; both must be currencies the ECB publishes",
            context={"from": frm, "to": to, "status": response.status_code},
        )
    if response.status_code != 200:
        raise PolsiaError(
            E_UPSTREAM,
            f"fx_convert: provider answered HTTP {response.status_code} for {frm}/{to}",
            how_to_fix="retry once; if it persists the free rate source is down",
            context={"from": frm, "to": to, "status": response.status_code},
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise PolsiaError(
            E_UPSTREAM,
            "fx_convert: provider returned a non-JSON body",
            how_to_fix="retry once",
            context={"from": frm, "to": to},
        ) from exc
    if not isinstance(body, dict):
        raise PolsiaError(
            E_UPSTREAM,
            "fx_convert: provider returned a malformed rate document",
            how_to_fix="retry once",
            context={"from": frm, "to": to},
        )

    raw_rate = body.get("rate")
    if (
        isinstance(raw_rate, bool)
        or not isinstance(raw_rate, (int, float))
        or not math.isfinite(float(raw_rate))
        or float(raw_rate) <= 0
    ):
        raise PolsiaError(
            E_UPSTREAM,
            f"fx_convert: provider returned a malformed rate for {frm}/{to}: {raw_rate!r}",
            how_to_fix="retry once; a zero or non-numeric rate is never converted",
            context={"from": frm, "to": to},
        )
    if body.get("base") != frm or body.get("quote") != to:
        raise PolsiaError(
            E_UPSTREAM,
            f"fx_convert: provider answered for {body.get('base')!r}/{body.get('quote')!r}, "
            f"not {frm}/{to}",
            how_to_fix="retry once; a rate for another pair is never used",
            context={"from": frm, "to": to},
        )

    market_date = body.get("date")
    payload: dict[str, Any] = {
        "rate": float(raw_rate),
        "date": market_date if isinstance(market_date, str) and market_date else None,
        "source": url,
        "fetched_at": now_utc().isoformat(),
        "cached": False,
    }
    _CACHE[(frm, to)] = (time.monotonic(), payload)
    if len(_CACHE) > CACHE_MAX_ENTRIES:
        _CACHE.pop(next(iter(_CACHE)))  # oldest insertion wins the door
    return payload


def convert(amount: float, base: str, quote: str) -> dict[str, Any]:
    """Convert `amount` from `base` to `quote` for DISPLAY; cached 6 h per pair.

    Returns `{amount, from, to, rate, converted, date, source, fetched_at, cached}`.  The identity
    pair answers without a network call.  Nothing is written anywhere: callers must not treat
    `converted` as a stored accounting figure.
    """
    value = _amount(amount)
    frm, to = _code(base, "from"), _code(quote, "to")

    if frm == to:
        return {
            "amount": value,
            "from": frm,
            "to": to,
            "rate": 1.0,
            "converted": _cents(value, 1.0),
            "date": None,  # no market exists for a same-currency conversion
            "source": "identity",
            "fetched_at": now_utc().isoformat(),
            "cached": False,
        }

    hit = _CACHE.get((frm, to))
    if hit is not None and time.monotonic() - hit[0] < CACHE_TTL_S:
        rate_payload = {**hit[1], "cached": True}  # fetched_at/date stay the ORIGINAL ones
    else:
        rate_payload = _fetch(frm, to)

    return {
        "amount": value,
        "from": frm,
        "to": to,
        **rate_payload,
        "converted": _cents(value, rate_payload["rate"]),
    }


__all__ = [
    "CACHE_MAX_ENTRIES",
    "CACHE_TTL_S",
    "RATE_URL",
    "TIMEOUT_S",
    "convert",
]
