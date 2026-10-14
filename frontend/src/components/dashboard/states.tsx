/**
 * The dashboard's shared three-state vocabulary, in one place.
 *
 * Every region on the dashboard reads a real endpoint and can be in exactly three states — not read
 * yet, failed, or answered. These two helpers keep those first two states identical everywhere (the
 * same skeleton, the same "Could not load <what>: <real error>" sentence), so no panel can drift
 * into rendering an empty list for a failure or dashes for "not read yet".
 */

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
 *  empty list, an em-dash row, or a plausible zero. */
export function PanelError({ label, message }: { label: string; message: string }) {
  return (
    <p role="alert" className="text-sm text-alert">
      Could not load {label}: {message}
    </p>
  );
}
