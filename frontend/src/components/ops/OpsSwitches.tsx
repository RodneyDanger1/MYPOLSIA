"use client";

import { useEffect, useState } from "react";

import { NOT_MEASURED, moneyText } from "@/components/dashboard/format";
import { errorText } from "@/components/finance/useFinanceRead";
import { Button, Card } from "@/components/ui";
import { api } from "@/lib/api";

/**
 * The /ops kill-switch and Auto Mode readings — three EXISTING routes, read and never guessed:
 *
 *   GET /auto-mode     `{active, session|null}` — the bounded window and its live counters
 *   GET /config        `dispatch_paused` — the owner Pause/Resume switch the WORKER enforces
 *                      (`crew_factory.dispatch_preflight`, `agent_tasks`' pre-claim gate)
 *   GET /ops/sandbox   `{sandbox_mode}` — external actions are drafted, never sent
 *
 * HONESTY LIMIT, stated in the card: the `POLSIA_KILL_SWITCH` environment grade ("throttle" /
 * "freeze") is enforced inside the worker (`policy_firewall.kill_grade_from_env`) but NO API route
 * exposes its value, so this surface shows no grade for it rather than inventing one. The two
 * switches that ARE readable are shown with their real source.
 *
 * Null law: `spend_used` null is unmeasured and renders the dash; a measured 0 renders `$0.00`.
 */
type ReadState<T> =
  | { state: "loading" }
  | { state: "ready"; value: T }
  | { state: "error"; message: string };

type AutoSession = {
  tasks_run: number;
  spend_used: number | null;
  time_left_seconds: number | null;
};

type AutoStatus = { active: boolean; session: AutoSession | null };

function asAutoStatus(body: unknown): AutoStatus | null {
  if (body === null || typeof body !== "object") return null;
  const candidate = body as { active?: unknown; session?: unknown };
  if (typeof candidate.active !== "boolean") return null;
  if (candidate.active) {
    if (candidate.session === null || typeof candidate.session !== "object") return null;
    const session = candidate.session as Partial<AutoSession>;
    if (typeof session.tasks_run !== "number") return null;
    return { active: true, session: session as AutoSession };
  }
  return { active: false, session: null };
}

function asDispatchPaused(body: unknown): boolean | null {
  if (body === null || typeof body !== "object") return null;
  const raw = (body as { dispatch_paused?: unknown }).dispatch_paused;
  return typeof raw === "boolean" ? raw : null;
}

function asSandboxMode(body: unknown): boolean | null {
  if (body === null || typeof body !== "object") return null;
  const raw = (body as { sandbox_mode?: unknown }).sandbox_mode;
  return typeof raw === "boolean" ? raw : null;
}

/** `time_left_seconds` as h/m; null (a closed/unknown window) is the dash. */
export function timeLeftText(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return NOT_MEASURED;
  const minutes = Math.max(0, Math.floor(seconds / 60));
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return hours > 0 ? `${hours}h ${rest}m` : `${rest}m`;
}

function SourceError({ what, message }: { what: string; message: string }) {
  return (
    <span className="text-alert" title={message}>
      could not read {what}: {message}
    </span>
  );
}

export function OpsSwitches({ tick, onRetry }: { tick: number; onRetry: () => void }) {
  const [auto, setAuto] = useState<ReadState<AutoStatus>>({ state: "loading" });
  const [paused, setPaused] = useState<ReadState<boolean>>({ state: "loading" });
  const [sandbox, setSandbox] = useState<ReadState<boolean>>({ state: "loading" });

  useEffect(() => {
    let cancelled = false;

    api
      .get<unknown>("/auto-mode")
      .then((body) => {
        if (cancelled) return;
        const parsed = asAutoStatus(body);
        setAuto(
          parsed === null
            ? { state: "error", message: "the endpoint answered without a usable auto-mode status" }
            : { state: "ready", value: parsed },
        );
      })
      .catch((e: unknown) => {
        if (!cancelled) setAuto({ state: "error", message: errorText(e) });
      });

    api
      .get<unknown>("/config")
      .then((body) => {
        if (cancelled) return;
        const parsed = asDispatchPaused(body);
        setPaused(
          parsed === null
            ? { state: "error", message: "the config answer carried no readable dispatch_paused" }
            : { state: "ready", value: parsed },
        );
      })
      .catch((e: unknown) => {
        if (!cancelled) setPaused({ state: "error", message: errorText(e) });
      });

    api
      .get<unknown>("/ops/sandbox")
      .then((body) => {
        if (cancelled) return;
        const parsed = asSandboxMode(body);
        setSandbox(
          parsed === null
            ? { state: "error", message: "the sandbox answer carried no readable sandbox_mode" }
            : { state: "ready", value: parsed },
        );
      })
      .catch((e: unknown) => {
        if (!cancelled) setSandbox({ state: "error", message: errorText(e) });
      });

    return () => {
      cancelled = true;
    };
  }, [tick]);

  const anyError =
    auto.state === "error" || paused.state === "error" || sandbox.state === "error";

  return (
    <Card
      title="Kill switches & Auto Mode"
      kicker="OPS · LIVE READ"
      action={
        anyError ? (
          <Button size="sm" data-testid="ops-switches-retry" onClick={onRetry}>
            Retry
          </Button>
        ) : undefined
      }
    >
      <dl className="space-y-2" aria-busy={auto.state === "loading" || paused.state === "loading"}>
        <div>
          <dt className="ui-label text-ink-muted">AUTO MODE</dt>
          <dd className="text-sm text-ink" data-testid="ops-switch-auto-mode">
            {auto.state === "loading" ? (
              "reading…"
            ) : auto.state === "error" ? (
              <SourceError what="auto mode" message={auto.message} />
            ) : auto.value.active && auto.value.session !== null ? (
              <>
                ON — {timeLeftText(auto.value.session.time_left_seconds)} left ·{" "}
                {auto.value.session.tasks_run} tasks done ·{" "}
                {moneyText(auto.value.session.spend_used)} used
              </>
            ) : (
              "OFF — no window is open"
            )}
          </dd>
        </div>

        <div>
          <dt className="ui-label text-ink-muted">DISPATCH PAUSE (KILL SWITCH)</dt>
          <dd className="text-sm text-ink" data-testid="ops-switch-dispatch-pause">
            {paused.state === "loading" ? (
              "reading…"
            ) : paused.state === "error" ? (
              <SourceError what="the dispatch pause" message={paused.message} />
            ) : paused.value ? (
              "PAUSED — the worker refuses dispatch (company_config.dispatch_paused=true); resume in Settings."
            ) : (
              "RUNNING — dispatch is not paused."
            )}
          </dd>
        </div>

        <div>
          <dt className="ui-label text-ink-muted">SANDBOX</dt>
          <dd className="text-sm text-ink" data-testid="ops-switch-sandbox">
            {sandbox.state === "loading" ? (
              "reading…"
            ) : sandbox.state === "error" ? (
              <SourceError what="the sandbox flag" message={sandbox.message} />
            ) : sandbox.value ? (
              "ON — external actions are drafted, never sent"
            ) : (
              "OFF — external actions may be sent"
            )}
          </dd>
        </div>
      </dl>

      <p className="mt-2 text-xs text-ink-muted">
        Read from <code>GET /auto-mode</code>, <code>GET /config</code> (dispatch_paused) and{" "}
        <code>GET /ops/sandbox</code>. The <code>POLSIA_KILL_SWITCH</code> environment grade
        (throttle/freeze) is enforced inside the worker, but no API route exposes its value — so no
        grade is shown here rather than guessed.
      </p>
    </Card>
  );
}
