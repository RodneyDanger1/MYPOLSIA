"""Recorded unit economics — the ONE honest seam between a supplier cost and the money agent.

THE MEASURED GAP THIS CLOSES (wave 3, run 313).  The finance agent was asked to cost the business
and refused, correctly: *"No supplier, packaging, label, or shipping-supply cost appears anywhere
in the context or memories: every COGS line for both candle and soap is UNMEASURED ... Stating a
quantity would be fabrication."*  The company profile carried `pricing_model` (retail prices) and
nothing carried a per-unit COST, so no contribution or break-even number could exist anywhere in
the system.  Every sourced supplier price the research lanes collected lived in documents the
agent cannot read.

THE SEAM.  A per-SKU cost may be RECORDED on the company profile beside the SKU it belongs to:

    pricing_model = {
      "currency": "USD",
      "skus": [
        {"sku": "CL-WSL-9", "name": "...", "retail": 26,
         "unit_cost": {"amount_cents": 949, "source": "https://printonic.com/...",
                       "observed_at": "2026-10-28", "kind": "sourced",
                       "note": "9 oz POD base price, before shipping"}},
        ...
      ],
      "payment_fee": {"percent": 2.9, "fixed_cents": 30, "kind": "sourced",
                      "source": "https://stripe.com/pricing", "observed_at": "2026-10-28"},
    }

and this module turns those inputs into (a) the finance agent's prompt block and (b) a derived
per-SKU arithmetic view.  NOTHING HERE INVENTS A NUMBER: a SKU with no recorded cost is rendered
UNMEASURED, its contribution and break-even are `None`, and the block says so.  A recorded cost is
CONFIGURED data (the owner/operator wrote it), never a runtime measurement — which is why every
line carries its own provenance and the derived lines are labelled DERIVED.

THE THREE COST CLASSES (only one of them is a fact about a supplier):

  * `sourced`   — the amount with a recorded source (URL or source key) AND the date it was
                  observed.  It is a DATED CLAIM: this module reports its age and flags it stale at
                  `COST_STALENESS_DAYS`, exactly as the pricing catalog flags its own snapshot.  It
                  is not a live check and it is not re-verified here.
  * `estimate`  — an assumption (freight, a packaged unit); legitimate for planning, never
                  presented as an observation.
  * `unsourced` — recorded with no source at all.  LEGACY tolerance only: the write boundary
                  (`schemas/company.py`) refuses to CREATE one, but a row stored before this seam
                  (the wave-2 trial wrote `unit_cost: 9.4` in DOLLARS) must still be rendered
                  truthfully rather than hidden.

THE WRITE BOUNDARY IS STRICT, THE READ EDGE IS TOLERANT.  `cost_input_errors()` is what
`CompanyConfigUpdate` runs so a `PUT /config` cannot store an ambiguous or unprovenanced cost
(a bare number is refused there because dollars-vs-cents cannot be guessed safely).  `recorded_*`
readers accept whatever is already stored, label it, and never raise: a junk value becomes a NAMED
problem line, not a crashed finance run.

WHAT THIS DOES NOT DO (named, not hidden): it does not fetch the supplier page, it does not verify
that a recorded source says what the recorder claims, it does not convert currencies (a SKU whose
cost currency differs from the catalogue's is NOT COMPUTABLE rather than FX-converted), and it does
not allocate shared costs (packaging, labour, tax) — those stay UNMEASURED unless recorded as part
of a unit cost.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, TypeGuard

from app.core.timeutil import today_in

#: The per-SKU cost key, INSIDE a catalogue row (beside `retail`).  A public catalogue reader must
#: never print it (`site_service` reads only name/sku/price/description — pinned by test).
UNIT_COST_KEY = "unit_cost"

#: The optional catalogue-level payment-fee input, under `pricing_model`.
PAYMENT_FEE_KEY = "payment_fee"

#: The provenance classes a recorded cost can carry (see the module docstring).
COST_SOURCED = "sourced"
COST_ESTIMATE = "estimate"
COST_UNSOURCED = "unsourced"
COST_KINDS: tuple[str, ...] = (COST_SOURCED, COST_ESTIMATE, COST_UNSOURCED)

#: The only cost basis this seam understands.  A per-ORDER or per-CASE figure recorded here would
#: silently divide by the wrong denominator, so it is refused by name rather than guessed around.
COST_BASE_PER_UNIT = "per_unit"
COST_BASES: tuple[str, ...] = (COST_BASE_PER_UNIT,)

#: Keys the provenance object may carry.  Anything else is a typo that would leave a real number
#: unread (`ammount_cents`), so the write boundary refuses it instead of silently ignoring it.
COST_OBJECT_KEYS: tuple[str, ...] = (
    "amount_cents",
    "currency",
    "basis",
    "kind",
    "source",
    "observed_at",
    "note",
)
FEE_OBJECT_KEYS: tuple[str, ...] = (
    "percent",
    "fixed_cents",
    "currency",
    "kind",
    "source",
    "observed_at",
    "note",
)

#: How old a sourced cost may get before the block calls it STALE (days).  The same 180-day rule
#: the dated pricing catalog uses (`pricing_service.CATALOG_STALENESS_THRESHOLD_DAYS`): a price is a
#: dated observation, and an old observation is not a current one.
COST_STALENESS_DAYS = 180

#: The catalogue list keys this module reads.  It deliberately MIRRORS `site_service`'s reader so
#: the row the public page renders and the row the money agent costs are the same row (a drift test
#: pins the equality); duplicated rather than imported because `site_service` is a 1,600-line HTML
#: renderer and a finance read has no business importing it.
CATALOGUE_LIST_KEYS: tuple[str, ...] = (
    "skus",
    "plans",
    "products",
    "items",
    "tiers",
    "packages",
    "offerings",
)
#: A row's display name — the same name-ish keys the storefront accepts (`sku` is an identifier).
_NAME_KEYS = ("name", "title", "label", "product")
#: The identifier slot (fallback when the row records no name at all).
_SKU_KEYS = ("sku",)
#: A selling price, in the row.  A cost is never read from these.
_PRICE_KEYS = ("retail", "price", "price_usd", "unit_price", "list_price", "amount")

#: Bounds on rendered free text, so a hostile/long note cannot turn the prompt block into a dump.
MAX_NAME_CHARS = 80
MAX_SOURCE_CHARS = 200
MAX_NOTE_CHARS = 160
MAX_CURRENCY_CHARS = 3


def _clean(value: Any) -> str:
    """One collapsed line of TEXT (``''`` for anything that is not a string).

    Mirrors `company_service._clean`/`site_service._bounded_text`: whitespace is collapsed so a
    recorded note cannot inject newlines into the prompt block, and ``<``/``>`` are neutralised so
    a recorded source URL cannot fabricate a quarantine delimiter the taint scan would then parse.
    """
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text.replace("<", "(").replace(">", ")")


def _bounded(value: Any, limit: int) -> str:
    """`_clean(value)` cut to `limit` characters with an ellipsis (never mid-word markup)."""
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _currency_code(value: Any, fallback: str) -> str | None:
    """A 3-letter ISO code, uppercased; `fallback` when absent/blank; ``None`` when malformed."""
    text = _clean(value).upper()
    if not text:
        return fallback
    if len(text) != MAX_CURRENCY_CHARS or not text.isalpha():
        return None
    return text


def _is_number(value: Any) -> TypeGuard[int | float]:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
    )


def _dollars_to_cents(value: Any) -> int | None:
    """A dollars-and-cents number (the wire/catalogue convention) as an exact integer of cents.

    `26` -> 2600, `9.49` -> 949, `19.999` -> 2000 (rounded half-up at the cent, like the fee).
    A bool, a string, a non-finite or negative number is ``None``: not a price.
    """
    if not _is_number(value) or float(value) < 0:
        return None
    try:
        amount = (Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    return int(amount)


def _cents(value: Any) -> int | None:
    """A recorded `amount_cents` as an int — integers only, positive only (a 0-cost is not a cost)."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if value > 0 else None


