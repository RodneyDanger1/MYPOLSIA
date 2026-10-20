"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { Button, SectionHeader } from "@/components/ui";
import { ProviderCard } from "@/components/integrations/ProviderCard";
import type { ProviderList, ProviderStatus } from "@/components/integrations/types";

/**
 * Settings → Integrations (V26/L2, F2).
 *
 * Reads `GET /api/v1/integrations` only when the owner asks: this page's shipped tests pin
 * that opening Settings issues exactly one read (`/config`) — the same law the Data, Pricing
 * and Site-package sections follow.  There is no polling and no mount fetch.
 *
 * Everything rendered here is the SERVER's answer: masked hints, Configured/Missing badges and
 * the provider's own order.  Saving goes to `PUT /integrations/{provider}`, clearing to
 * `DELETE /integrations/{provider}`, and testing to `POST /integrations/{provider}/test` —
 * whose refusals are shown by name (`E_AUTH`, `E_CONFIG`, …), never softened into a success.
 */

function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

export function IntegrationsSection() {
  const [providers, setProviders] = useState<ProviderStatus[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<ProviderList>("/integrations");
      if (!mounted.current) return;
      setProviders(data.providers);
    } catch (e: unknown) {
      if (mounted.current) setError(errorText(e));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, []);

  const replace = useCallback((next: ProviderStatus) => {
    setProviders((current) =>
      current === null
        ? current
        : current.map((entry) => (entry.provider === next.provider ? next : entry)),
    );
  }, []);

  return (
    <section className="card-surface p-4 space-y-4" aria-label="Integrations">
      <SectionHeader
        kicker="CONNECTIONS"
        title="Integrations"
        action={
          <Button
            size="sm"
            data-testid="settings-integrations-load"
            onClick={load}
            disabled={loading}
          >
            {loading ? "Checking…" : "Check status"}
          </Button>
        }
      />
      <p className="text-sm text-ink">
        The services Polsia calls. A value saved here is stored on the backend and never
        returned by the API — this panel only ever shows a masked hint. Nothing is read until
        you ask.
      </p>

      {error !== null ? (
        <div
          role="alert"
          data-testid="settings-integrations-error"
          className="border border-alert bg-surface p-3"
        >
          <p className="ui-label text-alert">Integration status refused</p>
          <p className="mt-1 text-sm text-ink">{error}</p>
          <Button size="sm" className="mt-2" onClick={() => void load()}>
            Retry
          </Button>
        </div>
      ) : null}

      {providers === null && error === null ? (
        <p className="ui-label text-ink-muted" data-testid="settings-integrations-idle">
          Nothing loaded yet — the provider status is read only when you ask for it.
        </p>
      ) : null}

      {providers !== null ? (
        <div className="space-y-3" data-testid="settings-integrations-list">
          {providers.map((entry) => (
            <ProviderCard key={entry.provider} status={entry} onChange={replace} />
          ))}
        </div>
      ) : null}
    </section>
  );
}
