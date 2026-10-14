/**
 * The finance page's three-state vocabulary, in one place — the dashboard's own
 * `components/dashboard/states.tsx` re-created inside this lane's territory (that file is not on
 * this lane's import allowlist), with the same shape and the same words so no region can invent
 * its own loading/error dialect.
 */

/**
 * "Not read yet" is neither "empty" nor "no values" — it is a skeleton. Seven tiles (the page has
 * seven cards) inside one `aria-busy` region, so a screen reader is told the surface is busy and
 * a sighted reader sees the live mono-caps `LOADING…` line instead of a grid of "—" that would
 * read as "nothing was ever measured" (audit Q-06 — this is that finding's prescribed fix).
 */
export function SummarySkeleton() {
  return (
    <div className="space-y-2" data-testid="summary-loading" aria-busy="true">
      <p className="ui-label text-ink-muted">Loading…</p>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-4">
        {Array.from({ length: 7 }).map((_, index) => (
          <div key={index} data-testid="kpi-skeleton" className="card-surface px-3 py-3">
            <div className="h-3 w-24 animate-pulse bg-surface-strong" />
            <div className="mt-3 h-7 w-32 animate-pulse bg-surface-strong" />
          </div>
        ))}
      </div>
    </div>
  );
}

/** The same skeleton for a list panel: the mono-caps loading line over pulsing hairlines. */
export function ListSkeleton({ testId }: { testId: string }) {
  return (
    <div className="space-y-2" data-testid={testId} aria-busy="true">
      <p className="ui-label text-ink-muted">Loading…</p>
      <div className="h-3 w-3/4 animate-pulse bg-surface-strong" />
      <div className="h-3 w-2/3 animate-pulse bg-surface-strong" />
    </div>
  );
}

/** One failure sentence, from the real error. The failure is named — never an empty grid, never a
 *  dash row standing in for it (Q-09's class). */
export function ReadError({ label, message }: { label: string; message: string }) {
  return (
    <p role="alert" className="text-sm text-alert">
      Could not load {label}: {message}
    </p>
  );
}

/**
 * The idle state — the live product's own pattern for a surface that has never measured anything
 * (`● Not running yet` status line + a next-step sentence, DOSSIER §1.7 / M8). It renders INSTEAD
 * of the card grid: seven em dashes would read as "someone measured and found nothing", and a row
 * of `$0.00`s would be a fabrication — both are exactly what the null-vs-zero law forbids.
 */
export function IdleNotice() {
  return (
    <div className="space-y-1" data-testid="finance-idle">
      <p className="ui-label text-ink-muted">
        <span aria-hidden="true">● </span>Not running yet
      </p>
      <p className="text-sm text-ink-muted">
        No revenue snapshot has been recorded and no agent has run today. The finance agent records
        the first snapshot when it runs — dispatch it from the Agents page.
      </p>
    </div>
  );
}
