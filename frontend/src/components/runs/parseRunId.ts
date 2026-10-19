/**
 * `/runs/[id]` route grammar: one POSITIVE integer, nothing else.
 *
 * A path segment that does not name a run is NEVER requested — a request whose 404 would blame
 * the server for the caller's typo is exactly the misstatement this guard removes. `005` is the
 * same id as `5` (Number semantics); `5.0`, `5abc`, `0` and negatives are not ids.
 */
export function parseRunId(raw: string | undefined | null): number | null {
  if (typeof raw !== "string") return null;
  if (!/^[0-9]+$/.test(raw)) return null;
  const id = Number(raw);
  if (!Number.isSafeInteger(id) || id <= 0) return null;
  return id;
}
