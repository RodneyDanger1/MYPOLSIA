"use client";
import { useEffect, useRef, useState } from "react";
import { api, type AgentStatus } from "@/lib/api";
import { readErrorText } from "@/components/dashboard/states";

/** Why a read was started. Only `tick` is subject to the hidden-tab guard (see the effect). */
type ReadReason = "mount" | "tick" | "visible";

/**
 * Polls `/agents/status`.
 *
 * Deliberate decisions, recorded because they are visible in the behaviour:
 *  - Requests are abortable and non-overlapping: if a poll is still in flight when the interval
 *    fires, that tick is skipped rather than queued, so a slow backend cannot stack requests.
 *  - A response is dropped when the component unmounted or when a newer request already landed, so
 *    a slow reply can never overwrite fresher data.
 *  - The interval stays per-hook, not a shared global poller: the unit suite pins the exact call
 *    count per tick, and each subscriber polls through its own hook instance — live consumers are
 *    `AgentsRoster` (Agents page) and `PolsiaStatusCard` (dashboard); `AgentStatusGrid` is the
 *    dead, labelled U1 leftover (see its why-comment). A shared poller would add coordination
 *    risk for no measured gain at this subscriber count — revisit only if that changes.
 *  - V26/F1 — visibility gates the INTERVAL, never the mount. A tab opened in the background used
 *    to skip its mount read entirely (the guard sat before the request) and `setLoading(false)`
 *    lived only in the skipped `finally`, so `/agents` could show nine skeletons forever. Now the
 *    mount read runs regardless of visibility, hidden suppresses only the tick nobody can see,
 *    and a `visibilitychange` back to visible is ONE immediate read (deduped by `inFlight`).
 *  - V26/F1 — `loading` is terminal on every outcome: success, error and an empty answer all
 *    reach `setLoading(false)` in the same `finally`. There is no path that leaves `loading` true
 *    once the last request has settled.
 */
export function useAgentStatus(pollIntervalMs = 30000) {
  const [statuses, setStatuses] = useState<AgentStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const inFlight = useRef(false);
  const requestSeq = useRef(0);
  /** False only after unmount. React 18 ignores a post-unmount setState, but the stale-drop and
   *  terminal-loading decisions below should be explicit rather than incidental — and an effect
   *  re-run (StrictMode, or a changed interval) must not drop the old request's result while
   *  `loading` stays true. */
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    const isHidden = () =>
      typeof document !== "undefined" && document.visibilityState === "hidden";

    const fetchStatuses = async (reason: ReadReason) => {
      if (inFlight.current || !mounted.current) return;
      // V24/L5, narrowed by V26/F1: a hidden tab does not need a fresh POLL — browsers throttle
      // its timers anyway, and the tick that DOES fire is a real request nobody could see.
      // Skipping the tick leaves the counters untouched; the next visible tick (or the
      // `visibilitychange` read below) updates them. The MOUNT read is exempt on purpose: the
      // page must render the same roster whether it was opened in front or in the background.
      if (reason === "tick" && isHidden()) return;
      inFlight.current = true;
      const seq = ++requestSeq.current;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), Math.max(5000, pollIntervalMs));

      try {
        const data = await api.get<AgentStatus[]>("/agents/status", { signal: controller.signal });
        if (seq === requestSeq.current && mounted.current) {
          setStatuses(Array.isArray(data) ? data : []);
          setError(null);
        }
      } catch (e) {
        if (seq === requestSeq.current && mounted.current) {
          setError(readErrorText(e));
        }
      } finally {
        clearTimeout(timeout);
        inFlight.current = false;
        // V26/F1: success, error and an empty answer all land here, so `loading` is ALWAYS
        // terminal once the last request settles. Never leave it true without a request in
        // flight — that is exactly the hidden-mount skeleton bug this wave fixes.
        if (mounted.current) setLoading(false);
      }
    };

    void fetchStatuses("mount");
    const interval = setInterval(() => {
      void fetchStatuses("tick");
    }, pollIntervalMs);

    // V26/F1: coming back to the tab is one immediate read instead of waiting up to a full
    // interval for the next tick. A read already on the wire wins (the `inFlight` gate), so a
    // quick hide/show cannot stack two requests; a transition TO hidden is not a reason to read.
    const onVisibilityChange = () => {
      if (isHidden()) return;
      void fetchStatuses("visible");
    };
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibilityChange);
    }

    return () => {
      clearInterval(interval);
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibilityChange);
      }
    };
  }, [pollIntervalMs]);

  return { statuses, loading, error };
}
