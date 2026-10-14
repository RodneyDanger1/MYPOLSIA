"use client";

import { cn } from "@/components/ui";

/**
 * The ads page's filter bar — two controls, both wired to the ONLY parameters the real endpoint
 * accepts (`GET /ads/campaigns`: `platform` and `status`, both EXACT-equality reads;
 * `backend/app/api/v1/ads.py:16-17`). Every interaction here re-issues the real read with the real
 * parameter; there is no client-side filtering, no decorative control, and no "apply" theatre —
 * each click/selection IS the query.
 *
 * Platform chips are built from the platform values OBSERVED in settled reads this session (a union;
 * see the page). That set makes no claim about what exists in the database — it is exactly "values
 * that appeared in what we read", so a chip can never miss a platform that does exist. "All
 * platforms" is the filter's off-state, not an enumeration claim.
 *
 * The status select offers the writer-asserted vocabulary (`ads_service.AD_CAMPAIGN_STATUSES` —
 * `active`, `paused`, `ended`, `draft`; the value set `ck_ad_campaigns_status` enforces on
 * PostgreSQL only, which SQLite cannot carry). Rows carrying any other word still render it raw
 * (the card's neutral tone), and the column may be NULL — which this select deliberately does NOT
 * offer, because the endpoint has no way to express "status is not recorded" as a filter; a row
 * with a null status is reachable under "All statuses".
 */

/** Mirror of `backend/app/services/ads_service.py` `AD_CAMPAIGN_STATUSES` — the
 *  `ck_ad_campaigns_status` value set. Kept as a literal because the frontend cannot import
 *  backend code; the write path (`create_campaign`) asserts exactly these four. */
const KNOWN_STATUS_FILTERS = ["active", "paused", "ended", "draft"] as const;

export type CampaignFiltersProps = {
  /** Distinct platform values observed in settled reads (union this session). */
  platforms: string[];
  platform: string | null;
  status: string | null;
  onPlatformChange: (value: string | null) => void;
  onStatusChange: (value: string | null) => void;
};

export function CampaignFilters({
  platforms,
  platform,
  status,
  onPlatformChange,
  onStatusChange,
}: CampaignFiltersProps) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2" data-testid="ads-filters">
      <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Filter by platform">
        <span className="ui-label mr-1 text-ink-muted">Platform</span>
        <button
          type="button"
          className={cn("chip", platform === null ? "chip-dark" : "chip-soft")}
          aria-pressed={platform === null}
          data-testid="ads-platform-all"
          onClick={() => onPlatformChange(null)}
        >
          All platforms
        </button>
        {platforms.map((value) => (
          <button
            key={value}
            type="button"
            className={cn("chip", platform === value ? "chip-dark" : "chip-soft")}
            aria-pressed={platform === value}
            data-testid={`ads-platform-${value}`}
            onClick={() => onPlatformChange(value)}
          >
            {value}
          </button>
        ))}
      </div>

      <div className="flex items-center gap-1">
        <label className="ui-label mr-1 text-ink-muted" htmlFor="ads-status">
          Status
        </label>
        <select
          id="ads-status"
          data-testid="ads-status-select"
          className="ui-label border border-line bg-surface px-1 py-[3px] text-ink"
          value={status ?? ""}
          onChange={(event) => onStatusChange(event.target.value === "" ? null : event.target.value)}
        >
          <option value="">All statuses</option>
          {KNOWN_STATUS_FILTERS.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