def _iso_date(value: Any) -> date | None:
    """`YYYY-MM-DD` (or a full ISO datetime, whose date is used) as a `date`; else ``None``."""
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _resolve_kind(raw_kind: Any, *, has_source: bool, has_date: bool) -> str | None:
    """The provenance class of a cost object — explicit when recorded, derived when not.

    An explicit `kind` wins.  Without one, a source AND an observation date make the line
    `sourced`; anything else is `unsourced`, because a number with no traceable origin is not a
    sourced price however it was typed.  An unknown `kind` is ``None`` (the caller names it).
    """
    if raw_kind is None:
        return COST_SOURCED if (has_source and has_date) else COST_UNSOURCED
    if not isinstance(raw_kind, str):
        return None
    kind = raw_kind.strip().lower()
    return kind if kind in COST_KINDS else None


# ---------------------------------------------------------------------------------------------
# the write boundary: called by `schemas/company.py::CompanyConfigUpdate`
# ---------------------------------------------------------------------------------------------


def _cost_object_errors(value: Any, path: str, currency: str) -> list[str]:
    """Every reason a recorded `unit_cost` object cannot be stored honestly (empty = valid)."""
    if not isinstance(value, dict):
        return [
            f"{path} is a bare {type(value).__name__} — a cost needs its provenance; record "
            '{"amount_cents": <int>, "kind": "sourced", "source": <url or source key>, '
            '"observed_at": "YYYY-MM-DD"} (or "kind": "estimate" for an assumption). A bare '
            "number is refused because dollars-vs-cents cannot be guessed safely"
        ]
    unknown = sorted(set(value) - set(COST_OBJECT_KEYS))
    errors: list[str] = []
    if unknown:
        errors.append(
            f"{path} carries unknown key(s) {unknown} (allowed: {list(COST_OBJECT_KEYS)}) — a "
            "misspelt cost key would leave the amount unread"
        )
    amount = _cents(value.get("amount_cents"))
    if amount is None:
        errors.append(
            f"{path}.amount_cents must be a positive INTEGER count of cents (got "
            f"{value.get('amount_cents')!r}) — a float here is ambiguous between dollars and cents"
        )
    basis = value.get("basis", COST_BASE_PER_UNIT)
    if basis not in COST_BASES:
        errors.append(
            f"{path}.basis is {basis!r}; only {list(COST_BASES)} is understood (a per-order or "
            "per-case figure here would divide by the wrong denominator)"
        )
    recorded_currency = _currency_code(value.get("currency"), currency)
    if recorded_currency is None:
        errors.append(f"{path}.currency must be a 3-letter ISO code")
    elif recorded_currency != currency:
        errors.append(
            f"{path}.currency is {recorded_currency!r} but the catalogue currency is "
            f"{currency!r} — this seam does not convert currencies, so one catalogue has one "
            "currency for its prices and its costs"
        )
    source = _clean(value.get("source"))
    observed = _iso_date(value.get("observed_at"))
    kind = _resolve_kind(
        value.get("kind"), has_source=bool(source), has_date=observed is not None
    )
    if kind is None:
        errors.append(
            f"{path}.kind is {value.get('kind')!r}; use one of {list(COST_KINDS)}"
        )
    elif kind == COST_SOURCED:
        if not source:
            errors.append(
                f'{path}.source is required when kind="sourced" — a sourced cost names where the '
                "number came from (the supplier page URL or a source key)"
            )
        if observed is None:
            errors.append(
                f'{path}.observed_at is required when kind="sourced" and must be an ISO date '
                f"(YYYY-MM-DD); got {value.get('observed_at')!r} — a price is a dated observation"
            )
    return errors


