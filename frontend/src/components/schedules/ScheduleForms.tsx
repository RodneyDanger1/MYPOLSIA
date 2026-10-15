"use client";

import { useEffect, useState } from "react";

import { Button, RadioList, Switch } from "@/components/ui";
import type { RadioOption } from "@/components/ui";
import type { Schedule, ScheduleCadence, ScheduleCreate, ScheduleUpdate } from "@/lib/api";

/**
 * The create/edit forms of the /schedules page, following the AutoModePanel/useAutoMode
 * interaction pattern: form state is local, every submit calls the REAL route, and the row the
 * server answers with is the only thing the page trusts afterwards.
 *
 * Wire truth (`backend/app/api/v1/schedules.py` :50/:70 over `app/schemas/schedule.py` :21-40):
 *   POST   /schedules        ScheduleCreate — cadence one of once|daily|weekdays|weekly,
 *                            hour 0-23 company-local, weekday 0-6 (Monday=0) for weekly,
 *                            `enabled` defaults FALSE (a schedule is created paused unless on).
 *   PATCH  /schedules/{id}   ScheduleUpdate — only the PROVIDED keys are applied
 *                            (`exclude_unset`), so the edit form sends exactly what changed.
 *
 * The Hour box is the one value JSON could launder (S2-F3, wave v14): a blank box used to ship
 * `Number.NaN`, which `JSON.stringify` serializes as `null`, and an out-of-range hour in the edit
 * form was silently DROPPED from the PATCH (with no other change, the form even closed as "nothing
 * changed"). Both forms now refuse LOCALLY, in the same words, before anything is sent — a refusal
 * the operator can read, instead of a value quietly rewritten. The refusal is stated text, not a
 * second `role="alert"` region: this page owns exactly one live alert (`schedules-action-error`).
 * An in-range hour still rides the body and the server stays the last word (its named refusal is
 * rendered by the page); only the value that can never be expressed on the wire is stopped here.
 */

/** The frozen cadence value set, in the schema's definition order. */
export const CADENCE_OPTIONS: RadioOption<ScheduleCadence>[] = [
  { value: "once", label: "Once" },
  { value: "daily", label: "Daily" },
  { value: "weekdays", label: "Weekdays" },
  { value: "weekly", label: "Weekly" },
];

/** Python's `date.weekday()` vocabulary (Monday=0) — the server's numbering, rendered verbatim. */
export const WEEKDAY_NAMES = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
] as const;

export function isCadence(value: string): value is ScheduleCadence {
  return value === "once" || value === "daily" || value === "weekdays" || value === "weekly";
}

/** The one stated local refusal both forms use for a bad Hour box (S2-F3) — same words, same
 *  meaning: the value is outside what the wire can carry (a whole number 0-23, company-local). */
const HOUR_REFUSAL = "Hour must be a whole number 0-23 (company-local)";

export type ScheduleDraft = {
  name: string;
  prompt: string;
  cadence: ScheduleCadence;
  hour: string;
  weekday: string;
};

