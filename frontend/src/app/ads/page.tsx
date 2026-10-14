"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { AdCampaignCard, type AdCampaign } from "@/components/ads/AdCampaignCard";
import { CampaignFilters } from "@/components/ads/CampaignFilters";
import { CAMPAIGNS_LIMIT, buildCampaignsPath } from "@/components/ads/query";
import { AdsSkeleton, ReadError } from "@/components/ads/states";
import { useAdsRead } from "@/components/ads/useAdsRead";

/**
 * Ads — the campaign surface, rebuilt to the live design language (wave U4).
 *
 * Client component on purpose. This page used to be an async Server Component doing
 * `await api.get(...)`. Next prerenders this route (`○ (Static)` in the build output), so that
 * fetch ran at BUILD time and the empty result was baked into the HTML: the page showed its empty
 * state forever, even while the API served rows. The repo's e2e run caught it — `GET
 * /api/v1/ads/campaigns` returned rows and the page still rendered "No campaigns yet.". The old
 * `catch { return [] }` made it worse: a failed read became the words "no data", which is the one
 * thing this project forbids.
 *
 * Three states now, disjoint and each stated in its own words:
 *   loading -> an aria-busy skeleton, because "not read yet" is not "empty"
 *   error   -> the failure NAMED on the page (`ApiError.display`; a non-ApiError shows its own
 *              message), and the value rows are REPLACED, not drawn under the banner (Q-09's class)
 *   empty   -> printed ONLY when the read succeeded and really returned nothing
 *
 * Reads exactly ONE real endpoint — `GET /ads/campaigns` (the backend's only ads route,
 * `backend/app/api/v1/ads.py`) — with the explicit bounded `limit` this page's own rules pass
 * (Q-12: the API serves no total and no cursor, so a full page renders the truncation marker
 * instead of presenting the cap as the whole list; anything shorter renders no marker). Nothing is
 * polled (C7).
 *
 * The two filter controls are real: each one re-issues the read with the endpoint's own
 * `platform`/`status` query parameters (`components/ads/query.ts`) — proven by
 * `src/__tests__/u4-ads.test.tsx`. Platform chips come from values OBSERVED in settled reads (a
 * union this session); the set is never presented as an enumeration of the database (Q-12-class
 * honesty). The status select offers the writer-asserted vocabulary only — see the filter bar's
 * comment for what it deliberately does not claim.
 *
 * Refused by omission, not drawn: no create/edit/pause/delete control exists because no write route
 * exists for campaigns in this backend (route inventory: `GET /ads/campaigns` only); a control
 * with no endpoint behind it is the exact defect this overhaul removes.
 *
 * Money, status and timestamps follow the shared nullable-wire laws: `null` is "not recorded" and
 * renders the app-wide marker (never `$0.00`, never a fabricated status word, never "Invalid
 * Date"); a measured `0` is a reading and renders `$0.00`; money is formatted by the finance
 * family (`usdText`), so the audit's Q-13 split (`$3.25 spent` above a raw-float `$12.5/day
 * budget`) cannot come back.
 */
export default function AdsPage() {
  const [platform, setPlatform] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const path = useMemo(() => buildCampaignsPath({ platform, status }), [platform, status]);
  const { data, error, loading } = useAdsRead<unknown>(path, reloadKey);

  const rows = Array.isArray(data) ? (data as AdCampaign[]) : null;
  // A successful response whose body is not a list at all is a broken read, named as one.
  const brokenBody = data !== null && rows === null;
  // `>=` on purpose: a server that overshot its own cap must still be announced as capped.
  const truncated = rows !== null && rows.length >= CAMPAIGNS_LIMIT;
  const filtered = platform !== null || status !== null;

  // The platform chip set: every distinct platform value a settled read has shown this session
  // (union). Real observed values only — never a hard-coded list, never a claimed enumeration.
  const [observedPlatforms, setObservedPlatforms] = useState<string[]>([]);
  useEffect(() => {
    if (rows === null || rows.length === 0) return;
    setObservedPlatforms((previous) => {
      const next = new Set(previous);
      for (const row of rows) {
        if (typeof row.platform === "string" && row.platform !== "") next.add(row.platform);
      }
      // Same size means the same set (values only ever get added) — return the SAME reference so
      // this effect cannot loop.
      return next.size === previous.length ? previous : Array.from(next);
    });
  }, [rows]);

  const refresh = useCallback(() => setReloadKey((count) => count + 1), []);

  return (
    <div className="space-y-4">
      <div className="flex items-end gap-2 border-b border-line pb-1">
        <div className="mr-auto">
          <h1 className="text-3xl font-bold text-ink">Ad Campaigns</h1>
          {rows !== null ? (
            <p className="ui-label text-ink-muted" data-testid="ads-count">
              {rows.length} {rows.length === 1 ? "campaign" : "campaigns"}
            </p>
          ) : null}
        </div>
        <button type="button" className="link ui-label" data-testid="ads-refresh" onClick={refresh}>
          Refresh
        </button>
      </div>

      <CampaignFilters
        platforms={observedPlatforms}
        platform={platform}
        status={status}
        onPlatformChange={setPlatform}
        onStatusChange={setStatus}
      />

      {loading ? <AdsSkeleton /> : null}

      {!loading && (error !== null || brokenBody) ? (
        <ReadError
          label="the campaign list"
          message={error ?? "the server returned no list of rows"}
        />
      ) : null}

      {!loading && error === null && !brokenBody && rows !== null && rows.length === 0 ? (
        filtered ? (
          <p className="text-sm text-ink-muted" data-testid="ads-empty-filtered">
            No campaigns match these filters. Choose “All platforms” and “All statuses” to read the
            full list.
          </p>
        ) : (
          <p className="text-sm text-ink-muted" data-testid="ads-empty">
            No campaigns yet. The ads agent creates campaigns during its cycles — run it from the
            Agents page.
          </p>
        )
      ) : null}

      {rows !== null && rows.length > 0 ? (
        <div className="space-y-3" data-testid="ads-campaigns">
          {rows.map((campaign) => (
            <AdCampaignCard key={campaign.id} campaign={campaign} />
          ))}
          {/* Q-12: the read is capped at CAMPAIGNS_LIMIT and the API serves no total, so a full
              page says exactly that instead of presenting the cap as the whole list. */}
          {truncated ? (
            <p className="ui-label text-ink-muted" data-testid="ads-truncation">
              Showing the first {CAMPAIGNS_LIMIT} — the API caps this read at {CAMPAIGNS_LIMIT} per
              request.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
