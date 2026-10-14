"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { Card } from "@/components/ui";
import { api } from "@/lib/api";

import { PanelError, PanelSkeleton } from "./states";

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

  useEffect(() => {
    let cancelled = false;
    api
      .get<Campaign[]>(`/ads/campaigns?limit=${WIDGET_LIMIT}`)
      .then((data) => {
        if (!cancelled) setCampaigns(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

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
      {!loading && error ? <PanelError label="ad campaigns" message={error} /> : null}
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
          <span className="text-ink">
            {latest.total_spent_usd === null ? "—" : `$${latest.total_spent_usd.toFixed(2)}`}
          </span>{" "}
          spent
        </span>
        <span className="ui-label text-ink-muted">
          <span className="text-ink">
            {latest.daily_budget_usd === null ? "—" : `$${latest.daily_budget_usd}`}
          </span>
          /day budget
        </span>
      </div>
      <p className="ui-label mt-2 text-ink-muted">{campaigns.length} most recent loaded</p>
    </div>
  );
}
