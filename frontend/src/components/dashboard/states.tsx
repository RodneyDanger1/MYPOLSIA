/**
 * The dashboard's shared three-state vocabulary, in one place.
 *
 * Every region on the dashboard reads a real endpoint and can be in exactly three states — not read
 * yet, failed, or answered. These helpers keep those first two states identical everywhere (the
 * same skeleton, the same "Could not load <what>: <real error>" sentence), so no panel can drift
 * into rendering an empty list for a failure or dashes for "not read yet".
 */

/**
 * The failure text for a read — the refusal, BY NAME.
 *
 * `ApiError.display` is `code: message — how_to_fix`; every page-level hook already renders it
 * (useInvoices, useSchedules, useFinanceRead, useAdsRead), while the dashboard panels used to keep
 * only `e.message` — so the same 403 read as `E_SANDBOX_BLOCKED: …` on /invoices and as a code-less
 * sentence on the dashboard (V17/L4 finding L4-02). One helper, used by every panel, so the two can
 * never drift again.
 *
 * Read STRUCTURALLY, not with `instanceof ApiError`: several suites factory-mock `@/lib/api` down to
 * `{api}`, where the class is undefined and `instanceof` would throw. Anything that carries a
 * non-empty string `display` is a named refusal; everything else shows its own message.
 */
export function readErrorText(e: unknown): string {
  const display = (e as { display?: unknown } | null | undefined)?.display;
  if (typeof display === "string" && display !== "") return display;
  return e instanceof Error ? e.message : String(e);
}

/** "Not read yet" is neither "empty" nor "no data" — a skeleton, with `aria-busy` for the reader
 *  who cannot see it (the idiom the other pages already use). */
export function PanelSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true">
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="h-4 animate-pulse bg-surface-strong" />
      ))}
    </div>
  );
}

/** One failure sentence, from the real error. The failure is named instead of being rendered as an
 *  empty list, an em-dash row, or a plausible zero.
 *
 *  V24/L5: a dashboard panel that failed used to stay failed until a full page reload — the one
 *  surface with no Refresh of its own. A panel that owns an attempt counter passes `onRetry` and
 *  the failure re-issues exactly its own read. */
export function PanelError({
  label,
  message,
  onRetry,
  retryTestId,
}: {
  label: string;
  message: string;
  onRetry?: () => void;
  retryTestId?: string;
}) {
  return (
    <div className="text-sm text-alert">
      <p role="alert">
        Could not load {label}: {message}
      </p>
      {onRetry ? (
        <button
          type="button"
          className="link ui-label mt-1"
          data-testid={retryTestId}
          onClick={onRetry}
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}