/** The shared field set of both forms — one definition, so create and edit cannot drift. */
export function ScheduleFields({
  idPrefix,
  draft,
  onChange,
}: {
  idPrefix: string;
  draft: ScheduleDraft;
  onChange: (patch: Partial<ScheduleDraft>) => void;
}) {
  return (
    <div className="space-y-3">
      <div>
        <label className="ui-label text-ink-muted" htmlFor={`${idPrefix}-name`}>
          Name
        </label>
        <input
          id={`${idPrefix}-name`}
          className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
          value={draft.name}
          onChange={(e) => onChange({ name: e.target.value })}
        />
      </div>
      <div>
        <label className="ui-label text-ink-muted" htmlFor={`${idPrefix}-prompt`}>
          Prompt
        </label>
        <textarea
          id={`${idPrefix}-prompt`}
          rows={2}
          className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
          value={draft.prompt}
          onChange={(e) => onChange({ prompt: e.target.value })}
        />
      </div>
      <div className="flex flex-wrap items-start gap-4">
        <div>
          <span className="ui-label text-ink-muted">Cadence</span>
          <RadioList
            options={CADENCE_OPTIONS}
            value={draft.cadence}
            onChange={(value) => onChange({ cadence: value })}
            name="Cadence"
          />
        </div>
        <div>
          <label className="ui-label text-ink-muted" htmlFor={`${idPrefix}-hour`}>
            Hour
          </label>
          <input
            id={`${idPrefix}-hour`}
            type="number"
            min={0}
            max={23}
            className="mt-1 w-20 border border-line bg-surface px-2 py-1 text-sm text-ink"
            value={draft.hour}
            onChange={(e) => onChange({ hour: e.target.value })}
          />
          <p className="mt-1 text-xs text-ink-muted">Company-local hour (0-23).</p>
        </div>
        {draft.cadence === "weekly" ? (
          <div>
            <label className="ui-label text-ink-muted" htmlFor={`${idPrefix}-weekday`}>
              Weekday
            </label>
            <select
              id={`${idPrefix}-weekday`}
              className="mt-1 border border-line bg-surface px-2 py-1 text-sm text-ink"
              value={draft.weekday}
              onChange={(e) => onChange({ weekday: e.target.value })}
            >
              {WEEKDAY_NAMES.map((label, index) => (
                <option key={label} value={String(index)}>
                  {label}
                </option>
              ))}
            </select>
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** POST /schedules — the body is the ScheduleCreate wire shape, field for field. */
export function ScheduleCreateForm({
  busy,
  lastCreatedId,
  onSubmit,
}: {
  busy: boolean;
  /** The id of the last created row (server's answer); a change clears the text fields. */
  lastCreatedId: number | null;
  onSubmit: (body: ScheduleCreate) => void;
}) {
  const [draft, setDraft] = useState<ScheduleDraft>({
    name: "",
    prompt: "",
    cadence: "daily",
    hour: "9",
    weekday: "0",
  });
  const [enabled, setEnabled] = useState(false);
  const [hourRefusal, setHourRefusal] = useState<string | null>(null);

  useEffect(() => {
    if (lastCreatedId !== null) {
      setDraft((current) => ({ ...current, name: "", prompt: "" }));
      setHourRefusal(null);
    }
  }, [lastCreatedId]);

  function submit() {
    const trimmedHour = draft.hour.trim();
    const parsedHour = trimmedHour === "" ? Number.NaN : Number(trimmedHour);
    if (Number.isNaN(parsedHour)) {
      // A blank (or unreadable) box is NOT a value: state it and send nothing. NOTE the deliberate
      // asymmetry with an out-of-range NUMBER (e.g. 25): that one is expressible, it rides the body,
      // and the server's named refusal comes back and is rendered by the page.
      setHourRefusal(HOUR_REFUSAL);
      return;
    }
    setHourRefusal(null);
    const body: ScheduleCreate = {
      name: draft.name.trim(),
      prompt: draft.prompt.trim(),
      cadence: draft.cadence,
      hour: parsedHour,
      enabled,
    };
    if (draft.cadence === "weekly") body.weekday = Number(draft.weekday);
    onSubmit(body);
  }

  return (
    <section className="card-surface" data-testid="schedule-create">
      <header className="flex items-end gap-2 border-b border-line px-3 py-2">
        <div className="mr-auto">
          <h2 className="text-base font-bold leading-tight text-ink">New schedule</h2>
        </div>
      </header>
      <div className="px-3 py-3">
        <ScheduleFields
          idPrefix="schedule-create"
          draft={draft}
          onChange={(patch) => {
            setHourRefusal(null);
            setDraft((current) => ({ ...current, ...patch }));
          }}
        />
        <div className="mt-3 flex items-center gap-3">
          <Switch checked={enabled} onChange={setEnabled} label="Create enabled" />
          <span className="text-xs text-ink-muted">
            Created paused unless switched on (the column default, `schedule.py` :22-29). An
            enabled schedule gets its first next-run computed by the server.
          </span>
        </div>
        <div className="mt-3">
          <Button variant="glossy" onClick={submit} disabled={busy}>
            {busy ? "Creating…" : "Create schedule"}
          </Button>
        </div>
        {hourRefusal !== null ? (
          <p className="mt-2 text-sm text-alert" data-testid="schedule-create-refusal">
            {hourRefusal} — nothing was sent.
          </p>
        ) : null}
      </div>
    </section>
  );
}

/** PATCH /schedules/{id} — sends ONLY the fields the operator changed (`exclude_unset`). */
export function ScheduleEditForm({
  row,
  busy,
  onSave,
  onCancel,
}: {
  row: Schedule;
  busy: boolean;
  onSave: (changes: ScheduleUpdate) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<ScheduleDraft>(() => ({
    name: row.name,
    prompt: row.prompt,
    cadence: isCadence(row.cadence) ? row.cadence : "daily",
    hour: String(row.hour),
    weekday: row.weekday === null ? "0" : String(row.weekday),
  }));

  const [hourRefusal, setHourRefusal] = useState<string | null>(null);

  function save() {
    const changes: ScheduleUpdate = {};
    if (draft.name !== row.name) changes.name = draft.name;
    if (draft.prompt !== row.prompt) changes.prompt = draft.prompt;
    if (draft.cadence !== row.cadence) changes.cadence = draft.cadence;

    const trimmedHour = draft.hour.trim();
    const parsedHour = trimmedHour === "" ? Number.NaN : Number(trimmedHour);
    if (draft.hour !== String(row.hour)) {
      // The operator touched the Hour box: if its content cannot be a wire hour (blank, 9.5, 25…),
      // refuse it BY NAME here. The old shape dropped it from the PATCH silently — and with no
      // other change closed the form as "nothing changed", a claim that was simply false (S2-F3).
      if (!Number.isInteger(parsedHour) || parsedHour < 0 || parsedHour > 23) {
        setHourRefusal(HOUR_REFUSAL);
        return;
      }
      changes.hour = parsedHour;
    }
    if (draft.cadence === "weekly") {
      const parsedWeekday = Number(draft.weekday);
      if (parsedWeekday !== (row.weekday ?? -1)) changes.weekday = parsedWeekday;
    }
    if (Object.keys(changes).length === 0) {
      // Nothing changed: there is no PATCH to make. Close rather than send an empty write.
      onCancel();
      return;
    }
    setHourRefusal(null);
    onSave(changes);
  }

  return (
    <div
      data-testid={`schedule-edit-${row.id}`}
      className="mt-2 border border-line bg-surface px-2 py-2"
    >
      <ScheduleFields
        idPrefix={`schedule-edit-${row.id}`}
        draft={draft}
        onChange={(patch) => {
          setHourRefusal(null);
          setDraft((current) => ({ ...current, ...patch }));
        }}
      />
      <div className="mt-3 flex gap-2">
        <Button size="sm" onClick={save} disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </Button>
        <Button size="sm" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
      {hourRefusal !== null ? (
        <p className="mt-2 text-sm text-alert" data-testid={`schedule-edit-refusal-${row.id}`}>
          {hourRefusal} — nothing was saved.
        </p>
      ) : null}
    </div>
  );
}
