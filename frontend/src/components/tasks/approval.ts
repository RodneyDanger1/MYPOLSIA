import type { TaskRow } from "./types";

/**
 * The parked-task fingerprint reader.
 *
 * D1's wire (`backend/app/schemas/task.py:152-182`) splits the old field in two — `approval_required`
 * is now a BOOLEAN derived from `approval_fingerprints: list[str]` — so the sources are, in order:
 *
 *   1. `TaskResponse.approval_fingerprints` — the fingerprints themselves. This is the source of
 *      truth: it is data, not a sentence, so a copy edit cannot make the Approve control disappear
 *      (A5-02). An EMPTY list is an ANSWER ("nothing is parked"), never "unknown".
 *   2. `approval_required` as a LIST — the pre-D1 spelling, still served by an older backend and
 *      still what the A3 pins feed. The BOOLEAN spelling of the same field is a flag, never a
 *      list (`Array.isArray(true)` is false, which is exactly how the Approve control went
 *      missing on a parked row).
 *   3. `result_summary` prose — the fallback for a server that serves NEITHER structured field.
 *      The producer writes, when a run parks a task `awaiting_approval`:
 *
 *        "awaiting approval: N gated action(s) need a token; request one for fingerprint(s) <fp>[, <fp>…]"
 *
 * Every path passes the SAME strict token contract (`action_fingerprint`, 8-128 chars —
 * `app/schemas/approval.py`) and returns [] rather than a guess. A present-but-unreadable
 * structured answer is deliberately NOT backfilled from prose: the structured answer is the
 * authority, and a control built from a stale sentence could post a token the server rejects.
 * The sheet then states that the fingerprint is missing instead of rendering a control that
 * would post garbage.
 */
const FINGERPRINT_LIST_RE = /fingerprint\(s\)\s+([^\n]+)/i;
const FINGERPRINT_RE = /^[A-Za-z0-9_-]{8,128}$/;

/** The fields this reader consults — satisfied by `TaskRow` and by the lighter row a surface keeps
 *  for its own list (Ops' pending rows), so both use ONE reader instead of re-deriving the rule. */
export type ApprovalCarrier = Pick<
  TaskRow,
  "approval_required" | "approval_fingerprints" | "result_summary"
>;

export function fingerprintsFrom(resultSummary: string | null | undefined): string[] {
  if (!resultSummary) return [];
  const match = FINGERPRINT_LIST_RE.exec(resultSummary);
  if (!match) return [];
  return match[1]
    .split(",")
    .map((token) => token.trim())
    .filter((token) => FINGERPRINT_RE.test(token));
}

/** One list of raw values -> the tokens the mint endpoint accepts, in the server's order and
 *  deduplicated (the same collapse D1 applies). Non-strings and out-of-contract tokens are
 *  dropped, never coerced into a value the server would reject. */
function strictTokens(values: readonly unknown[]): string[] {
  const found = values
    .map((value) => (typeof value === "string" ? value.trim() : ""))
    .filter((token) => FINGERPRINT_RE.test(token));
  return Array.from(new Set(found));
}

/**
 * The fingerprints the SERVER served, or `null` when it served no structured field at all
 * (`null` = "this server predates D1; the parked sentence is the only source it has").
 *
 *   200 + `approval_fingerprints: []`      -> []      (nothing is parked — a measured answer)
 *   200 + `approval_required: true` only   -> []      (parked, but this response serves no names)
 *   200 + `approval_required: [{...}]`     -> tokens  (the pre-D1 list spelling)
 *   neither key                            -> null    (fall back to the parked sentence)
 */
export function structuredFingerprints(task: ApprovalCarrier): string[] | null {
  const served = task.approval_fingerprints;
  if (Array.isArray(served)) return strictTokens(served);

  const legacy = task.approval_required;
  if (Array.isArray(legacy)) {
    return strictTokens(
      legacy.map((item) => (typeof item?.fingerprint === "string" ? item.fingerprint : null)),
    );
  }
  // D1's boolean with no list beside it. `false` is "nothing is parked"; `true` is "parked, but
  // this response does not name the fingerprints". Either way the server ANSWERED, so the prose —
  // older than that answer, and possibly describing a park already cleared — is not consulted.
  if (typeof legacy === "boolean") return [];
  return null;
}

/** The parked task's fingerprints: the server's structured answer first, the prose parser only as
 *  a fallback for a server that serves neither structured field. */
export function fingerprintsForTask(task: ApprovalCarrier): string[] {
  return structuredFingerprints(task) ?? fingerprintsFrom(task.result_summary);
}
