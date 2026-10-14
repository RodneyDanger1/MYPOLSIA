/**
 * The ads page's ONE query builder.
 *
 * `GET /ads/campaigns` accepts exactly three parameters — `platform` (str, EXACT equality),
 * `status` (str, EXACT equality) and `limit` (int, 1..200, server default 50) — and nothing else
 * (`backend/app/api/v1/ads.py:16-18`, passed straight into `ads_service.list_campaigns`'s
 * `.where(...)` equality reads). An unset filter OMITS its param entirely: the endpoint defines no
 * empty-string filter, and sending `platform=` would be a value the read cannot honour honestly.
 */

/** The read size this page passes explicitly — the endpoint's own default, never a guessed cap. */
export const CAMPAIGNS_LIMIT = 50;

export type CampaignQueryFilters = {
  /** Exact platform value, or `null` for "no platform filter". */
  platform: string | null;
  /** Exact status value, or `null` for "no status filter". */
  status: string | null;
};

/** `/ads/campaigns?limit=…[&platform=…][&status=…]`, params URL-encoded and stably ordered. */
export function buildCampaignsPath(
  filters: CampaignQueryFilters,
  limit: number = CAMPAIGNS_LIMIT,
): string {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  if (filters.platform !== null && filters.platform !== "") {
    params.set("platform", filters.platform);
  }
  if (filters.status !== null && filters.status !== "") {
    params.set("status", filters.status);
  }
  return `/ads/campaigns?${params.toString()}`;
}
