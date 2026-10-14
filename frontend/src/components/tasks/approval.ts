/**
 * The parked-task fingerprint reader.
 *
 * When a run parks a task `awaiting_approval`, the producer
 * (`backend/celery_app/tasks/agent_tasks.py`, the `parking_summary` construction) writes into
 * the task row's `result_summary`:
 *
 *   "awaiting approval: N gated action(s) need a token; request one for fingerprint(s) <fp>[, <fp>…]"
 *
 * That prose is the only per-task source for a fingerprint this wave has — the same values also
 * travel in `GET /dashboard/activity`'s `detail.fingerprints`, but that endpoint is day-windowed
 * and names tasks only by id. Parsing prose is fragile, which is why tasks.md OPEN DECISION #5
 * records the follow-up (a structured field on `TaskResponse`). Until that lands this parser is
 * deliberately strict: it extracts only tokens satisfying the mint endpoint's own contract
 * (`action_fingerprint`, 8-128 chars — `app/schemas/approval.py`) and returns [] rather than a
 * guess when the prose does not match. The sheet then says the fingerprint is missing instead of
 * rendering a control that would post garbage.
 */
const FINGERPRINT_LIST_RE = /fingerprint\(s\)\s+([^\n]+)/i;
const FINGERPRINT_RE = /^[A-Za-z0-9_-]{8,128}$/;

export function fingerprintsFrom(resultSummary: string | null | undefined): string[] {
  if (!resultSummary) return [];
  const match = FINGERPRINT_LIST_RE.exec(resultSummary);
  if (!match) return [];
  return match[1]
    .split(",")
    .map((token) => token.trim())
    .filter((token) => FINGERPRINT_RE.test(token));
}