def _fee_object_errors(value: Any, path: str, currency: str) -> list[str]:
    """Every reason a recorded `payment_fee` cannot be stored honestly (empty = valid)."""
    if not isinstance(value, dict):
        return [
            f"{path} must be an object like "
            '{"percent": 2.9, "fixed_cents": 30, "kind": "sourced", "source": "...", '
            '"observed_at": "YYYY-MM-DD"}'
        ]
    unknown = sorted(set(value) - set(FEE_OBJECT_KEYS))
    errors: list[str] = []
    if unknown:
        errors.append(
            f"{path} carries unknown key(s) {unknown} (allowed: {list(FEE_OBJECT_KEYS)})"
        )
    percent = value.get("percent")
    fixed = value.get("fixed_cents")
    if percent is None and fixed is None:
        errors.append(
            f"{path} records neither percent nor fixed_cents — nothing to derive a fee from"
        )
    if percent is not None and (not _is_number(percent) or not (0 <= float(percent) <= 100)):
        errors.append(
            f"{path}.percent must be a finite number of percent between 0 and 100 (got "
            f"{percent!r})"
        )
    if fixed is not None and (not isinstance(fixed, int) or isinstance(fixed, bool) or fixed < 0):
        errors.append(
            f"{path}.fixed_cents must be a non-negative integer count of cents (got {fixed!r})"
        )
    recorded_currency = _currency_code(value.get("currency"), currency)
    if recorded_currency is None:
        errors.append(f"{path}.currency must be a 3-letter ISO code")
    elif recorded_currency != currency:
        errors.append(
            f"{path}.currency is {recorded_currency!r} but the catalogue currency is "
            f"{currency!r} — no conversion is performed"
        )
    source = _clean(value.get("source"))
    observed = _iso_date(value.get("observed_at"))
    kind = _resolve_kind(
        value.get("kind"), has_source=bool(source), has_date=observed is not None
    )
    if kind is None:
        errors.append(f"{path}.kind is {value.get('kind')!r}; use one of {list(COST_KINDS)}")
    elif kind == COST_SOURCED:
        if not source:
            errors.append(
                f'{path}.source is required when kind="sourced" (e.g. the provider pricing page)'
            )
        if observed is None:
            errors.append(
                f'{path}.observed_at is required when kind="sourced" and must be an ISO date; '
                f"got {value.get('observed_at')!r}"
            )
    return errors


