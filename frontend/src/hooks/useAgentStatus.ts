"use client";
import { useEffect, useRef, useState } from "react";
import { api, type AgentStatus } from "@/lib/api";
import { readErrorText } from "@/components/dashboard/states";

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
 */
export function useAgentStatus(pollIntervalMs = 30000) {
  const [statuses, setStatuses] = useState<AgentStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const inFlight = useRef(false);
  const requestSeq = useRef(0);

  useEffect(() => {
    let cancelled = false;

    const fetchStatuses = async () => {
      if (inFlight.current || cancelled) return;
      // V24/L5: a hidden tab does not need a fresh dashboard read — browsers throttle its timers
      // anyway, but the tick that DOES fire was still a real request nobody could see. Skipping it
      // leaves the counters untouched; the next visible tick (or a refocus) updates them.
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      inFlight.current = true;
      const seq = ++requestSeq.current;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), Math.max(5000, pollIntervalMs));

      try {
        const data = await api.get<AgentStatus[]>("/agents/status", { signal: controller.signal });
        if (!cancelled && seq === requestSeq.current) {
          setStatuses(Array.isArray(data) ? data : []);
          setError(null);
        }
      } catch (e) {
        if (!cancelled && seq === requestSeq.current) {
          setError(readErrorText(e));
        }
      } finally {
        clearTimeout(timeout);
        inFlight.current = false;
        if (!cancelled) setLoading(false);
      }
    };

    void fetchStatuses();
    const interval = setInterval(() => {
      void fetchStatuses();
    }, pollIntervalMs);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [pollIntervalMs]);

  return { statuses, loading, error };
}
