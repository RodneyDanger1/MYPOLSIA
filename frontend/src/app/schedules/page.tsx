"use client";

import { useState } from "react";

import { NOT_MEASURED, absoluteStamp } from "@/components/dashboard/format";
import {
  ScheduleCreateForm,
  ScheduleEditForm,
  WEEKDAY_NAMES,
} from "@/components/schedules/ScheduleForms";
import { errorText, useScheduleList } from "@/components/schedules/useSchedules";
import { Button, Card, Switch } from "@/components/ui";
import { api } from "@/lib/api";
import type { Schedule, ScheduleCreate, ScheduleUpdate } from "@/lib/api";

/**
 * Schedules — the CRUD surface for the owner's recurring task prompts (wiring audit F1:
 * schedules ran via `celery_app/tasks/cadence.py` with no UI at all).
 *
 * Every control calls a REAL route and nothing else (`backend/app/api/v1/schedules.py`):
 *   GET    /schedules           -> {items, total}                        (:39)
 *   POST   /schedules           -> 201 <schedule>                        (:50)
 *   PATCH  /schedules/{id}      -> <schedule>   (pause = {"enabled": false})(:70)
 *   DELETE /schedules/{id}      -> the REMOVED row                       (:84)
 *
 * Rules rendered, never re-invented:
 *   - `enabled=false` IS the pause (the service's rule): the switch sends exactly that PATCH, and
 *     the cleared `next_run_at` shown afterwards is the SERVER's answer, not a client guess;
 *   - the edit form sends only the fields the operator changed (`exclude_unset` semantics), so a
 *     name-only edit cannot accidentally re-arm or clear anything else;
 *   - a refused action (E_VALIDATION 422 / E_NOT_FOUND 404) renders `ApiError.display` — code +
 *     message + how_to_fix — and the row keeps the server's last state;
 *   - `next_run_at`/`last_run_at`/`weekday` nulls are the em dash; a measured value is rendered
 *     through the house stamp. No instant is ever fabricated;
 *   - an action's answer (every route returns the `ScheduleResponse`) replaces the row it names —
 *     no follow-up read is invented for data the action already returned;
 *   - loading / failed-read / empty are three disjoint states, each stated in its own words.
 */

/** `hour` is a company-local wall-clock hour 0-23 (`schedule.py` :27); render it as HH:00. */
function hourText(hour: number): string {
  return Number.isFinite(hour) ? `${String(hour).padStart(2, "0")}:00` : NOT_MEASURED;
}

/** `weekday` 0-6 (Monday=0) applies to a weekly cadence only; null is the em dash. */
function weekdayText(weekday: number | null): string {
  if (weekday === null) return NOT_MEASURED;
  return WEEKDAY_NAMES[weekday] ?? String(weekday);
}

/** A nullable server instant: the house stamp when present, the em dash when null. */
function stampText(value: string | null): string {
  return value !== null ? absoluteStamp(value) : NOT_MEASURED;
}

