"use client";

import { useState } from "react";

import { Button, Card, RadioList } from "@/components/ui";
import { NOT_MEASURED, moneyText } from "@/components/dashboard/format";
import { PanelError, PanelSkeleton } from "@/components/dashboard/states";

import { useAutoMode, type AutoModeSession } from "./useAutoMode";

/**
 * Dashboard → left rail: the AUTO MODE panel (wave V7 / V7-T14), mounted directly under
 * `PolsiaStatusCard`.
 *
 * The wire surface is `GET/POST/DELETE /api/v1/auto-mode` (`auto_mode.py` :56-127) — consumed
 * verbatim: POST `{duration_hours}` → 201 `{started, session}`; GET → `{active, session|null}`;
 * DELETE → `{stopped, session}`. Everything rendered comes off those answers: `spend_used` is
 * `null` when nothing was measured and renders the app's dash — never a fabricated $0; the
 * closed session's `stop_reason` is mapped to the operator's words (an unknown word renders as
 * itself, never disguised).
 */

const DURATIONS: { value: string; label: string }[] = [
  { value: "1", label: "1h" },
  { value: "6", label: "6h" },
  { value: "24", label: "24h" },
  { value: "72", label: "3d" },
  { value: "120", label: "5d" },
];

/** The frozen `stop_reason` vocabulary (`auto_mode_service.STOP_REASONS`) → operator words. */
const STOP_REASON_TEXT: Record<string, string> = {
  duration_end: "Stopped — the window's duration ended.",
  day_cap: "Stopped — the day cap was reached.",
  user_stop: "Stopped by you.",
  error: "Stopped after a run error.",
};

export function stopReasonText(reason: string | null): string {
  if (!reason) return NOT_MEASURED;
  return STOP_REASON_TEXT[reason] ?? `Stopped — ${reason.replace(/_/g, " ")}`;
}

/** `time_left_seconds` as h/m ("1h 30m"); null (a closed/unknown window) is the dash. */
export function timeLeftText(seconds: number | null): string {
  if (seconds === null) return NOT_MEASURED;
  const minutes = Math.max(0, Math.floor(seconds / 60));
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return hours > 0 ? `${hours}h ${rest}m` : `${rest}m`;
}

export function AutoModePanel() {
  const { status, loading, loadError, busy, actionError, lastStopped, start, stop } = useAutoMode();
  const [duration, setDuration] = useState<string | null>(null);

  return (
    <Card title="Auto Mode">
      {loading ? <PanelSkeleton rows={3} /> : null}

      {!loading && loadError !== null ? <PanelError label="auto mode" message={loadError} /> : null}

      {!loading && loadError === null && status !== null ? (
        status.active && status.session !== null ? (
          <OnBody session={status.session} busy={busy} onStop={() => void stop()} />
        ) : (
          <OffBody
            duration={duration}
            busy={busy}
            onDuration={setDuration}
            onStart={() => {
              if (duration === null) return;
              void start(Number(duration));
            }}
          />
        )
      ) : null}

      {lastStopped !== null ? (
        <p className="ui-label mt-2 text-ink-muted">{stopReasonText(lastStopped.stop_reason)}</p>
      ) : null}

      {actionError !== null ? (
        <p role="alert" className="mt-1 text-sm text-alert">
          {actionError}
        </p>
      ) : null}
    </Card>
  );
}

function OffBody({
  duration,
  busy,
  onDuration,
  onStart,
}: {
  duration: string | null;
  busy: boolean;
  onDuration: (next: string) => void;
  onStart: () => void;
}) {
  return (
    <div className="space-y-3">
      <p className="text-sm text-ink-muted">
        Opens a bounded window (1 hour to 5 days). One at a time — it closes when the duration
        ends, the day cap hits, or you stop it.
      </p>
      <RadioList
        options={DURATIONS}
        value={duration}
        onChange={onDuration}
        name="Auto mode duration"
      />
      <Button
        variant="glossy"
        className="w-full"
        onClick={onStart}
        disabled={duration === null || busy}
      >
        {busy ? "Starting…" : "Start Auto Mode"}
      </Button>
    </div>
  );
}

function OnBody({ session, busy, onStop }: { session: AutoModeSession; busy: boolean; onStop: () => void }) {
  return (
    <div className="space-y-2">
      <p className="flex items-center gap-2">
        <span aria-hidden="true" className="inline-block h-2 w-2 rounded-full bg-accent" />
        <span className="ui-label font-bold text-ink">Auto Mode on</span>
      </p>
      <p className="text-sm text-ink">
        <span>{timeLeftText(session.time_left_seconds)} left</span>
        {" · "}
        <span>{`${session.tasks_run} tasks done`}</span>
        {" · "}
        <span>{`${moneyText(session.spend_used)} used`}</span>
      </p>
      <Button variant="glossy" className="w-full" onClick={onStop} disabled={busy}>
        {busy ? "Stopping…" : "Stop Auto Mode"}
      </Button>
    </div>
  );
}
