"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { readErrorText } from "@/components/dashboard/states";
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
 *
 * V23/L1 adds the cycle-report opt-in (`company_config.cycle_report_email`, NOT NULL server
 * default FALSE). It is NOT one of the form's `notify_on_*` drafts: the frozen interface for this
 * control is its own GET /config read and its own PUT /config, awaited — the switch moves only
 * after the SERVER answers, and a refusal leaves it where it was with the refusal named inline.
 * The copy states what the backend actually does (`notification_service.notify_owner_cycle_report`
 * + `celery_app.tasks.daily_cycle`): one email per morning and evening report, sent after the
 * report committed, to `notification_email`; a skipped or failed send is recorded as an activity
 * row. No claimed email beyond those two cycle reports.
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

/** The cycle-report wire shape this control reads and writes (`CompanyConfigResponse` /
 *  `CompanyConfigUpdate`). Optional on purpose, like the page's bool fields: an absent key reads
 *  as the server's own NOT NULL default FALSE, never a crash. */
export type CycleReportConfig = { cycle_report_email?: boolean };

const CYCLE_REPORT_ID = "settings-cycle_report_email";
const CYCLE_REPORT_LABEL = "Email me each cycle report";
const CYCLE_REPORT_HELPER =
  "One email per morning and evening cycle report, sent to the Notification Email above after the report is saved. Needs that address set — a skipped or failed send is recorded in the activity feed.";

export function EmailsSection({
  values,
  onChange,
  notificationEmail,
}: {
  values: Record<NotifyKey, boolean>;
  onChange: (key: NotifyKey, next: boolean) => void;
  /** The SAVED recipient (`company_config.notification_email`, from the page's one GET /config).
   *  `null`/blank means no address is configured — the funnel the four switches feed is then a
   *  named no-op (see the note below), and the screen must say so instead of letting a switch read
   *  ON while no email can ever leave. */
  notificationEmail?: string | null;
}) {
  // `null` = not read yet. The control is not rendered until GET /config has answered, so the UI
  // never shows a fabricated OFF while the real value is still unknown.
  const [cycleReport, setCycleReport] = useState<boolean | null>(null);
  const [cycleReportLoading, setCycleReportLoading] = useState(true);
  const [cycleReportReadError, setCycleReportReadError] = useState<string | null>(null);
  const [cycleReportSaving, setCycleReportSaving] = useState(false);
  const [cycleReportSaveError, setCycleReportSaveError] = useState<string | null>(null);

  /** Wave 7 (email call-path honesty): is a notification ADDRESS saved? Blank/absent is "no
   *  recipient", which is the server's own reading of the column (`notification_service.
   *  _configured_recipient` strips it and answers None). */
  const addressConfigured =
    typeof notificationEmail === "string" && notificationEmail.trim() !== "";

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // The setting has no editor on the Settings form (the page's drafts cover only the fields it
  // owns), so this control reads its own value from the endpoint that serves it. Mounted with the
  // Emails tab, exactly like the tab's other controls; nothing polls.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await api.get<CycleReportConfig>("/config");
        if (cancelled) return;
        // The response model always serves the boolean; an absent key reads as the server's own
        // NOT NULL default FALSE (the same rule the page's `boolsFrom` keeps for older fixtures).
        setCycleReport(data?.cycle_report_email === true);
      } catch (e) {
        if (!cancelled) setCycleReportReadError(readErrorText(e));
      } finally {
        if (!cancelled) setCycleReportLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const toggleCycleReport = async (next: boolean) => {
    if (cycleReport === null || cycleReportSaving) return;
    setCycleReportSaving(true);
    setCycleReportSaveError(null);
    try {
      // No optimistic flip: the switch keeps showing the old value until this PUT answers. The
      // server validates and persists the key, and its own response is what the control reflects.
      const updated = await api.put<CycleReportConfig>("/config", { cycle_report_email: next });
      if (!mounted.current) return;
      if (typeof updated?.cycle_report_email === "boolean") {
        setCycleReport(updated.cycle_report_email);
      } else {
        // The write may have landed, but this response does not say what the row now holds.
        // Reflecting the click would be a guess; keep the previous state and say so by name.
        setCycleReportSaveError(
          "PUT /config answered without a boolean cycle_report_email — the switch keeps its previous state.",
        );
      }
    } catch (e) {
      if (!mounted.current) return;
      setCycleReportSaveError(readErrorText(e));
    } finally {
      if (mounted.current) setCycleReportSaving(false);
    }
  };

  return (
    <div className="space-y-4 border-t border-line pt-4">
      {/* Wave 7 — the state that used to be invisible. The four `notify_on_*` switches are stored
          flags the notification funnel reads AFTER it resolves a recipient
          (`notification_service.notify_owner` -> `_configured_recipient`): with the address blank
          the funnel returns before composing anything, so a switch can read ON while NO email can
          ever leave and NOTHING is recorded in the activity feed (unlike the cycle-report control
          below, whose skip IS recorded). Saying it here is the difference between "off by choice"
          and "on but dead". */}
      {!addressConfigured ? (
        <div
          data-testid="settings-emails-no-address"
          className="border border-line-strong bg-surface-sunk p-3"
        >
          <p className="ui-label text-ink">No notification address is saved</p>
          <p className="mt-1 text-sm text-ink">
            Notification Email above is blank, and the server stores blank as &ldquo;not
            configured&rdquo;. None of the switches below can send anything until an address is
            saved: the notification funnel stops before it composes a message, so no email is sent
            and no activity row records the skip.
          </p>
        </div>
      ) : null}

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

      {/* V23/L1 — immediate-save control (not a form draft): its own PUT, awaited. */}
      <div className="flex items-start justify-between gap-3 border-t border-line pt-4">
        <div>
          <p className="ui-label text-ink">{CYCLE_REPORT_LABEL}</p>
          <p id={`${CYCLE_REPORT_ID}-helper`} className="ui-label mt-1 text-ink-muted">
            {CYCLE_REPORT_HELPER}
          </p>
          {cycleReportSaveError ? (
            <p role="alert" className="mt-1 text-sm text-alert">
              Could not save the cycle-report setting: {cycleReportSaveError}
            </p>
          ) : null}
        </div>
        {cycleReportLoading ? (
          <p aria-busy="true" className="ui-label text-ink-muted">
            Reading…
          </p>
        ) : cycleReport === null ? null : (
          <Switch
            checked={cycleReport}
            onChange={toggleCycleReport}
            label={CYCLE_REPORT_LABEL}
            id={CYCLE_REPORT_ID}
            disabled={cycleReportSaving}
          />
        )}
      </div>

      {cycleReportReadError ? (
        <p role="alert" className="text-sm text-alert">
          Could not read the cycle-report setting: {cycleReportReadError}
        </p>
      ) : null}
    </div>
  );
}
