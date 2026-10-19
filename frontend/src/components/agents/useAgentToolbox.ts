"use client";

import { useCallback, useEffect, useState } from "react";

import { capabilitiesFrom, type CapabilitiesRead } from "@/components/capabilities/types";
import { readErrorText } from "@/components/dashboard/states";
import { api } from "@/lib/api";

/**
 * One read of `GET /api/v1/ops/capabilities` for the WHOLE agents roster — never one request per
 * row. It reuses the capabilities page's own `capabilitiesFrom` parser (same accepted envelopes,
 * same malformed-row discipline, same `unreadable` count) and the dashboard's shared
 * `readErrorText` (so a refusal stays the server's `code: message — how_to_fix`).
 *
 * States: loading (not read yet), error (named, retryable), read (a genuine answer — possibly an
 * empty map). A 2xx body that is not the map lands in `error` via the parser's named throw; it can
 * never masquerade as "this agent has no tools".
 */
export type AgentToolboxRead = {
  read: CapabilitiesRead | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
};

export function useAgentToolbox(): AgentToolboxRead {
  const [read, setRead] = useState<CapabilitiesRead | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloadCount, setReloadCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    // A retry goes back through "not read yet", so a stale map can never sit under a new read.
    setRead(null);
    setError(null);
    setLoading(true);
    // `Promise.resolve` keeps a partial/mocked client (or a future sync return) from crashing the
    // roster: a non-payload answer lands in the parser's named error state, never in a dead tree.
    Promise.resolve(api.get<unknown>("/ops/capabilities"))
      .then((body) => {
        if (cancelled) return;
        try {
          setRead(capabilitiesFrom(body));
        } catch (e: unknown) {
          setError(readErrorText(e));
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(readErrorText(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadCount]);

  const reload = useCallback(() => setReloadCount((n) => n + 1), []);

  return { read, loading, error, reload };
}
