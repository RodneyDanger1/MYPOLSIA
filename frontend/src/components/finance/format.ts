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

/** Cents off the wire (`mrr_cents`) as dollars; 123455 cents is `$1,234.55`. */
export function centsText(value: number | null | undefined): string {
  const cents = finiteNumber(value);
  if (cents === null) return NOT_MEASURED;
  return usdText(cents / 100);
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
