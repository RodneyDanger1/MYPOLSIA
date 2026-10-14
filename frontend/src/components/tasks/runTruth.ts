/**
 * The measured run values of ONE task, as display strings — the single place the wave-U2 truth
 * fields (`cost_usd` / `tokens_in` / `tokens_out`, `backend/app/schemas/task.py:62-65`) become
 * text on the tasks surfaces.
 *
 * The contract these helpers enforce (B4, wave U3):
 *   * a RECORDED value renders exactly as the server measured it — a measured `0` prints as
 *     `$0.00` / `0`, never as the "not measured" dash; a real sub-cent cost prints to all four of
 *     its stored decimals (the sums come off NUMERIC(14,4)) and never rounds down to `$0.00`;
 *   * an UNMEASURED value (null, absent, or anything that is not a finite number) answers `null`
 *     so the caller can state the reason — these helpers never fabricate `$0.00` or `0`.
 *
 * WHY LOCAL (and not `format.ts`'s `moneyText`, which is another lane's file and stays untouched):
 * `moneyText` renders `"$0"` for a zero and `"$2.5"` for two-and-a-half (no fixed precision) and
 * folds null into a bare em dash — B4 needs the exact-precision form AND null kept
 * distinguishable from a measured zero so the sheet can attach the stated reason. Recorded in
 * the wave receipt per the lane rules.
 */

/**
 * `$`-prefixed cost for a finite wire value; `null` when the wire carries no measured number.
 * Precision: the exact value to at most 4 decimals (the stored precision) and never fewer than
 * two (`0 -> "$0.00"`, `0.0042 -> "$0.0042"`, `1.5 -> "$1.50"`). The number is printed as the
 * server summed it — no estimate, no pricing table, no rounding into a "nicer" figure.
 */
export function costUsdText(cost: number | null | undefined): string | null {
  if (typeof cost !== "number" || !Number.isFinite(cost)) return null;
  // `toFixed(4)` is lossless for the ≤4-decimal stored sums; the split below is written
  // defensively so even a hostile payload (e.g. >= 1e21, where toFixed goes exponential) can
  // never throw here.
  const fixed = cost.toFixed(4);
  const dot = fixed.indexOf(".");
  const whole = dot === -1 ? fixed : fixed.slice(0, dot);
  const fraction = dot === -1 ? "00" : fixed.slice(dot + 1);
  const trimmed = fraction.replace(/0+$/, "");
  const kept = trimmed.length < 2 ? fraction.slice(0, 2) : trimmed;
  return `$${whole}.${kept}`;
}

/**
 * Grouped token count (`1234 -> "1,234"`) for a finite wire value; `null` when unmeasured.
 * Grouping is pinned to `en-US` deliberately (Q-10's lesson: a locale-dependent rendering is an
 * unpinnable display).
 */
export function tokenText(count: number | null | undefined): string | null {
  if (typeof count !== "number" || !Number.isFinite(count)) return null;
  return count.toLocaleString("en-US");
}
