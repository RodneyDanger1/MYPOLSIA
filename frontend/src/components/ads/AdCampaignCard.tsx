import type { HTMLAttributes } from "react";

import { NOT_MEASURED, absoluteStamp } from "@/components/dashboard/format";
import { textOrDash, usdText } from "@/components/finance/format";
import { Card, Chip, type ChipVariant, cn } from "@/components/ui";

import { CampaignMetrics } from "./CampaignMetrics";

/**
 * One campaign row of `GET /ads/campaigns` (`AdCampaignResponse`, `app/schemas/social.py:47-64`)
 * and its card renderer, on the U1 design language.
 *
 * Every field the wire serves is carried — including the three the old page-local type dropped
 * (`external_id`, `goal`, `created_at`) — because a rebuild that keeps hiding served data is not a
 * rebuild. Nullable semantics, in the schema's own words:
 *  - `status` null is "not recorded" and "the frontend renders the dash for it" (schema docstring);
 *    a non-null word renders raw, with the writer-asserted four (`active/paused/ended/draft` —
 *    `ads_service.AD_CAMPAIGN_STATUSES`, the value set `ck_ad_campaigns_status` enforces on
 *    PostgreSQL only) getting distinct named tones, and any other word a neutral tone of its own so
 *    it cannot masquerade as a known status (the Q-25 discipline).
 *  - `daily_budget_usd`/`total_spent_usd` null is "not recorded" — the marker, never `$0.00` — and
 *    money is formatted by the SHARED finance family (`usdText`: thousands separators, two
 *    decimals, non-finite -> marker), which is what kills the audit's Q-13 split where this page
 *    printed `$3.25 spent` above a raw-float `$12.5/day budget`.
 *  - `created_at` null renders "date unknown" (the app-wide placeholder), never "Invalid Date".
 *
 * Deliberately presentational apart from the ONE metrics expander below: no write route exists for
 * campaigns in this backend (route inventory: `GET /ads/campaigns` + the per-campaign metrics read,
 * `backend/app/api/v1/ads.py`), so no edit/pause/delete control is drawn for it (refused by
 * omission, the outreach/social precedent). The expander reads `GET /ads/campaigns/{id}/metrics`
 * when opened — the stored-measurement seam V22/L3 closed; it writes nothing.
 */
export type AdCampaign = {
  id: number;
  /** NOT NULL in the model (`models/ads.py:44`) — defensive rendering only. */
  platform: string;
  external_id: string | null;
  /** NOT NULL in the model (`models/ads.py:46`). */
  name: string;
  goal: string | null;
  /** Open nullable string on the wire — see the class comment for the rendering law. */
  status: string | null;
  daily_budget_usd: number | null;
  total_spent_usd: number | null;
  created_at: string | null;
};

type ChipTreatment = { variant: ChipVariant; className: string };

/** The writer-asserted statuses (`AD_CAMPAIGN_STATUSES`) — each with its own tone. */
const NAMED_STATUS_TREATMENT: Record<string, ChipTreatment> = {
  active: { variant: "soft", className: "text-green-800" },
  paused: { variant: "soft", className: "text-blue-800" },
  ended: { variant: "soft", className: "text-ink" },
  draft: { variant: "soft", className: "text-ink-muted" },
};

/** Null status is "not recorded": the not-measured marker in a dashed gray chip — the one treatment
 *  no named status shares, so an unrecorded status can never wear a real status's clothes. */
const UNMEASURED_TREATMENT: ChipTreatment = {
  variant: "soft",
  className: "border-dashed border-line-strong bg-transparent text-ink-muted",
};

/** Any other non-null value is a real word from an open column — rendered neutral, raw, and
 *  visually distinct from both named tones and the unmeasured treatment. */
const OTHER_TREATMENT: ChipTreatment = {
  variant: "soft",
  className: "border-line-strong bg-surface text-ink-muted",
};

/** The status chip: named lookup -> raw-word fallback -> the marker for null (never a fabricated
 *  word, and an unknown value never borrows a known status's tone). */
export function StatusChip({
  status,
  className,
  ...props
}: { status: string | null } & HTMLAttributes<HTMLSpanElement>) {
  if (status === null) {
    return (
      <Chip
        variant={UNMEASURED_TREATMENT.variant}
        data-status-tone="unmeasured"
        title="Status not recorded on this row"
        className={cn(UNMEASURED_TREATMENT.className, className)}
        {...props}
      >
        {NOT_MEASURED}
      </Chip>
    );
  }
  const named = NAMED_STATUS_TREATMENT[status];
  if (named) {
    return (
      <Chip
        variant={named.variant}
        data-status-tone={status}
        className={cn(named.className, className)}
        {...props}
      >
        {status}
      </Chip>
    );
  }
  return (
    <Chip
      variant={OTHER_TREATMENT.variant}
      data-status-tone="other"
      className={cn(OTHER_TREATMENT.className, className)}
      {...props}
    >
      {status}
    </Chip>
  );
}

export function AdCampaignCard({ campaign }: { campaign: AdCampaign }) {
  // `platform` is NOT NULL in the model; the marker is belt-and-braces for a hostile body, never a
  // normal path.
  const platform =
    typeof campaign.platform === "string" && campaign.platform !== ""
      ? campaign.platform
      : NOT_MEASURED;
  // The only timestamp this row carries: labelled when present, the app-wide "date unknown" when
  // absent — the cell is never deleted and never prints "Invalid Date".
  const created = campaign.created_at
    ? `Created ${absoluteStamp(campaign.created_at)}`
    : "date unknown";

  return (
    <Card data-testid="campaign-row" className="border-line-strong">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <p className="mr-auto text-sm font-bold text-ink">{textOrDash(campaign.name)}</p>
        <span className="ui-label text-ink-muted">
          <span className="text-ink">{usdText(campaign.total_spent_usd)}</span> spent
        </span>
        <span className="ui-label text-ink-muted">
          <span className="text-ink">{usdText(campaign.daily_budget_usd)}</span>/day budget
        </span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Chip variant="soft" data-testid="campaign-platform" className="text-ink-muted">
          {platform}
        </Chip>
        <StatusChip status={campaign.status ?? null} data-testid="campaign-status" />
        {campaign.goal ? <span className="ui-label text-ink-muted">goal {campaign.goal}</span> : null}
        <span data-testid="campaign-created" className="ui-label text-ink-muted">
          {created}
        </span>
        {campaign.external_id ? (
          <span className="ui-label text-ink-muted">ID {campaign.external_id}</span>
        ) : null}
      </div>

      <CampaignMetrics campaignId={campaign.id} campaignName={textOrDash(campaign.name)} />
    </Card>
  );
}
