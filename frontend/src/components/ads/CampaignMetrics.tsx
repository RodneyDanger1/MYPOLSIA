"use client";

import { useState } from "react";

import { NOT_MEASURED } from "@/components/dashboard/format";
import { countText, usdText } from "@/components/finance/format";
import { Button, DataTable, type Column } from "@/components/ui";

import { useAdsRead } from "./useAdsRead";

/**
 * One stored `ad_metrics` day (`AdMetricResponse`, `app/schemas/social.py:67-89`) — the read
 * `GET /ads/campaigns/{id}/metrics` serves. The metrics writer (`record_ads_run`) stores a row per
 * completed ads run; before this consumer existed the read route had no product surface, so the
 * stored measurement was unreachable from the UI (V22/W1's dead-seam class).
 *
 * Why an expander rather than a prefetch: the campaign list is one read per page load (no polling,
 * C7), and ninety days of metrics per campaign would multiply that read by the number of rows for
 * data most visits do not open. Collapsed means ZERO requests; opening issues exactly one read.
 *
 * Null law: every numeric field is nullable on the schema (`null` = "not measured") and renders
 * the app-wide marker. A measured 0 is a reading: counts print `0`, money `$0.00`, CTR `0.00%`.
 * The three generated ratios are fractions/multiples, not money — CTR renders as a percent, ROAS
 * as a multiple, and CPC keeps four decimals so a real sub-cent cost cannot collapse to `$0.00`.
 */

export type AdMetric = {
  id: number;
  campaign_id: number;
  date: string;
  impressions: number | null;
  clicks: number | null;
  conversions: number | null;
  spend_usd: number | null;
  revenue_usd: number | null;
  ctr: number | null;
  cpc: number | null;
  roas: number | null;
};

/** The route's own default (`ads.py:51`), passed explicitly like every other bounded read. */
export const METRICS_LIMIT = 90;

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** CTR is a fraction on the wire (0.05 = 5%); null/invalid is the marker, 0 is `0.00%`. */
function percentText(value: number | null): string {
  const ratio = finite(value);
  if (ratio === null) return NOT_MEASURED;
  return `${(ratio * 100).toFixed(2)}%`;
}

/** CPC is money but can be sub-cent; four decimals preserve the reading. */
function cpcText(value: number | null): string {
  const amount = finite(value);
  if (amount === null) return NOT_MEASURED;
  return `$${amount.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  })}`;
}

/** ROAS is a multiple of spend; null/invalid is the marker. */
function multipleText(value: number | null): string {
  const multiple = finite(value);
  if (multiple === null) return NOT_MEASURED;
  return `${multiple.toFixed(2)}×`;
}

const METRIC_COLUMNS: Column<AdMetric>[] = [
  { key: "date", header: "DATE", render: (row) => row.date || NOT_MEASURED },
  { key: "impressions", header: "IMPR.", render: (row) => countText(row.impressions) },
  { key: "clicks", header: "CLICKS", render: (row) => countText(row.clicks) },
  { key: "conversions", header: "CONV.", render: (row) => countText(row.conversions) },
  { key: "spend", header: "SPEND", render: (row) => usdText(row.spend_usd) },
  { key: "revenue", header: "REVENUE", render: (row) => usdText(row.revenue_usd) },
  { key: "ctr", header: "CTR", render: (row) => percentText(row.ctr) },
  { key: "cpc", header: "CPC", render: (row) => cpcText(row.cpc) },
  { key: "roas", header: "ROAS", render: (row) => multipleText(row.roas) },
];

/** The expanded body: its own read, its own three states — a failed metrics read can never blank
 *  (or impersonate) the campaign list that hosts it. */
function MetricsBody({ campaignId }: { campaignId: number }) {
  const { data, error, loading } = useAdsRead<unknown>(
    `/ads/campaigns/${campaignId}/metrics?limit=${METRICS_LIMIT}`,
    0,
  );

  if (loading) {
    return (
      <p className="ui-label mt-2 text-ink-muted" aria-busy="true" data-testid={`campaign-metrics-loading-${campaignId}`}>
        Loading stored metrics…
      </p>
    );
  }

  if (error !== null) {
    return (
      <div
        role="alert"
        className="mt-2 border border-alert bg-surface p-2"
        data-testid={`campaign-metrics-error-${campaignId}`}
      >
        <p className="ui-label text-alert">Could not load this campaign&apos;s metrics</p>
        <p className="mt-1 text-sm text-ink">{error}</p>
      </div>
    );
  }

  const rows = Array.isArray(data) ? (data as AdMetric[]) : null;

  if (rows === null) {
    return (
      <p className="mt-2 text-sm text-alert" data-testid={`campaign-metrics-broken-${campaignId}`}>
        The metrics read answered with no list of rows — this is a broken read, not an empty one.
      </p>
    );
  }

  return (
    <div className="mt-2 border border-line px-2 py-1">
      <DataTable<AdMetric>
        label={`Stored metrics for campaign ${campaignId}`}
        columns={METRIC_COLUMNS}
        rows={rows}
        rowKey={(row) => String(row.id)}
        empty={
          <p className="text-sm text-ink-muted" data-testid={`campaign-metrics-empty-${campaignId}`}>
            No stored metric days for this campaign yet — the ads agent records one for each
            completed run.
          </p>
        }
        footer={
          rows.length >= METRICS_LIMIT ? (
            <p className="ui-label text-ink-muted" data-testid={`campaign-metrics-cap-${campaignId}`}>
              Showing the newest {METRICS_LIMIT} days — this read is capped at {METRICS_LIMIT} per
              request.
            </p>
          ) : undefined
        }
      />
    </div>
  );
}

/**
 * The per-campaign metrics expander. Real read on open, real closure on close; no control here
 * writes anything (the backend exposes no campaign write route — `ads.py` serves reads only).
 */
export function CampaignMetrics({
  campaignId,
  campaignName,
}: {
  campaignId: number;
  campaignName: string;
}) {
  const [open, setOpen] = useState(false);
  const panelId = `campaign-metrics-${campaignId}`;

  return (
    <div className="mt-2">
      <Button
        size="sm"
        data-testid={`campaign-metrics-toggle-${campaignId}`}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={`${open ? "Hide" : "Show"} stored metrics for ${campaignName}`}
        onClick={() => setOpen((current) => !current)}
      >
        {open ? "Hide metrics" : "Metrics"}
      </Button>
      {open ? (
        <div id={panelId} data-testid={panelId}>
          <MetricsBody campaignId={campaignId} />
        </div>
      ) : null}
    </div>
  );
}