export default function SchedulesPage() {
  const { rows, error, loading, setRows } = useScheduleList("/schedules");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [createdId, setCreatedId] = useState<number | null>(null);
  const [editId, setEditId] = useState<number | null>(null);
  const [deleteId, setDeleteId] = useState<number | null>(null);

  /** Merge one server-answered schedule: replace in place when the list holds it (PATCH), append
   *  when it is new (POST — the list is id-ascending, so new rows belong at the end). */
  function mergeSchedule(updated: Schedule) {
    setRows((current) => {
      if (current === null) return current;
      const exists = current.some((row) => row.id === updated.id);
      return exists
        ? current.map((row) => (row.id === updated.id ? updated : row))
        : [...current, updated];
    });
  }

  async function create(body: ScheduleCreate) {
    setBusy("create");
    setActionError(null);
    try {
      const created = await api.post<Schedule>("/schedules", body);
      setCreatedId(created.id);
      // V28/L1 — a POST answers ONE row, never the table. When the list read has not landed yet
      // (`rows === null`) the created row must not become "All schedules": `mergeSchedule` returns
      // the unread `null` untouched, exactly like the prospects page. The read that is in flight
      // (or the next one) paints the list; until then the page keeps saying it is loading.
      mergeSchedule(created);
    } catch (e: unknown) {
      setActionError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function toggleEnabled(row: Schedule, next: boolean) {
    setBusy(`toggle-${row.id}`);
    setActionError(null);
    try {
      const updated = await api.patch<Schedule>(`/schedules/${row.id}`, { enabled: next });
      mergeSchedule(updated);
    } catch (e: unknown) {
      setActionError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function save(row: Schedule, changes: ScheduleUpdate) {
    setBusy(`save-${row.id}`);
    setActionError(null);
    try {
      const updated = await api.patch<Schedule>(`/schedules/${row.id}`, changes);
      mergeSchedule(updated);
      setEditId(null);
    } catch (e: unknown) {
      setActionError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function remove(row: Schedule) {
    setBusy(`delete-${row.id}`);
    setActionError(null);
    try {
      // The route answers the removed representation; the list drops the row it named.
      await api.delete<Schedule>(`/schedules/${row.id}`);
      setRows((current) => (current === null ? current : current.filter((r) => r.id !== row.id)));
      setDeleteId(null);
    } catch (e: unknown) {
      setActionError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="border-b border-line pb-1">
        <h1 className="text-3xl font-bold text-ink">Schedules</h1>
        <p className="ui-label text-ink-muted">
          Recurring task prompts — the cadence tick creates one task per due instant, in the
          company&apos;s local hour. Pausing clears the next run; the server recomputes it on resume.
        </p>
      </div>

      {actionError !== null ? (
        <p role="alert" className="text-sm text-alert" data-testid="schedules-action-error">
          {actionError}
        </p>
      ) : null}

      {loading ? (
        <p className="ui-label text-ink-muted" aria-busy="true" data-testid="schedules-loading">
          Loading…
        </p>
      ) : null}

      {!loading && error !== null ? (
        <p role="alert" className="text-sm text-alert" data-testid="schedules-read-error">
          Could not load the schedules: {error}
        </p>
      ) : null}

      {!loading && error === null && rows !== null && rows.length === 0 ? (
        <p className="text-sm text-ink-muted" data-testid="schedules-empty">
          No schedules yet — create one below. A schedule runs its prompt on the cadence you pick,
          in the company&apos;s local hour.
        </p>
      ) : null}

      {!loading && error === null && rows !== null && rows.length > 0 ? (
        <Card title="All schedules" data-testid="schedules-list">
          <div className="divide-y divide-line">
            {rows.map((row) => (
              <div key={row.id} data-testid={`schedule-row-${row.id}`} className="space-y-1 py-2">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-bold text-ink" data-testid="schedule-name">
                    {row.name}
                  </span>
                  <Switch
                    checked={row.enabled}
                    onChange={(next) => void toggleEnabled(row, next)}
                    label={`${row.name} enabled`}
                    disabled={busy !== null}
                  />
                  <span className="ml-auto flex gap-2">
                    <Button
                      size="sm"
                      onClick={() => {
                        setEditId(row.id);
                        setDeleteId(null);
                        setActionError(null);
                      }}
                      disabled={busy !== null}
                    >
                      Edit
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => {
                        setDeleteId(row.id);
                        setEditId(null);
                        setActionError(null);
                      }}
                      disabled={busy !== null}
                    >
                      Delete
                    </Button>
                  </span>
                </div>
                <p className="text-sm text-ink-muted" data-testid="schedule-prompt">
                  {row.prompt}
                </p>
                <div className="flex flex-wrap gap-3 ui-label text-ink-muted">
                  <span data-testid="schedule-cadence">{row.cadence}</span>
                  <span data-testid="schedule-hour">{hourText(row.hour)}</span>
                  <span data-testid="schedule-weekday">{weekdayText(row.weekday)}</span>
                  <span>
                    Next{" "}
                    <span data-testid="schedule-next-run">{stampText(row.next_run_at)}</span>
                  </span>
                  <span>
                    Last{" "}
                    <span data-testid="schedule-last-run">{stampText(row.last_run_at)}</span>
                  </span>
                </div>

                {deleteId === row.id ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm text-ink-muted">
                      Delete “{row.name}”? The cadence tick stops creating tasks from it.
                    </span>
                    <Button
                      size="sm"
                      data-testid={`schedule-delete-confirm-${row.id}`}
                      onClick={() => void remove(row)}
                      disabled={busy !== null}
                    >
                      Confirm delete
                    </Button>
                    <Button size="sm" onClick={() => setDeleteId(null)} disabled={busy !== null}>
                      Cancel
                    </Button>
                  </div>
                ) : null}

                {editId === row.id ? (
                  <ScheduleEditForm
                    row={row}
                    busy={busy !== null}
                    onSave={(changes) => void save(row, changes)}
                    onCancel={() => setEditId(null)}
                  />
                ) : null}
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      <ScheduleCreateForm busy={busy === "create"} lastCreatedId={createdId} onSubmit={(body) => void create(body)} />
    </div>
  );
}
