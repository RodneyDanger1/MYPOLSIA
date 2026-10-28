"use client";

import { Button } from "@/components/ui";
import type {
  ApprovalGate,
  DeployResponse,
  DeployStateResponse,
} from "@/components/sites/types";

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
  /** WHAT is in flight for `busySlug`: the dry-run preview, the live deploy, or the approval mint
   *  + live retry.  The button label follows this, so a live deploy never reads "Previewing…". */
  busyKind: "preview" | "deploy" | "approve" | null;
  previews: Record<string, DeployResponse>;
  /** A live deploy the server refused with `require_approval`: the fingerprint IT computed and
   *  the refusal it wrote.  Presence is what arms the "Approve & deploy" control. */
  gates: Record<string, ApprovalGate>;
  /** What an approved deploy actually did, in the server's own words.  The token is never part of
   *  a notice (it is not even passed to this component). */
  notices: Record<string, string>;
  errors: Record<string, string>;
  onDryRun: (slug: string) => void;
  onDeploy: (slug: string) => void;
  /** Mint the single-use token for `gates[slug].fingerprint`, then repeat the live deploy with it.
   *  The minted token is never rendered (see `/sites` page). */
  onApprove: (slug: string) => void;
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
 * The flow is deliberately staged: Deploy never fires a live deploy.  It asks the server for the
 * dry-run plan (`{dry_run: true}`), which sends nothing and writes nothing, and shows the EXACT call
 * — method, URL, body — with the credential masked by the server (`Bearer …`).  Only Confirm posts
 * `{dry_run: false}`.  While any call is in flight the primary button is disabled and
 * `aria-busy="true"`, so a double click cannot queue two builds.
 *
 * A LIVE deploy is `EXTERNAL_IRREVERSIBLE`, so the FIRST Confirm is normally refused by the policy
 * gate with `E_POLICY_DENIED` / `require_approval`, and the server's own computed fingerprint is in
 * that refusal.  The page turns that refusal into `gates[slug]` and THIS component renders the
 * approval leg: the fingerprint, the server's named refusal, and one "Approve & deploy" control
 * that mints a single-use token (`POST /approvals`) and repeats the live deploy with it.  The token
 * itself is a bearer credential and is NEVER rendered here or anywhere else — this component only
 * receives the gate, never the token.
 *
 * A refusal that approval cannot resolve is NOT dressed as an approval prompt: `E_SANDBOX_BLOCKED`
 * (sandbox on — nothing sent), an exhausted render day cap, the operator kill switch, or "approval
 * required but no approval channel configured" all render as the server's named refusal with no
 * mint control (the page decides by reading `context.decision`/`context.fingerprint`).
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
  const gate = deploy.gates[slug] ?? null;
  const notice = deploy.notices[slug] ?? null;
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
        {!configured || !stateKnown ? null : gate !== null ? (
          <>
            <Button
              size="sm"
              variant="glossy"
              data-testid={`site-deploy-approve-${slug}`}
              aria-label={`Approve and deploy ${name}`}
              aria-busy={busy}
              disabled={busy}
              onClick={() => deploy.onApprove(slug)}
            >
              {busy && deploy.busyKind === "approve" ? "Approving…" : "Approve & deploy"}
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
        ) : preview === null ? (
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
              {busy && deploy.busyKind === "deploy" ? "Deploying…" : "Confirm deploy"}
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

      {gate !== null ? (
        <div
          data-testid={`site-deploy-approval-${slug}`}
          role="group"
          aria-label={`Approval required to deploy ${name}`}
          className="w-full border border-line-strong bg-surface-sunk px-3 py-2 text-xs text-ink"
        >
          <p className="font-semibold">Approval required — nothing was sent to Render.</p>
          <p className="mt-1 text-ink-muted">{gate.message}</p>
          <p className="mt-1 break-all">
            Fingerprint{" "}
            <code className="font-mono" data-testid={`site-deploy-fingerprint-${slug}`}>
              {gate.fingerprint}
            </code>{" "}
            — the server computed this for this site&apos;s slug and Render service id.
          </p>
          <p className="mt-1 text-ink-muted">
            The next click mints a single-use token for that fingerprint (POST /api/v1/approvals)
            and repeats the live deploy with it. The token is a bearer credential and is never
            displayed.
          </p>
        </div>
      ) : null}

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
          <p className="mt-1 text-ink-muted">
            Confirm asks the server for the LIVE deploy. It is an irreversible external action, so
            the server gates it first — the operator kill switch, the Render day cap, the sandbox,
            then a single-use approval token bound to this site&apos;s fingerprint — and answers a
            named refusal if any of those stop it. No build is queued on a refusal.
          </p>
        </div>
      ) : null}

      {notice !== null ? (
        <p
          role="status"
          data-testid={`site-deploy-notice-${slug}`}
          className="w-full border border-line bg-surface px-3 py-2 text-xs text-ink"
        >
          {notice}
        </p>
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
