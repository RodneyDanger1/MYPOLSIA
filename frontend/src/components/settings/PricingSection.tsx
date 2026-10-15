"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { Button, DataTable, SectionHeader } from "@/components/ui";

/**
 * Settings → Pricing (V11/W4) — a READ-ONLY view of the dated pricing-catalog snapshot
 * (`backend/app/api/v1/pricing.py` + `pricing_service.py`, dataset
 * `backend/app/data/pricing_catalog.json`).
 *
 * GET /api/v1/pricing?limit=50 answers `{query, count, items, catalog_as_of, staleness_days,
 * stale}` — the rows are market-rate CLAIMS with their sources, and the provenance block is
 * COMPUTED per call. Every nullable value is rendered as the em dash, never fabricated:
 *   - a row's `hours` is `{low, high} | null`; a price is a number or absent;
 *   - `catalog_as_of`/`staleness_days`/`stale` may each be null ("age unknown") — null staleness
 *     is NOT a freshness claim, and `stale: true` renders an explicit re-verify alarm.
 *
 * The read fires on the load button, never on mount: this page's shipped tests pin that opening
 * Settings starts no request beyond the config form and the DeepSeek section.
 */

type PricingHours = { low: number | null; high: number | null } | null;

type PricingRow = {
  service: string;
  price_low: number | null;
  price_high: number | null;
  model: string | null;
  hours: PricingHours;
  source_url: string | null;
  fetch_type: string | null;
  date: string | null;
  source_label: string | null;
  taxable_hint: string | null;
};

type PricingCatalog = {
  query: string | null;
  count: number;
  items: PricingRow[];
  catalog_as_of: string | null;
  staleness_days: number | null;
  stale: boolean | null;
};

function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

/** Exact USD or null: an integer renders without cents, otherwise two digits; never `$0` for
 *  a value the wire did not carry. */
function usd(value: unknown): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const formatted = Number.isInteger(value)
    ? value.toLocaleString("en-US")
    : value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `$${formatted}`;
}

function priceText(low: unknown, high: unknown): string {
  const a = usd(low);
  const b = usd(high);
  if (a === null && b === null) return "—";
  if (a !== null && b !== null) return a === b ? a : `${a}–${b}`;
  return a ?? b ?? "—";
}

function hoursText(hours: PricingHours): string {
  if (!hours || typeof hours !== "object") return "—";
  const low = typeof hours.low === "number" ? hours.low : null;
  const high = typeof hours.high === "number" ? hours.high : null;
  if (low === null && high === null) return "—";
  if (low !== null && high !== null) return low === high ? `${low} hrs` : `${low}–${high} hrs`;
  return `${low ?? high} hrs`;
}

export function PricingSection() {
  const [catalog, setCatalog] = useState<PricingCatalog | null>(null);
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
      const response = await api.get<PricingCatalog>("/pricing?limit=50");
      if (!mounted.current) return;
      setCatalog(response);
    } catch (e: unknown) {
      if (mounted.current) setError(errorText(e));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, []);

  const items = Array.isArray(catalog?.items) ? catalog.items : [];
  const asOf = catalog?.catalog_as_of ?? null;
  const staleness = catalog?.staleness_days ?? null;
  const stale = catalog?.stale ?? null;
  const count = typeof catalog?.count === "number" ? catalog.count : items.length;

  return (
    <section className="card-surface p-4 space-y-4" aria-label="Pricing catalog">
      <SectionHeader
        kicker="REFERENCE"
        title="Pricing catalog"
        action={
          <Button size="sm" data-testid="settings-pricing-load" onClick={load} disabled={loading}>
            {loading ? "Loading…" : "Load pricing catalog"}
          </Button>
        }
      />
      <p className="text-sm text-ink">
        A DATED SNAPSHOT of market-rate claims with their sources (GET /api/v1/pricing) — not live
        pricing, and nothing here is a quote.
      </p>

      {error !== null ? (
        <div role="alert" data-testid="settings-pricing-error" className="border border-alert bg-surface p-3">
          <p className="ui-label text-alert">Pricing catalog refused</p>
          <p className="mt-1 text-sm text-ink">{error}</p>
        </div>
      ) : null}

      {catalog === null && error === null ? (
        <p className="ui-label text-ink-muted">
          Nothing loaded yet — the table reads the snapshot only when you ask for it.
        </p>
      ) : null}

      {catalog !== null ? (
        <div className="space-y-2">
          <p className="ui-label text-ink-muted" data-testid="settings-pricing-provenance">
            {asOf === null
              ? "Snapshot as-of date unknown (no row states one) — freshness cannot be claimed."
              : `Snapshot dated ${asOf}${
                  typeof staleness === "number" ? ` — ${staleness} day${staleness === 1 ? "" : "s"} old` : ""
                }.`}
          </p>
          {stale === true ? (
            <p role="status" data-testid="settings-pricing-stale" className="border border-alert bg-surface px-3 py-2 text-sm text-alert">
              STALE: this snapshot is at or over the 180-day threshold — re-verify any row before
              quoting it.
            </p>
          ) : null}
          {stale === null ? (
            <p className="ui-label text-ink-muted">
              Staleness cannot be computed for this snapshot (the server reported no age).
            </p>
          ) : null}

          <DataTable
            columns={[
              { key: "service", header: "Service", render: (row: PricingRow) => row.service },
              {
                key: "price",
                header: "Price",
                render: (row: PricingRow) => priceText(row.price_low, row.price_high),
              },
              { key: "model", header: "Model", render: (row: PricingRow) => row.model ?? "—" },
              { key: "hours", header: "Hours", render: (row: PricingRow) => hoursText(row.hours) },
              { key: "date", header: "As of", render: (row: PricingRow) => row.date ?? "—" },
              {
                key: "source",
                header: "Source",
                render: (row: PricingRow) =>
                  row.source_url ? (
                    <a
                      className="underline underline-offset-2"
                      href={row.source_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {row.source_label ?? row.source_url}
                    </a>
                  ) : (
                    (row.source_label ?? "—")
                  ),
              },
            ]}
            rows={items}
            rowKey={(row) => `${row.service}::${row.date ?? ""}`}
            empty={
              <span className="ui-label text-ink-muted" data-testid="settings-pricing-empty">
                The snapshot answered zero rows.
              </span>
            }
          />
          <p className="ui-label text-ink-muted">
            {count} row{count === 1 ? "" : "s"} served
            {catalog.query ? ` for “${catalog.query}”` : " (full snapshot, file order)"}.
          </p>
        </div>
      ) : null}
    </section>
  );
}
