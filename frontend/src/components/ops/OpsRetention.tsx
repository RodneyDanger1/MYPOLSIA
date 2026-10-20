"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { relativeTime } from "@/components/dashboard/format";
import { errorText } from "@/components/finance/useFinanceRead";
import { ListSkeleton, ReadError } from "@/components/finance/states";
import { Button, Card, Chip } from "@/components/ui";
import { api } from "@/lib/api";

/**
 * Run-workspace retention — the owner's manual surface for the V26 policy.
 *
 * The policy lives in `tool_registry.cleanup_expired_run_workspaces`: keep the newest 20 run
 * directories under `<workspace>/runs/` and delete only those older than 72 h. The daily beat
 * task (`run-workspace-cleanup`, 04:15 UTC) applies it automatically; this panel is how the owner
 * previews or applies it by hand.
 *
 * Honesty rules this panel keeps:
 *  - "last result" is the server's newest recorded activity row (`GET`), never client memory
 *    dressed up as history; `{data: null}` renders an explicit "nothing recorded yet" sentence;
 *  - a recorded row whose `created_at` came back null says "no timestamp recorded" — V28/L1: the
 *    panel used to call it "just now" and "the result of this run", inventing an instant;
 *  - Dry run and Apply are the SAME server verb, differing only in `{apply: …}` — the panel never
 *    simulates a delete locally;
 *  - Apply is a TWO-CLICK verb: the first click only arms a confirmation ("Confirm apply"), the
 *    second sends `{apply: true}`. Cancel defuses it. There is no one-click destructive path;
 *  - a refusal is rendered by `errorText` — the server's code, message and fix hint, never
 *    rewritten into a friendlier sentence;
 *  - `applied` comes from the RESPONSE, not from which button was pressed: the chip says what the
 *    server says it did;
 *  - V29/L2: the server caps the name lists (50 entries each). `removed_truncated` /
 *    `skipped_truncated` are OPTIONAL — absent means the server did not say, so the panel makes no
 *    truncation claim at all; `true` prints the cap sentence with the still-complete count.
 */
const ROUTE = "/ops/run-workspace-cleanup";

export type RunWorkspaceCleanupPolicy = {
  keep_latest: number;
  max_age_hours: number;
};

export type RunWorkspaceCleanupSkip = { name: string; reason: string };

export type RunWorkspaceCleanupResult = {
  runs_dir: string;
  count: number;
  kept: number;
  removed: string[];
  skipped: RunWorkspaceCleanupSkip[];
  policy: RunWorkspaceCleanupPolicy;
  applied: boolean;
  /** V29/L1 adds these to the route; until it lands they are absent. Optional on purpose: absent
   *  is "the server did not say", never "not truncated". */
  removed_truncated?: boolean;
  skipped_truncated?: boolean;
};

type CleanupRecord = {
  activity_id: number;
  created_at: string | null;
  trigger: string;
  result: RunWorkspaceCleanupResult;
};

/** What the panel currently shows, and WHERE it came from. `at === null` has two meanings — a
 *  stored record whose `created_at` the server could not state (`RunWorkspaceCleanupRecord.created_at`
 *  is `UtcDatetime | None`, ops.py:328) and the result of a POST pressed in this session — so the
 *  provenance travels with the state instead of being re-guessed from a null. V28/L1: the panel used
 *  to print "just now" for BOTH, inventing an instant for a cleanup that may be days old. */
type Shown = {
  result: RunWorkspaceCleanupResult;
  at: string | null;
  trigger: string;
  /** True only for the POST's own answer (the one case where "now" is the clock). */
  justRan: boolean;
};

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isResult(value: unknown): value is RunWorkspaceCleanupResult {
  if (value === null || typeof value !== "object") return false;
  const candidate = value as Partial<RunWorkspaceCleanupResult>;
  const policy = candidate.policy as Partial<RunWorkspaceCleanupPolicy> | undefined;
  return (
    typeof candidate.runs_dir === "string" &&
    isNumber(candidate.count) &&
    isNumber(candidate.kept) &&
    Array.isArray(candidate.removed) &&
    candidate.removed.every((name) => typeof name === "string") &&
    Array.isArray(candidate.skipped) &&
    candidate.skipped.every(
      (item) =>
        item !== null &&
        typeof item === "object" &&
        typeof (item as RunWorkspaceCleanupSkip).name === "string" &&
        typeof (item as RunWorkspaceCleanupSkip).reason === "string",
    ) &&
    policy !== undefined &&
    policy !== null &&
    isNumber(policy.keep_latest) &&
    isNumber(policy.max_age_hours) &&
    typeof candidate.applied === "boolean" &&
    // Optional flags: absent OR null is fine (an older row did not say); a present value must be
    // a real boolean, never a truthy string.  V29 stores these as null when not truncated.
    (candidate.removed_truncated == null ||
      typeof candidate.removed_truncated === "boolean") &&
    (candidate.skipped_truncated == null ||
      typeof candidate.skipped_truncated === "boolean")
  );
}

