import { Suspense } from "react";

import { PhoneConsole } from "@/components/phone/PhoneConsole";

/**
 * `/phone` — the phone-link console (wave V16 / seat W3). A thin server wrapper on purpose: all
 * reading, ?thread= handling and the export download live in the client console, and the
 * `Suspense` boundary is what Next 14 requires above `useSearchParams` in a statically-rendered
 * route (the same shape `app/tasks/page.tsx` uses). The fallback IS the not-read-yet state.
 */
export default function PhonePage() {
  return (
    <Suspense fallback={<PhoneLoading />}>
      <PhoneConsole />
    </Suspense>
  );
}

/** The not-read-yet state — `aria-busy` skeleton rows, the repo's loading idiom. */
function PhoneLoading() {
  return (
    <div aria-busy="true" className="space-y-2">
      <p className="ui-label text-ink-muted">Loading the phone store…</p>
      {Array.from({ length: 4 }).map((_, index) => (
        <div key={index} className="h-4 animate-pulse bg-surface-strong" />
      ))}
    </div>
  );
}
