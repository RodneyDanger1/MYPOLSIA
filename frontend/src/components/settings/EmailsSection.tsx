"use client";

import { Switch } from "@/components/ui";

/**
 * Settings → Emails: the four owner-notification toggles (wave V7 / V7-T13).
 *
 * These bind the frozen `notify_on_*` columns the U5 notification funnel reads
 * (`notification_service._OUTCOME_ROUTES` routes completed / failed / awaiting_approval / requeued
 * to one email each, when the toggle is on AND an address is configured). The requeued toggle is a
 * REAL route: `_OUTCOME_ROUTES["requeued"]` reads `notify_on_run_requeued`, and a retried attempt
 * is reported as `requeued` by `_notification_outcome` — the helper says exactly that, and that the
 * requeued email is its own toggle (a requeued attempt never also fires the failed email).
 */

export type NotifyKey =
  | "notify_on_completed"
  | "notify_on_failed"
  | "notify_on_awaiting_approval"
  | "notify_on_run_requeued";

export const NOTIFY_ROWS: readonly { key: NotifyKey; label: string; helper: string }[] = [
  {
    key: "notify_on_completed",
    label: "Notify on completed tasks",
    helper: "One email per completed task, sent to the address above.",
  },
  {
    key: "notify_on_failed",
    label: "Notify on failed tasks",
    helper: "One email per failed task, with the error the run recorded.",
  },
  {
    key: "notify_on_awaiting_approval",
    label: "Notify on awaiting approval",
    helper: "One email when a task parks for your approval.",
  },
  {
    key: "notify_on_run_requeued",
    label: "Notify on requeued runs",
    helper:
      "One email when a failed attempt is put back in the queue. Its own toggle — a requeued attempt never also fires the “failed” email.",
  },
];

export function EmailsSection({
  values,
  onChange,
}: {
  values: Record<NotifyKey, boolean>;
  onChange: (key: NotifyKey, next: boolean) => void;
}) {
  return (
    <div className="space-y-4 border-t border-line pt-4">
      {NOTIFY_ROWS.map((row) => (
        <div key={row.key} className="flex items-start justify-between gap-3">
          <div>
            <p className="ui-label text-ink">{row.label}</p>
            <p className="ui-label mt-1 text-ink-muted">{row.helper}</p>
          </div>
          <Switch
            checked={values[row.key]}
            onChange={(next) => onChange(row.key, next)}
            label={row.label}
            id={`settings-${row.key}`}
          />
        </div>
      ))}
    </div>
  );
}
