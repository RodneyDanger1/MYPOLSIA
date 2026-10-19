"use client";

import { Button } from "@/components/ui";
import type { DeployResponse, DeployStateResponse } from "@/components/sites/types";

/**
 * The deploy board the `/sites` page keeps for EVERY row, keyed by slug.
 *
 * Kept as one object (not one prop per row) so `SiteList`/`SiteDeployControl` stay presentational:
 * the page owns the calls, the components own the words.  `busySlug` is the single in-flight slug —
 * a deploy is a deliberate, expensive action, not something to fan out by clicking five rows.
 */
export type DeployBoard = {
  states: Record<string, DeployStateResponse>;
  stateErrors: Record<string, string>;
  busySlug: string | null;
  /** WHAT is in flight for `busySlug`: the dry-run preview or the live deploy.  The button label
   *  follows this, so a live deploy never reads "Previewing…". */
  busyKind: "preview" | "deploy" | null;
  previews: Record<string, DeployResponse>;
  errors: Record<string, string>;
  onDryRun: (slug: string) => void;
  onDeploy: (slug: string) => void;
  onCancel: (slug: string) => void;
};

/**
 * The one badge line for a row — four honest states plus the two absences:
 *
 *  * `Reading deploy state…` while the read is in flight (never "never deployed" before it lands);
 *  * `Deploy state unread — <reason>` when the read failed (a failed read is NOT "never deployed");
 *  * `Not configured — set RENDER_API_KEY and RENDER_SERVICE_ID` when the server says so: the row
 *    then renders NO deploy button at all (no dead control);
 *  * `Never deployed` / `Dry-run ok — nothing sent` / `Failed — <code>: <reason>` /
 *    `Deploy submitted — dep-… (Render status: created)` / `Deployed — <url>`.
 *
 * A dry-run preview is the row's newest fact, so it wins over the stored record until Confirm or
 * Cancel — after Confirm the server's stored state takes over again.
 */
export function deployBadgeText(
  state: DeployStateResponse | null,
  stateError: string | null,
  preview: DeployResponse | null,
): string {
  if (preview !== null && preview.dry_run) return "Dry-run ok — nothing sent";
  if (state === null && stateError !== null) return `Deploy state unread — ${stateError}`;
  if (state === null) return "Reading deploy state…";
  if (!state.configured) {
    const missing = state.missing.length > 0 ? state.missing.join(" and ") : "RENDER_API_KEY and RENDER_SERVICE_ID";
    return `Not configured — set ${missing}`;
  }
  const last = state.last_deploy;
  if (last === null) return "Never deployed";
  if (last.state === "failed") {
    const code = last.error?.code ? `${last.error.code}: ` : "";
    const reason = last.error?.message ?? "Render did not give a reason";
    return `Failed — ${code}${reason}`;
  }
  if (last.state === "deployed") {
    return `Deployed — ${last.url ?? last.deploy_id ?? "Render reports it live"}`;
  }
  if (last.state === "submitted") {
    const id = last.deploy_id ?? "no deploy id recorded";
    return last.status
      ? `Deploy submitted — ${id} (Render status: ${last.status})`
      : `Deploy submitted — ${id}`;
  }
  return `Deploy ${last.state}`;
}

/**
 * One site's deploy surface: the status badge, the Deploy button, and — after the dry run — the
 * planned-call preview with Confirm/Cancel.
 *
 * The two-step flow is deliberate: Deploy never fires a live deploy.  It asks the server for the
 * dry-run plan (`{dry_run: true}`), which sends nothing and writes nothing, and shows the EXACT call
 * — method, URL, body — with the credential masked by the server (`Bearer …`).  Only Confirm posts
 * `{dry_run: false}`.  While either call is in flight the primary button is disabled and
 * `aria-busy="true"`, so a double click cannot queue two builds.
 *
 * With `configured: false` the button is NOT RENDERED at all — the badge names the missing
 * environment variables.  A control that exists but can never work is the dead-button defect this
 * page refuses to ship.
 */
export function SiteDeployControl({
  slug,
  name,
  deploy,
}: {
  slug: string;
  name: string;
  deploy: DeployBoard;
}) {
  const state = deploy.states[slug] ?? null;
  const stateError = deploy.stateErrors[slug] ?? null;
  const preview = deploy.previews[slug] ?? null;
  const error = deploy.errors[slug] ?? null;
  const busy = deploy.busySlug === slug;
  const configured = state?.configured !== false;
  // No deploy action until the state has landed: while the read is in flight the row shows
  // "Reading deploy state…" and NO button, so an unconfigured row never flashes a control that is
  // about to disappear.  A FAILED read still gets the button — the dry run will name the refusal.
  const stateKnown = state !== null || stateError !== null;

  return (
    <div className="flex w-full flex-wrap items-start gap-2 border-t border-line px-3 py-2">
      <p
        data-testid={`site-deploy-badge-${slug}`}
        className="mr-auto text-xs text-ink-muted"
        aria-live="polite"
      >
        {deployBadgeText(state, stateError, preview)}
      </p>

      <div className="flex items-center gap-2">
        {!configured || !stateKnown ? null : preview === null ? (
          <Button
            size="sm"
            data-testid={`site-deploy-button-${slug}`}
            aria-label={`Deploy ${name} to Render`}
            aria-busy={busy}
            disabled={busy}
            onClick={() => deploy.onDryRun(slug)}
          >
            {busy && deploy.busyKind === "preview" ? "Previewing…" : "Deploy"}
          </Button>
        ) : (
          <>
            <Button
              size="sm"
              variant="glossy"
              data-testid={`site-deploy-button-${slug}`}
              aria-label={`Confirm deploy ${name}`}
              aria-busy={busy}
              disabled={busy}
              onClick={() => deploy.onDeploy(slug)}
            >
              {busy ? "Deploying…" : "Confirm deploy"}
            </Button>
            <Button
              size="sm"
              data-testid={`site-deploy-cancel-${slug}`}
              aria-label={`Cancel deploy of ${name}`}
              disabled={busy}
              onClick={() => deploy.onCancel(slug)}
            >
              Cancel
            </Button>
          </>
        )}
      </div>

      {preview !== null ? (
        <div
          data-testid={`site-deploy-preview-${slug}`}
          aria-label={`Planned deploy for ${name}`}
          className="w-full border border-line bg-surface px-3 py-2 text-xs text-ink"
        >
          <p className="mb-1 font-semibold">
            {preview.plan.method} {preview.plan.url}
          </p>
          <p className="mb-1 break-all text-ink-muted">
            Body: {JSON.stringify(preview.plan.body)} · Authorization:{" "}
            {preview.plan.headers.authorization} (redacted)
          </p>
          <p className="text-ink-muted">{preview.message}</p>
        </div>
      ) : null}

      {error !== null ? (
        <p
          role="alert"
          data-testid={`site-deploy-error-${slug}`}
          className="w-full border border-line bg-surface px-3 py-2 text-xs text-ink"
        >
          {error}
        </p>
      ) : null}
    </div>
  );
}