def _catalogue_rows(pricing_model: Any) -> list[dict[str, Any]]:
    """The first recognised list of row objects in the pricing model (never invented)."""
    if not isinstance(pricing_model, dict):
        return []
    lists: dict[str, Any] = {str(key).lower(): value for key, value in pricing_model.items()}
    for list_key in CATALOGUE_LIST_KEYS:
        candidate = lists.get(list_key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def cost_input_errors(pricing_model: Any) -> list[str]:
    """Path-prefixed reasons a `pricing_model` cannot be stored (empty list = honest).

    Runs at the `PUT /config` boundary (through `CompanyConfigUpdate`'s model validator), so a
    malformed cost is a 422 that NAMES the offending path instead of a stored value every reader
    has to guess about.  A `unit_cost: null` is legal (clears the cost); an absent key is legal
    (unmeasured); any other shape must satisfy the rules above.
    """
    if not isinstance(pricing_model, dict):
        return []
    currency = _currency_code(pricing_model.get("currency"), "USD")
    if currency is None:
        return [
            "pricing_model.currency must be a 3-letter ISO code (costs are compared to it; no "
            "conversion is performed)"
        ]
    errors: list[str] = []
    fee = pricing_model.get(PAYMENT_FEE_KEY)
    if fee is not None:
        errors.extend(_fee_object_errors(fee, f"pricing_model.{PAYMENT_FEE_KEY}", currency))
    for index, row in enumerate(_catalogue_rows(pricing_model)):
        if UNIT_COST_KEY not in row:
            continue
        cost = row.get(UNIT_COST_KEY)
        if cost is None:
            continue  # an explicit null clears the recorded cost (unmeasured, never 0)
        errors.extend(
            _cost_object_errors(cost, f"pricing_model.row[{index}].{UNIT_COST_KEY}", currency)
        )
    return errors


# ---------------------------------------------------------------------------------------------
# the read edge: tolerant, labelled, never raising
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedCost:
    """One catalogue row's recorded unit cost, with its own provenance (see the module docstring)."""

    amount_cents: int
    currency: str
    basis: str
    kind: str
    source: str
    observed_at: date | None
    note: str
    #: how the value was recorded — `object` | `legacy_dollars` (a stored bare number).
    recorded_as: str

    def label(self) -> str:
        """The provenance phrase a prompt line names, e.g. `SOURCED — source ...`."""
        if self.kind == COST_SOURCED and self.source and self.observed_at is not None:
            return f"SOURCED — recorded source {self.source}, observed {self.observed_at.isoformat()}"
        if self.kind == COST_ESTIMATE:
            return "ESTIMATE — a stated assumption, not an observation"
        return (
            "UNSOURCED — recorded with no source"
            + (" (legacy bare number, read as dollars)" if self.recorded_as == "legacy_dollars" else "")
        )


@dataclass(frozen=True)
class RecordedFee:
    """The catalogue-level payment fee, with the same provenance vocabulary as a cost."""

    percent: Decimal | None
    fixed_cents: int | None
    currency: str
    kind: str
    source: str
    observed_at: date | None
    note: str

    def describe(self) -> str:
        parts = []
        if self.percent is not None:
            parts.append(f"{self.percent.normalize():f}%")
        if self.fixed_cents is not None:
            parts.append(f"{self.fixed_cents} cents")
        return " + ".join(parts) if parts else "nothing recorded"

    def label(self) -> str:
        if self.kind == COST_SOURCED and self.source and self.observed_at is not None:
            return f"SOURCED — recorded source {self.source}, observed {self.observed_at.isoformat()}"
        if self.kind == COST_ESTIMATE:
            return "ESTIMATE — a stated assumption, not an observation"
        return "UNSOURCED — recorded with no source"


@dataclass(frozen=True)
class SkuRow:
    """One catalogue row as the money agent needs it: what it sells for, what it costs."""

    sku: str
    name: str
    retail_cents: int | None
    cost: RecordedCost | None
    #: why `cost` is None / why `retail_cents` is None — named, never silent.
    cost_reason: str
    retail_reason: str


def _read_cost(value: Any, currency: str) -> tuple[RecordedCost | None, str]:
    """Read one stored `unit_cost` into a labelled `RecordedCost`, or a named reason why not."""
    if value is None:
        return None, "no unit_cost is recorded for this row"
    if _is_number(value):
        # LEGACY SHAPE (wave-2 trial: `unit_cost: 9.4` beside `retail: 28` — DOLLARS, not cents).
        amount = _dollars_to_cents(value)
        if amount is None or amount <= 0:
            return None, f"stored unit_cost {value!r} is not a usable positive amount"
        return (
            RecordedCost(
                amount_cents=amount,
                currency=currency,
                basis=COST_BASE_PER_UNIT,
                kind=COST_UNSOURCED,
                source="",
                observed_at=None,
                note="",
                recorded_as="legacy_dollars",
            ),
            "",
        )
    if not isinstance(value, dict):
        return None, f"stored unit_cost is a {type(value).__name__}, not an amount or object"
    amount = _cents(value.get("amount_cents"))
    if amount is None:
        return None, (
            f"stored unit_cost has no usable amount_cents ({value.get('amount_cents')!r}); a cost "
            "must be a positive integer count of cents"
        )
    recorded_currency = _currency_code(value.get("currency"), currency) or currency
    if recorded_currency != currency:
        return None, (
            f"stored unit_cost currency {recorded_currency} differs from the catalogue currency "
            f"{currency}; no conversion is performed"
        )
    basis = value.get("basis", COST_BASE_PER_UNIT)
    if basis != COST_BASE_PER_UNIT:
        return None, f"stored unit_cost basis {basis!r} is not {COST_BASE_PER_UNIT}"
    source = _clean(value.get("source"))
    observed = _iso_date(value.get("observed_at"))
    kind = _resolve_kind(
        value.get("kind"), has_source=bool(source), has_date=observed is not None
    )
    if kind is None:
        return None, f"stored unit_cost kind {value.get('kind')!r} is not one of {list(COST_KINDS)}"
    if kind == COST_SOURCED and (not source or observed is None):
        return None, (
            'stored unit_cost claims kind="sourced" but carries no source and/or no usable '
            "observed_at date — an undated or unattributed price is not a sourced one"
        )
    return (
        RecordedCost(
            amount_cents=amount,
            currency=recorded_currency,
            basis=COST_BASE_PER_UNIT,
            kind=kind,
            source=_bounded(source, MAX_SOURCE_CHARS),
            observed_at=observed,
            note=_bounded(value.get("note"), MAX_NOTE_CHARS),
            recorded_as="object",
        ),
        "",
    )


def _read_fee(pricing_model: Any, currency: str) -> tuple[RecordedFee | None, str]:
    """Read the catalogue-level `payment_fee`, or a named reason why it is unmeasured."""
    if not isinstance(pricing_model, dict):
        return None, "no pricing model is recorded"
    value = pricing_model.get(PAYMENT_FEE_KEY)
    if value is None:
        return None, "no payment_fee is recorded (no fee schedule in the company profile)"
    if not isinstance(value, dict):
        return None, f"stored payment_fee is a {type(value).__name__}, not an object"
    percent: Decimal | None = None
    raw_percent = value.get("percent")
    percent_number = float(raw_percent) if _is_number(raw_percent) else None
    if percent_number is not None and 0 <= percent_number <= 100:
        percent = Decimal(str(raw_percent))
    fixed: int | None = None
    if (
        isinstance(value.get("fixed_cents"), int)
        and not isinstance(value.get("fixed_cents"), bool)
        and value["fixed_cents"] >= 0
    ):
        fixed = int(value["fixed_cents"])
    if percent is None and fixed is None:
        return None, "stored payment_fee carries neither a usable percent nor a fixed_cents"
    recorded_currency = _currency_code(value.get("currency"), currency) or currency
    if recorded_currency != currency:
        return None, (
            f"stored payment_fee currency {recorded_currency} differs from the catalogue currency "
            f"{currency}; no conversion is performed"
        )
    source = _clean(value.get("source"))
    observed = _iso_date(value.get("observed_at"))
    kind = _resolve_kind(
        value.get("kind"), has_source=bool(source), has_date=observed is not None
    ) or COST_UNSOURCED
    return (
        RecordedFee(
            percent=percent,
            fixed_cents=fixed,
            currency=recorded_currency,
            kind=kind,
            source=_bounded(source, MAX_SOURCE_CHARS),
            observed_at=observed,
            note=_bounded(value.get("note"), MAX_NOTE_CHARS),
        ),
        "",
    )


def recorded_skus(pricing_model: Any) -> list[SkuRow]:
    """Every catalogue row with its recorded cost (or the named reason it has none)."""
    currency = "USD"
    if isinstance(pricing_model, dict):
        currency = _currency_code(pricing_model.get("currency"), "USD") or "USD"
    rows: list[SkuRow] = []
    for row in _catalogue_rows(pricing_model):
        lowered = {str(key).lower(): value for key, value in row.items()}
        name = ""
        for key in _NAME_KEYS:
            name = _bounded(lowered.get(key), MAX_NAME_CHARS)
            if name:
                break
        sku = ""
        for key in _SKU_KEYS:
            sku = _bounded(lowered.get(key), MAX_NAME_CHARS)
            if sku:
                break
        if not name and not sku:
            continue  # no name and no identifier: not a catalogue row (the storefront agrees)
        retail_cents: int | None = None
        for key in _PRICE_KEYS:
            if key in lowered:
                retail_cents = _dollars_to_cents(lowered.get(key))
                break
        cost, cost_reason = _read_cost(lowered.get(UNIT_COST_KEY), currency)
        rows.append(
            SkuRow(
                sku=sku,
                name=name,
                retail_cents=retail_cents,
                cost=cost,
                cost_reason=cost_reason,
                retail_reason=(
                    "" if retail_cents is not None else "no usable selling price is recorded"
                ),
            )
        )
    return rows


def _fee_cents(fee: RecordedFee, retail_cents: int) -> int:
    """The fee on one sale: `round(percent% x retail) + fixed`, exact at the cent (half-up)."""
    percent_part = 0
    if fee.percent is not None:
        percent_part = int(
            (fee.percent / Decimal(100) * Decimal(retail_cents)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    return percent_part + int(fee.fixed_cents or 0)


def derive_unit_economics(
    pricing_model: Any,
    *,
    budget_cents: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """The derived per-SKU arithmetic — `None` wherever an input is unmeasured.

    The formula is printed in the prompt block and re-stated here so the numbers are checkable:

        payment_fee  = round(percent% x retail) + fixed_cents        (cents, half-up)
        contribution = retail - unit_cost - payment_fee             (cents)
        break-even   = ceil(working budget / contribution)          (units, only if > 0)

    `today` defaults to the company's "today" (`today_in()`); it is a parameter so staleness and
    future-dated observations are testable without a clock.
    """
    day = today if today is not None else today_in()
    currency = "USD"
    if isinstance(pricing_model, dict):
        currency = _currency_code(pricing_model.get("currency"), "USD") or "USD"
    fee, fee_reason = _read_fee(pricing_model, currency)
    rows = recorded_skus(pricing_model)
    derived: list[dict[str, Any]] = []
    for row in rows:
        line: dict[str, Any] = {
            "sku": row.sku,
            "name": row.name,
            "currency": currency,
            "retail_cents": row.retail_cents,
            "unit_cost_cents": None,
            "cost_kind": None,
            "cost_source": None,
            "cost_observed_at": None,
            "cost_age_days": None,
            "cost_stale": None,
            "cost_note": "",
            "cost_label": "",
            "fee_cents": None,
            "contribution_cents": None,
            "contribution_pct": None,
            "break_even_units": None,
            "computable": False,
            "reason": "",
        }
        reasons: list[str] = []
        if row.retail_cents is None:
            reasons.append(row.retail_reason)
        if row.cost is None:
            reasons.append(row.cost_reason)
        else:
            line["unit_cost_cents"] = row.cost.amount_cents
            line["cost_kind"] = row.cost.kind
            line["cost_source"] = row.cost.source or None
            line["cost_observed_at"] = (
                row.cost.observed_at.isoformat() if row.cost.observed_at else None
            )
            line["cost_note"] = row.cost.note
            line["cost_label"] = row.cost.label()
            if row.cost.observed_at is not None:
                age = (day - row.cost.observed_at).days
                line["cost_age_days"] = age
                line["cost_stale"] = age >= COST_STALENESS_DAYS
                if age < 0:
                    reasons.append(
                        f"unit_cost observed_at {row.cost.observed_at.isoformat()} is in the "
                        "future — not a completed observation"
                    )
        if not reasons and fee is None:
            reasons.append(f"payment fee unmeasured: {fee_reason}")
        if reasons:
            line["reason"] = "; ".join(reasons)
            derived.append(line)
            continue
        assert row.retail_cents is not None and row.cost is not None and fee is not None
        fee_cents = _fee_cents(fee, row.retail_cents)
        contribution = row.retail_cents - row.cost.amount_cents - fee_cents
        line["fee_cents"] = fee_cents
        line["contribution_cents"] = contribution
        if row.retail_cents > 0:
            line["contribution_pct"] = float(
                (Decimal(contribution) / Decimal(row.retail_cents) * 100).quantize(
                    Decimal("0.1"), rounding=ROUND_HALF_UP
                )
            )
        if contribution <= 0:
            # A non-positive contribution never recovers a budget: there is no sale count to
            # report, and inventing one would be the exact fabrication this module exists to stop.
            line["reason"] = (
                "contribution is not positive at the recorded inputs — no sale count can recover "
                "the budget"
            )
            derived.append(line)
            continue
        line["computable"] = True
        if budget_cents is None or budget_cents <= 0:
            line["reason"] = (
                "no working budget is recorded — break-even units cannot be derived"
            )
        else:
            line["break_even_units"] = -(-budget_cents // contribution)
        derived.append(line)
    return {
        "currency": currency,
        "rows": derived,
        "payment_fee": None if fee is None else {
            "percent": None if fee.percent is None else float(fee.percent),
            "fixed_cents": fee.fixed_cents,
            "currency": fee.currency,
            "kind": fee.kind,
            "source": fee.source or None,
            "observed_at": fee.observed_at.isoformat() if fee.observed_at else None,
        },
        "payment_fee_reason": fee_reason or None,
        "budget_cents": budget_cents,
        "computed_rows": sum(1 for row in derived if row["computable"]),
        "unmeasured_rows": sum(1 for row in derived if not row["computable"]),
    }


def _usd(cents: int | None) -> str:
    """`$15.46` from cents; `—` for an unmeasured amount (never `$0.00` for "unknown")."""
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(cents), 100)
    return f"{sign}${whole:,}.{part:02d}"


def render_cost_basis_block(
    pricing_model: Any,
    *,
    budget_cents: int | None = None,
    today: date | None = None,
) -> str | None:
    """The finance agent's `extra_context` block — or ``None`` when no pricing model is recorded.

    A missing pricing model says nothing (the run degrades to its previous behaviour); a pricing
    model WITH rows and no costs renders the UNMEASURED lines, because that is the actionable state
    (it tells the model exactly what is missing and what to record).  Never a 0, never a guess.
    """
    if not isinstance(pricing_model, dict) or not pricing_model:
        return None
    view = derive_unit_economics(pricing_model, budget_cents=budget_cents, today=today)
    fee = view["payment_fee"]
    lines: list[str] = [
        "RECORDED UNIT ECONOMICS (cost inputs recorded on the company profile; each line carries "
        "its own provenance)",
        "DERIVATION: payment_fee = round(percent x retail) + fixed_cents; contribution = retail - "
        "unit_cost - payment_fee; break-even units = ceil(working budget / contribution).",
    ]
    if view["budget_cents"] is None:
        lines.append(
            "WORKING BUDGET: UNMEASURED — no owner_budget_usd is recorded, so break-even units "
            "cannot be derived."
        )
    else:
        lines.append(
            f"WORKING BUDGET: {view['budget_cents']} cents ({_usd(view['budget_cents'])}) — the "
            "owner-recorded launch budget (company_config.owner_budget_usd), not a measured spend."
        )
    if fee is None:
        lines.append(f"PAYMENT FEE: UNMEASURED — {view['payment_fee_reason']}.")
    else:
        fee_text = []
        if fee["percent"] is not None:
            fee_text.append(f"{fee['percent']}%")
        if fee["fixed_cents"] is not None:
            fee_text.append(f"{fee['fixed_cents']} cents")
        lines.append(f"PAYMENT FEE: {' + '.join(fee_text)} [{_fee_label(fee)}]")
    rows = view["rows"]
    if not rows:
        lines.append(
            "CATALOGUE: the pricing model carries no priced product rows, so no per-SKU cost can "
            "be matched to a sale."
        )
    for index, row in enumerate(rows, start=1):
        identity = " / ".join(part for part in (row["sku"], f"\"{row['name']}\"") if part)
        lines.append(f"SKU {index} — {identity}:")
        lines.append(
            f"  retail: {row['retail_cents']} cents ({_usd(row['retail_cents'])})"
            if row["retail_cents"] is not None
            else "  retail: UNMEASURED — no usable selling price is recorded"
        )
        if row["unit_cost_cents"] is None:
            lines.append(
                "  unit_cost: UNMEASURED — contribution, margin and break-even are NOT COMPUTABLE "
                "for this SKU (the reason is named below; do not substitute 0 or an estimate)."
            )
        else:
            age = row["cost_age_days"]
            stale = " STALE — older than the 180-day re-verify threshold" if row["cost_stale"] else ""
            age_text = "" if age is None else f", {age} day(s) old{stale}"
            note = f" (note: {row['cost_note']})" if row["cost_note"] else ""
            lines.append(
                f"  unit_cost: {row['unit_cost_cents']} cents ({_usd(row['unit_cost_cents'])}) "
                f"[{row['cost_label']}{age_text}]{note}"
            )
        if row["computable"]:
            lines.append(
                f"  payment fee on one sale: {row['fee_cents']} cents — DERIVED"
            )
            pct = "" if row["contribution_pct"] is None else f" ({row['contribution_pct']}% of retail)"
            lines.append(
                f"  contribution: {row['contribution_cents']} cents "
                f"({_usd(row['contribution_cents'])}){pct} — DERIVED from the inputs above"
            )
            if row["break_even_units"] is not None:
                lines.append(
                    f"  break-even: {row['break_even_units']} unit(s) — DERIVED "
                    f"({view['budget_cents']} / {row['contribution_cents']}, rounded up)"
                )
            if row["reason"]:
                lines.append(f"  note: {row['reason']}")
        else:
            lines.append(f"  NOT COMPUTABLE — {row['reason']}")
    lines.extend(
        [
            "PROVENANCE VOCABULARY: SOURCED = a cost observed at the recorded source on "
            "observed_at (a dated claim, not a live check); ESTIMATE = a stated assumption, not an "
            "observation; UNSOURCED = recorded with no source; UNMEASURED = absent and NOT zero.",
            "NOT IN THIS BLOCK (unmeasured — never estimate them): supplier shipping per order "
            "beyond what unit_cost includes, packaging/labelling not in unit_cost, sales tax, "
            "labour, and whether any unit has actually sold.",
            "HOW TO USE: quote the DERIVED lines as derived arithmetic from the recorded inputs "
            "(never as measurements); never use an UNMEASURED line as 0; name every unmeasured "
            "input you needed in `alerts` so someone records it; if this block is missing from a "
            "run, say COGS is unmeasured rather than inferring a cost.",
        ]
    )
    return "\n".join(lines)


def _fee_label(fee: dict[str, Any]) -> str:
    """The provenance phrase for the fee line (same vocabulary as a cost)."""
    if fee["kind"] == COST_SOURCED and fee["source"] and fee["observed_at"]:
        return f"SOURCED — recorded source {fee['source']}, observed {fee['observed_at']}"
    if fee["kind"] == COST_ESTIMATE:
        return "ESTIMATE — a stated assumption, not an observation"
    return "UNSOURCED — recorded with no source"


def budget_cents_from_context(context: dict[str, Any] | None) -> int | None:
    """The owner's recorded launch budget as exact cents — `None` when it was never set.

    Read from the company context (`owner_budget_usd`, a NUMERIC(10,2) at rest, so it may arrive
    as `Decimal`, `float` or `str`).  It is CONFIGURED data: the profile's launch budget, not a
    measured spend, and the prompt block says so beside the break-even numbers.
    """
    company = (context or {}).get("company")
    if not isinstance(company, dict):
        return None
    value = company.get("owner_budget_usd")
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cost_basis_from_context(context: dict[str, Any] | None) -> str | None:
    """The finance agent's cost block from the run context — ``None`` when none is recorded."""
    company = (context or {}).get("company")
    if not isinstance(company, dict):
        return None
    return render_cost_basis_block(
        company.get("pricing_model"),
        budget_cents=budget_cents_from_context(context),
    )


__all__ = [
    "CATALOGUE_LIST_KEYS",
    "COST_BASES",
    "COST_ESTIMATE",
    "COST_KINDS",
    "COST_SOURCED",
    "COST_STALENESS_DAYS",
    "COST_UNSOURCED",
    "FEE_OBJECT_KEYS",
    "COST_OBJECT_KEYS",
    "PAYMENT_FEE_KEY",
    "RecordedCost",
    "RecordedFee",
    "SkuRow",
    "UNIT_COST_KEY",
    "budget_cents_from_context",
    "cost_basis_from_context",
    "cost_input_errors",
    "derive_unit_economics",
    "recorded_skus",
    "render_cost_basis_block",
]
