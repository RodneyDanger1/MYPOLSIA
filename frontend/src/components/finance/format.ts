import { NOT_MEASURED } from "@/components/dashboard/format";

/**
 * The finance page's money and count formatters — ONE family, so the Q-13 split ($1234.56 on one
 * card vs $1,234.56 on the next) cannot come back. Thousands separators and two decimals are the
 * house money shape (the live Business widget prints `$0.00`).
 *
 * The null law lives inside every formatter: `null`/`undefined` is "not measured" — the API serves
 * null, never a fabricated 0 — and renders as the one "not measured" marker this project uses
 * (`NOT_MEASURED`, imported from the shared dashboard helpers so the marker cannot fork). A value
 * that is not a finite number (NaN, an Infinity, a hostile string payload) is not a reading at
 * all: it renders the marker too, never `$NaN`. A measured 0 IS a reading and renders `$0.00`.
 */

/** A displayable reading, or null when the value is not a finite number. */
function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Dollars with thousands separators and always two decimals (`$1,234.50`, `$0.00`). */
export function usdText(value: number | null | undefined): string {
  const amount = finiteNumber(value);
  if (amount === null) return NOT_MEASURED;
  return `$${amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** The row's ISO code, normalised: surrounding space and case are irrelevant, and an absent or
 *  blank code is the server's documented default (`USD`) — never a bare number with no unit. */
export function currencyCode(currency: string | null | undefined): string {
  const raw = typeof currency === "string" ? currency.trim().toUpperCase() : "";
  return raw === "" ? "USD" : raw;
}

/** A grouped amount with NO unit — the measured half of a money figure whose currency nobody
 *  recorded. Kept separate so a caller can state the absence instead of borrowing a code. */
function amountText(value: number): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/**
 * Money in the row's OWN currency (V29/L4). USD goes through `usdText`, so every existing
 * `$1,234.50` expectation is untouched — the USD shape is exactly what it always was. Any other
 * code renders as the code plus the grouped amount (`EUR 1,234.56`), because printing `$` on a
 * EUR invoice was the bug this closes. A null/non-finite value is the house em dash for BOTH
 * branches: an amount nobody measured has no currency either.
 */
export function moneyText(
  value: number | null | undefined,
  currency: string | null | undefined,
): string {
  const amount = finiteNumber(value);
  if (amount === null) return NOT_MEASURED;
  const code = currencyCode(currency);
  if (code === "USD") return usdText(amount);
  return `${code} ${amountText(amount)}`;
}

/** Cents off the wire (`mrr_cents`) as dollars; 123455 cents is `$1,234.55`. */
export function centsText(value: number | null | undefined): string {
  const cents = finiteNumber(value);
  if (cents === null) return NOT_MEASURED;
  return usdText(cents / 100);
}

/** Cents in the row's OWN currency (V29 follow-up): USD keeps `centsText`; any other code renders
 *  `EUR 1,234.56`. Used by the finance expenses/Stripe-event tables, which carry a currency.
 *
 *  V29 follow-up A — `currency` is NULLABLE on exactly these rows (`ExpenseRecordResponse.currency`
 *  / `StripeEventResponse.currency` are `str | None`, the columns are nullable, and a blank is
 *  normalised to None on write), UNLIKE the invoice table's non-null default-USD column that
 *  `currencyCode` documents. NULL here is "the unit was not recorded": the measured amount is
 *  kept, the missing unit is STATED — it is never filled in with the invoice default, which
 *  printed `$49.99` for a row nobody gave a currency. */
export function moneyCentsText(
  value: number | null | undefined,
  currency: string | null | undefined,
): string {
  const cents = finiteNumber(value);
  if (cents === null) return NOT_MEASURED;
  const recorded = typeof currency === "string" ? currency.trim() : "";
  if (recorded === "") return `${amountText(cents / 100)} (currency not recorded)`;
  return moneyText(cents / 100, recorded);
}

/** A bare count (`active_subscribers`): grouped, no currency. */
export function countText(value: number | null | undefined): string {
  const count = finiteNumber(value);
  if (count === null) return NOT_MEASURED;
  return count.toLocaleString("en-US");
}

/** A bare string field off a list row (`customer_id`): absent — null or the empty string — is the
 *  marker, a present value is itself. */
export function textOrDash(value: string | null | undefined): string {
  return value == null || value === "" ? NOT_MEASURED : value;
}
