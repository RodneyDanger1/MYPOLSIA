"use client";

import { useEffect, useRef } from "react";

/** Minimum spacing between two focus-driven re-reads. A tab that is alt-tabbed through cannot
 *  stampede the API: the first focus after the window re-reads, every one inside it is silent. */
export const REFRESH_THROTTLE_MS = 30_000;

/**
 * Re-read a page's data when the tab becomes active again.
 *
 * WHY (wave V24 / L5): every list page in this lane reads ONCE on mount and then only when its
 * explicit Refresh is pressed (`C7`: no polling). A dashboard left open in a background tab
 * therefore shows whatever it read hours ago the moment the owner returns to it. Polling is not
 * the answer (it costs requests while nobody is looking); the honest signal is the one the
 * browser already gives — the tab became visible / the window got focus.
 *
 * The contract:
 *  - `focus` (window) and `visibilitychange` → visible both count as "the owner is back";
 *  - the FIRST call is throttled against mount time, so the page's own mount read is never
 *    immediately repeated by a stray focus event (e.g. the click that opened the tab);
 *  - at most one re-read per `throttleMs` (default 30 s), even if both events fire together;
 *  - nothing fires while `document.visibilityState === "hidden"`.
 *
 * The callback is read through a ref, so a page may pass an inline arrow without re-binding the
 * listeners (and without restarting the throttle window) on every render.
 */
export function useRefreshOnFocus(
  onRefresh: () => void,
  throttleMs: number = REFRESH_THROTTLE_MS,
): void {
  const handler = useRef(onRefresh);
  const lastRefreshAt = useRef<number | null>(null);

  useEffect(() => {
    handler.current = onRefresh;
  }, [onRefresh]);

  useEffect(() => {
    // Baseline = mount time: the read the page just performed IS the first read.
    if (lastRefreshAt.current === null) lastRefreshAt.current = Date.now();

    const hidden = () =>
      typeof document !== "undefined" && document.visibilityState === "hidden";

    const maybeRefresh = () => {
      if (hidden()) return;
      const now = Date.now();
      const last = lastRefreshAt.current;
      if (last !== null && now - last < throttleMs) return;
      lastRefreshAt.current = now;
      handler.current();
    };

    const onVisibilityChange = () => {
      if (!hidden()) maybeRefresh();
    };

    window.addEventListener("focus", maybeRefresh);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("focus", maybeRefresh);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [throttleMs]);
}
