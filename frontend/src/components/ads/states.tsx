/**
 * The ads page's state vocabulary, in one place — the same shape and the same words as the finance
 * lane's `components/finance/states.tsx` so the two surfaces cannot drift into different
 * loading/error dialects.
 */

/**
 * "Not read yet" is neither "empty" nor "no values" — it is a skeleton. Three card outlines (the
 * row species this page renders) inside one `aria-busy` region, so a screen reader is told the
 * surface is busy and a sighted reader sees the live mono-caps `LOADING…` line instead of an empty
 * page that would read as "there is nothing here".
 */
export function AdsSkeleton() {
  return (
    <div className="space-y-3" data-testid="ads-loading" aria-busy="true">
      <p className="ui-label text-ink-muted">Loading…</p>
      {Array.from({ length: 3 }).map((_, index) => (
        <div key={index} data-testid="ads-skeleton-row" className="card-surface px-3 py-3">
          <div className="h-4 w-1/3 animate-pulse bg-surface-strong" />
          <div className="mt-3 h-3 w-2/3 animate-pulse bg-surface-strong" />
        </div>
      ))}
    </div>
  );
}

/** One failure sentence, built from the real error. The failure is named — never an empty list,
 *  never a dash row standing in for it (the audit's Q-09 class). */
export function ReadError({ label, message }: { label: string; message: string }) {
  return (
    <p role="alert" className="text-sm text-alert">
      Could not load {label}: {message}
    </p>
  );
}
