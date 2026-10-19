"use client";

import { useEffect } from "react";

/**
 * V25/L5 — the /ops cockpit's visibility-aware tick.
 *
 * The page polls live operator data (runs, approvals, agent status, switches) while someone is
 * watching it. A hidden tab is not watching: its interval ticks are SKIPPED (no request leaves the
 * browser for a surface nobody can see), and becoming visible again fires one immediate tick so the
 * operator never has to wait out the interval or press Refresh to see the current state.
 *
 * `onTick` must be stable (the page passes a `useState` updater wrapped in `useCallback`); this hook
 * re-subscribes only when the callback identity or cadence changes.
 */
export const OPS_POLL_MS = 10_000;

export function useVisibilityPoll(onTick: () => void, intervalMs: number = OPS_POLL_MS): void {
  useEffect(() => {
    const tickIfVisible = () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      onTick();
    };

    const interval = setInterval(tickIfVisible, intervalMs);
    document.addEventListener("visibilitychange", tickIfVisible);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", tickIfVisible);
    };
  }, [onTick, intervalMs]);
}
