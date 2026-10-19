"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { Card } from "@/components/ui";
import { api } from "@/lib/api";

import { PanelError, PanelSkeleton, readErrorText } from "./states";

/** The wire row of `GET /ads/campaigns` (`AdCampaignResponse`); `status`/`daily_budget_usd`/
 *  `total_spent_usd` are nullable server-side — null means "not recorded", never a fabricated
 *  `$0.00`. Declared locally — the shared api module does not carry the type. */
type Campaign = {
  id: number;
  platform: string;
  name: string;
  status: string | null;
  daily_budget_usd: number | null;
  total_spent_usd: number | null;
};

const WIDGET_LIMIT = 5;

/** The house money shape (finance/format.ts law): thousands separators, always two decimals;
 *  a non-finite reading is the not-measured marker, never `$NaN`. The null guard at each call
 *  site keeps "not recorded" distinct from a measured 0.00. */
function usdText(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/**
 * Right column, bottom: the live `Meta Ads` widget's slot, adapted to the real surface this repo
 * has — `GET /ads/campaigns` (newest id first).
 *
 * The live widget's `Not running yet` / budget-setup copy describes Meta account plumbing this
 * backend does not have; the honest equivalent is this endpoint's real rows, and its empty state
 * says what to do instead of pretending a Meta account is one click away.
 */
export function AdsWidget() {
  const [campaigns, setCampaigns] = useState<Campaign[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // V24/L5: the failure's Retry re-issues this read.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setCampaigns(null);
    setError(null);
    api
      .get<Campaign[]>(`/ads/campaigns?limit=${WIDGET_LIMIT}`)
      .then((data) => {
        if (!cancelled) setCampaigns(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(readErrorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const loading = campaigns === null && !error;

  return (
    <Card
      title="Ads"
      action={
        <Link href="/ads" className="ui-label text-link hover:underline">
          View all campaigns
        </Link>
      }
    >
      {loading ? <PanelSkeleton rows={2} /> : null}
      {!loading && error ? (
        <PanelError
          label="ad campaigns"
          message={error}
          onRetry={() => setAttempt((n) => n + 1)}
          retryTestId="ads-widget-retry"
        />
      ) : null}
      {!loading && !error && campaigns && campaigns.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No campaigns yet. The ads agent creates campaigns during its cycles — run it from the
          Agents page, or open Ads for the full list.
        </p>
      ) : null}
      {!loading && !error && campaigns && campaigns.length > 0 ? (
        <LatestCampaign campaigns={campaigns} />
      ) : null}
    </Card>
  );
}

function LatestCampaign({ campaigns }: { campaigns: Campaign[] }) {
  // `list_campaigns` orders by `AdCampaign.id DESC`, so the first row is the newest.
  const latest = campaigns[0];
  return (
    <div>
      <p className="text-sm font-bold text-ink">{latest.name}</p>
      <p className="ui-label mt-0.5 text-ink-muted">
        {latest.platform} · {latest.status ?? "—"}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <span className="ui-label text-ink-muted">
          <span className="text-ink">{usdText(latest.total_spent_usd)}</span>{" "}
          spent
        </span>
        <span className="ui-label text-ink-muted">
          <span className="text-ink">{usdText(latest.daily_budget_usd)}</span>
          /day budget
        </span>
      </div>
      <p className="ui-label mt-2 text-ink-muted">{campaigns.length} most recent loaded</p>
    </div>
  );
}
