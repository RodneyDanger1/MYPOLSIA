"use client";
import { useEffect, useRef, useState } from "react";
import { api, type AgentStatus } from "@/lib/api";

/**
 * Polls `/agents/status`.
 *
 * Deliberate decisions, recorded because they are visible in the behaviour:
 *  - Requests are abortable and non-overlapping: if a poll is still in flight when the interval
 *    fires, that tick is skipped rather than queued, so a slow backend cannot stack requests.
 *  - A response is dropped when the component unmounted or when a newer request already landed, so
 *    a slow reply can never overwrite fresher data.
 *  - The interval stays per-hook on purpose: there is exactly one consumer (`AgentStatusGrid`) and
 *    the unit suite pins the exact call count per tick, so a shared global poller would add
 *    coordination risk for no measured gain while there is a single subscriber.
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
          setError(e instanceof Error ? e.message : String(e));
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