function isRecord(value: unknown): value is CleanupRecord {
  if (value === null || typeof value !== "object") return false;
  const candidate = value as Partial<CleanupRecord>;
  return (
    isNumber(candidate.activity_id) &&
    (candidate.created_at === null || typeof candidate.created_at === "string") &&
    typeof candidate.trigger === "string" &&
    isResult(candidate.result)
  );
}

/** `72` not `72.0`; a fractional policy keeps one decimal. */
export function policyHoursText(value: number): string {
  if (!Number.isFinite(value)) return String(value);
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function triggerText(trigger: string): string {
  if (trigger === "beat") return "the daily beat";
  if (trigger === "owner") return "the owner";
  return trigger;
}

export function OpsRetention() {
  const [last, setLast] = useState<CleanupRecord | null>(null);
  const [shown, setShown] = useState<Shown | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<"dry" | "apply" | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .get<unknown>(ROUTE)
      .then((body) => {
        if (cancelled) return;
        if (body === null || typeof body !== "object" || !("data" in body)) {
          setError("the server returned no run-workspace cleanup record");
          setLoading(false);
          return;
        }
        const data = (body as { data: unknown }).data;
        if (data === null) {
          setLast(null);
          setShown(null);
          setLoading(false);
          return;
        }
        if (!isRecord(data)) {
          setError("the stored cleanup result is not the documented shape");
          setLoading(false);
          return;
        }
        setLast(data);
        setShown({
          result: data.result,
          at: data.created_at,
          trigger: data.trigger,
          justRan: false,
        });
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(errorText(e));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const reload = useCallback(() => setReloadKey((current) => current + 1), []);

  const run = async (apply: boolean) => {
    if (busy !== null) return;
    if (apply && !confirming) {
      setConfirming(true);
      setNotice(null);
      setActionError(null);
      return;
    }
    setBusy(apply ? "apply" : "dry");
    setConfirming(false);
    setNotice(null);
    setActionError(null);
    try {
      const result = await api.post<RunWorkspaceCleanupResult>(ROUTE, { apply });
      if (!alive.current) return;
      if (!isResult(result)) {
        setActionError("the server's cleanup response is not the documented shape");
        return;
      }
      setShown({ result, at: null, trigger: "owner", justRan: true });
      setNotice(
        result.applied
          ? `Applied: removed ${result.count} expired run workspace(s); ${result.kept} kept.`
          : `Dry run: ${result.count} run workspace(s) would be removed; ${result.kept} kept. Nothing was deleted.`,
      );
    } catch (e) {
      if (alive.current) setActionError(errorText(e));
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const busyAny = busy !== null;

  return (
    <Card
      data-testid="ops-retention"
      title="Run workspace retention"
      kicker="RUN WORKSPACES · DRY RUN BY DEFAULT"
      action={
        <Chip
          variant={shown?.result.applied ? "dark" : "soft"}
          data-testid="ops-retention-applied"
        >
          {shown === null ? "—" : shown.result.applied ? "APPLIED" : "DRY RUN"}
        </Chip>
      }
    >
      <p className="text-sm text-ink-muted">
        The retention policy keeps the newest {shown?.result.policy.keep_latest ?? 20} run
        directories under the workspace&rsquo;s <code className="font-mono">runs/</code> folder and
        removes only those older than {policyHoursText(shown?.result.policy.max_age_hours ?? 72)} h.
        The daily beat applies it at 04:15 UTC; the buttons below run the same verb by hand —
        Dry run changes nothing.
      </p>

      {loading ? <ListSkeleton testId="ops-retention-loading" /> : null}

      {!loading && error !== null ? (
        <ReadError
          label="the last cleanup result"
          message={error}
          onRetry={reload}
          retryTestId="ops-retention-retry"
        />
      ) : null}

      {!loading && error === null && shown === null ? (
        <p
          data-testid="ops-retention-empty"
          className="mt-2 border border-line-strong bg-surface-sunk px-3 py-2 text-sm text-ink-muted"
        >
          No cleanup has been recorded yet — the daily beat task runs at 04:15 UTC, or press Dry
          run to preview the workspace now.
        </p>
      ) : null}

      {!loading && error === null && shown !== null ? (
        <div className="mt-2" data-testid="ops-retention-result">
          <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <div>
              <dt className="ui-label text-ink-muted">
                {shown.result.applied ? "REMOVED" : "WOULD REMOVE"}
              </dt>
              <dd className="text-xl font-bold text-ink" data-testid="ops-retention-count">
                {shown.result.count}
              </dd>
            </div>
            <div>
              <dt className="ui-label text-ink-muted">KEPT</dt>
              <dd className="text-xl font-bold text-ink" data-testid="ops-retention-kept">
                {shown.result.kept}
              </dd>
            </div>
            <div className="md:col-span-2">
              <dt className="ui-label text-ink-muted">POLICY</dt>
              <dd className="text-sm text-ink" data-testid="ops-retention-policy">
                keep newest {shown.result.policy.keep_latest} · max age{" "}
                {policyHoursText(shown.result.policy.max_age_hours)} h
              </dd>
            </div>
          </dl>

          <p className="mt-2 text-xs text-ink-muted">
            <span className="ui-label">RUNS DIR</span>{" "}
            <code className="font-mono">{shown.result.runs_dir}</code>
          </p>

          <div className="mt-2" data-testid="ops-retention-removed">
            <span className="ui-label text-ink-muted">
              {shown.result.applied ? "REMOVED" : "WOULD REMOVE"}
            </span>
            {shown.result.removed.length === 0 ? (
              <p className="text-sm text-ink-muted">
                Nothing — every run directory is inside the policy.
              </p>
            ) : (
              <span className="mt-1 flex flex-wrap items-center gap-1">
                {shown.result.removed.map((name) => (
                  <Chip key={name} variant="soft" className="font-mono text-[10px]">
                    {name}
                  </Chip>
                ))}
              </span>
            )}
            {/* V29/L2 T2.2: only when the response SAYS the list was capped. The count is the
                server's own total, so it is still complete even though the names are not. */}
            {shown.result.removed_truncated === true ? (
              <p className="mt-1 text-xs text-ink-muted" data-testid="ops-retention-truncated">
                The name list was capped at {shown.result.removed.length}; the count of{" "}
                {shown.result.count} is complete.
              </p>
            ) : null}
          </div>

          <div className="mt-2" data-testid="ops-retention-skipped">
            <span className="ui-label text-ink-muted">SKIPPED</span>
            {shown.result.skipped.length === 0 ? (
              <p className="text-sm text-ink-muted">Nothing skipped.</p>
            ) : (
              <ul className="mt-1 space-y-1 text-sm text-ink-muted">
                {shown.result.skipped.map((item) => (
                  <li key={`${item.name}:${item.reason}`}>
                    <code className="font-mono text-xs text-ink">{item.name}</code> — {item.reason}
                  </li>
                ))}
              </ul>
            )}
            {/* V29/L2 T2.2: a capped SKIPPED list hides names, not deletions — say only that the
                counts above are complete. */}
            {shown.result.skipped_truncated === true ? (
              <p
                className="mt-1 text-xs text-ink-muted"
                data-testid="ops-retention-skipped-truncated"
              >
                The skipped name list was capped at {shown.result.skipped.length}; the counts above
                are complete.
              </p>
            ) : null}
          </div>

          <p className="mt-2 text-xs text-ink-muted" data-testid="ops-retention-trigger">
            Last run: {triggerText(shown.trigger)} ·{" "}
            {shown.justRan
              ? "just now"
              : shown.at === null
                ? "no timestamp recorded"
                : relativeTime(shown.at)}
          </p>
        </div>
      ) : null}

      {confirming ? (
        <p
          data-testid="ops-retention-confirm-hint"
          className="mt-3 border border-line-strong bg-surface-sunk px-3 py-2 text-sm text-ink"
        >
          Confirm apply: the next click deletes the expired run directories under{" "}
          <code className="font-mono">{shown?.result.runs_dir ?? "the workspace runs/ folder"}</code>
          . Recent directories and anything not shaped like a run id are never touched.
        </p>
      ) : null}

      {notice !== null ? (
        <p
          role="status"
          data-testid="ops-retention-notice"
          className="mt-2 border border-line-strong bg-surface-sunk px-3 py-2 text-sm text-ink"
        >
          {notice}
        </p>
      ) : null}

      {actionError !== null ? (
        <p role="alert" data-testid="ops-retention-action-error" className="mt-2 text-sm text-alert">
          {actionError}
        </p>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          data-testid="ops-retention-dry-run"
          aria-busy={busy === "dry" || undefined}
          disabled={busyAny}
          onClick={() => void run(false)}
        >
          {busy === "dry" ? "Reading…" : "Dry run"}
        </Button>
        <Button
          size="sm"
          variant={confirming ? "glossy" : "bevel"}
          data-testid="ops-retention-apply"
          aria-busy={busy === "apply" || undefined}
          disabled={busyAny}
          onClick={() => void run(true)}
        >
          {busy === "apply" ? "Applying…" : confirming ? "Confirm apply" : "Apply"}
        </Button>
        {confirming ? (
          <button
            type="button"
            className="link ui-label"
            data-testid="ops-retention-cancel"
            onClick={() => setConfirming(false)}
          >
            Cancel
          </button>
        ) : null}
        <span className="ui-label text-ink-muted" data-testid="ops-retention-basis">
          {shown === null
            ? "no recorded run"
            : shown.justRan
              ? "result of this run"
              : `last recorded row #${last?.activity_id ?? "—"}`}
        </span>
      </div>
    </Card>
  );
}
