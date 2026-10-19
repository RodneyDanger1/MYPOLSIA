"use client";

import { useEffect, useState } from "react";

import { useRefreshOnFocus } from "@/hooks/useRefreshOnFocus";

import { CapabilityMapView } from "@/components/capabilities/CapabilityMapView";
import { capabilitiesFrom, type CapabilitiesRead } from "@/components/capabilities/types";
import { ApiError, api } from "@/lib/api";

/**
 * `/capabilities` — the read-only capability map (frozen interface H-C1, wave V16): every agent's
 * REAL tool list + allowed action classes + channels, straight from the runtime's single sources
 * of truth (`policy_firewall.AGENT_CAPABILITIES` + the tool registry), served by
 * `GET /api/v1/ops/capabilities`. Client component on purpose (every data page here is): the API
 * base URL is known only at runtime.
 *
 * States are the house three, and the ONE forbidden stand-in is the empty state standing in for a
 * failed or unreadable read: loading (skeleton, `aria-busy`), error (the named failure under
 * `role="alert"`, with a Retry that re-reads), empty (only after a successful read that returned
 * genuinely empty arrays). Nothing is polled (C7); the retry is the explicit reload key.
 */

function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  return e instanceof Error ? e.message : String(e);
}

export default function CapabilitiesPage() {
  const [read, setRead] = useState<CapabilitiesRead | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadCount, setReloadCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    // A retry goes back through "not read yet": the skeleton is the one honest way to say
    // "the map you are looking at is being replaced" (the outreach-page rule).
    setRead(null);
    setError(null);
    api
      .get<unknown>("/ops/capabilities")
      .then((body) => {
        if (cancelled) return;
        try {
          setRead(capabilitiesFrom(body));
        } catch (e: unknown) {
          // A 2xx body that is not the map is a NAMED error, never an empty map.
          setError(errorText(e));
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [reloadCount]);

  // V24/L5: the capability map is a read-only report; coming back to the tab re-reads it after the
  // shared 30 s throttle (the registry can change between deploys) — still no polling.
  useRefreshOnFocus(() => setReloadCount((n) => n + 1));

  return (
    <div className="space-y-4">
      <div className="border-b border-line pb-1">
        <h1 className="text-3xl font-bold text-ink">Capabilities</h1>
        <p className="mt-1 text-sm text-ink-muted">
          What every agent can actually do, straight from the runtime registry — the policy
          firewall allowlists and the tool registry. Read from <code>GET /api/v1/ops/capabilities</code>;
          nothing on this page is hand-copied.
        </p>
      </div>

      {error ? (
        <div role="alert" data-testid="capabilities-error" className="border border-alert bg-surface p-3">
          <p className="ui-label text-alert">Could not load the capability map</p>
          <p className="mt-1 text-sm text-ink">{error}</p>
          <button
            type="button"
            className="link ui-label mt-2"
            onClick={() => setReloadCount((n) => n + 1)}
          >
            Retry
          </button>
        </div>
      ) : null}

      {error === null && read === null ? (
        <div className="space-y-2" data-testid="capabilities-loading" aria-busy="true">
          <p className="ui-label text-ink-muted">Loading the capability map…</p>
          {Array.from({ length: 3 }).map((_, index) => (
            <div key={index} className="card-surface px-4 py-4">
              <div className="h-3 w-1/3 animate-pulse bg-surface-strong" />
              <div className="mt-2 h-3 w-2/3 animate-pulse bg-surface-strong" />
            </div>
          ))}
        </div>
      ) : null}

      {read !== null ? <CapabilityMapView read={read} /> : null}
    </div>
  );
}
