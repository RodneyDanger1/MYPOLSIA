"use client";

import { RadioList, Switch } from "@/components/ui";

/**
 * Settings → Scheduler: the NIGHT TASK controls (wave V7 / V7-T12).
 *
 * The backend surface landed in wave U5 (`company_config.night_task_*`; the hourly beat tick
 * `celery_app.tasks.night_task.run_night_task` self-gates to the company-local hour): this
 * section is the operator's handle on it. All state lives in the page — every control here is a
 * controlled input feeding the page's ONE PUT; nothing is saved from this file.
 *
 * Copy honesty (re-verify against the backend before editing any string here):
 *  - OFF writes nothing (no activity row, no task touch, no broker contact) — the tick's own
 *    documented contract;
 *  - an unset hour is NOT a stored default: the column stays NULL and the tick resolves the
 *    documented 23:00 at read time (never written back);
 *  - the limit is how many queue-top rows ONE run hands to the same dispatcher — a bounded
 *    drain + receipt, never a backlog-clearer.
 */

/** The two night-task numbers the page holds: `null` = no stored value (or not yet loaded). */
export type NightDraft = { hour: number | null; limit: number | null };

const HOUR_OPTIONS = Array.from({ length: 24 }, (_, hour) => ({
  value: String(hour),
  label: `${String(hour).padStart(2, "0")}:00`,
}));

const LIMIT_OPTIONS = [1, 2, 3, 4, 5].map((count) => ({
  value: String(count),
  label: String(count),
  note: count === 1 ? "task" : "tasks",
}));

export function NightTaskSection({
  enabled,
  hour,
  limit,
  onEnabledChange,
  onHourChange,
  onLimitChange,
}: {
  enabled: boolean;
  hour: number | null;
  limit: number | null;
  onEnabledChange: (next: boolean) => void;
  onHourChange: (next: number) => void;
  onLimitChange: (next: number) => void;
}) {
  return (
    <div className="space-y-4 border-t border-line pt-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="ui-label text-ink">Night task</p>
          <p className="ui-label mt-1 text-ink-muted">
            Hands the top of the queue to the dispatcher once per night at the hour below. Off
            writes nothing.
          </p>
        </div>
        <Switch
          checked={enabled}
          onChange={onEnabledChange}
          label="Night task enabled"
          id="settings-night-task-enabled"
        />
      </div>

      <div>
        <p className="ui-label text-ink-muted">Night task hour (company-local)</p>
        <div className="mt-1">
          <RadioList
            options={HOUR_OPTIONS}
            value={hour === null ? null : String(hour)}
            onChange={(next) => onHourChange(Number(next))}
            name="Night task hour"
          />
        </div>
        <p className="ui-label mt-1 text-ink-muted">
          0–23 in the company-local timezone. An unset hour is stored as no value — the server
          resolves its documented 23:00 at run time.
        </p>
      </div>

      <div>
        <p className="ui-label text-ink-muted">Night task limit</p>
        <div className="mt-1">
          <RadioList
            options={LIMIT_OPTIONS}
            value={limit === null ? null : String(limit)}
            onChange={(next) => onLimitChange(Number(next))}
            name="Night task limit"
          />
        </div>
        <p className="ui-label mt-1 text-ink-muted">
          How many queued tasks one run hands to the dispatcher (1–5).
        </p>
      </div>
    </div>
  );
}
