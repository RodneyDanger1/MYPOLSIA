"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { API_KEY, api, type ActivityEvent } from "@/lib/api";

const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost";

// Reconnect policy: exponential backoff with full jitter, capped.  The uploaded hook retried a
// flat 3s later, so a backend restart produced a synchronised reconnect stampede from every open
// dashboard.  Jitter spreads the retries; the cap keeps recovery bounded.
const BASE_RETRY_MS = 1_000;
const MAX_RETRY_MS = 30_000;

function backoffDelay(attempt: number): number {
  const ceiling = Math.min(MAX_RETRY_MS, BASE_RETRY_MS * 2 ** Math.min(attempt, 6));
  return Math.floor(ceiling / 2 + Math.random() * (ceiling / 2));
}

export type UseActivityFeedOptions = {
  /** Pull recent history over REST on connect, so the feed is not empty until the next event. */
  snapshot?: boolean;
  /** How many rows that snapshot requests. */
  snapshotLimit?: number;
};

export function useActivityFeed(maxEvents = 100, options: UseActivityFeedOptions = {}) {
  const { snapshot = true, snapshotLimit = 50 } = options;
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attempts = useRef(0);
  const seen = useRef<Set<string>>(new Set());
  const closedByUs = useRef(false);
  const mounted = useRef(true);

  /** Merge incoming events: drop duplicates by id, newest first, cap at maxEvents. */
  const merge = useCallback(
    (incoming: ActivityEvent[]) => {
      if (incoming.length === 0) return;
      setEvents((prev) => {
        const fresh = incoming.filter((e) => {
          const key = String(e?.id ?? `${e?.created_at}-${e?.summary}`);
          if (seen.current.has(key)) return false;
          seen.current.add(key);
          return true;
        });
        if (fresh.length === 0) return prev;
        const next = [...fresh, ...prev];
        next.sort((a, b) => {
          const ta = Date.parse(a?.created_at ?? "") || 0;
          const tb = Date.parse(b?.created_at ?? "") || 0;
          return tb - ta || (b?.id ?? 0) - (a?.id ?? 0);
        });
        const trimmed = next.slice(0, maxEvents);
        seen.current = new Set(trimmed.map((e) => String(e?.id ?? `${e?.created_at}-${e?.summary}`)));
        return trimmed;
      });
    },
    [maxEvents],
  );

  const loadSnapshot = useCallback(async () => {
    if (!snapshot) return;
    try {
      const recent = await api.get<ActivityEvent[]>(`/dashboard/activity?limit=${snapshotLimit}`);
      if (mounted.current && Array.isArray(recent)) merge(recent);
    } catch (e) {
      // The live socket is the primary channel: a failed snapshot must not break the feed.
      if (mounted.current) setError(e instanceof Error ? e.message : String(e));
    }
  }, [merge, snapshot, snapshotLimit]);

  const connect = useCallback(() => {
    if (closedByUs.current) return;
    const current = wsRef.current;
    if (
      current &&
      (current.readyState === WebSocket.OPEN || current.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }

    // Auth parity with every REST call (audit L1-01/L3-08): browsers cannot set WebSocket
    // handshake headers, so the key rides the query string — the same NEXT_PUBLIC_API_KEY the
    // REST client already ships to the browser (no new exposure).
    const ws = new WebSocket(`${WS_URL}/ws/activity?key=${encodeURIComponent(API_KEY)}`);
    wsRef.current = ws;

    ws.onopen = () => {
      attempts.current = 0;
      if (!mounted.current) return;
      setConnected(true);
      setError(null);
      void loadSnapshot();
    };

    ws.onmessage = (e) => {
      try {
        const parsed = JSON.parse(e.data) as ActivityEvent | ActivityEvent[];
        if (mounted.current) merge(Array.isArray(parsed) ? parsed : [parsed]);
      } catch {
        // A malformed frame is ignored on purpose: one bad message must not kill the socket.
      }
    };

    ws.onclose = () => {
      if (mounted.current) setConnected(false);
      if (closedByUs.current) return;
      reconnectTimer.current = setTimeout(connect, backoffDelay(attempts.current++));
    };

    ws.onerror = () => {
      // Let onclose own the retry; closing here would double-schedule one.
      try {
        ws.close();
      } catch {
        /* already closed */
      }
    };
  }, [loadSnapshot, merge]);

  useEffect(() => {
    mounted.current = true;
    closedByUs.current = false;
    connect();
    return () => {
      mounted.current = false;
      closedByUs.current = true;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      try {
        wsRef.current?.close();
      } catch {
        /* already closed */
      }
      wsRef.current = null;
    };
  }, [connect]);

  return { events, connected, error };
}
