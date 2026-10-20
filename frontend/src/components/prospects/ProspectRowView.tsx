"use client";

import { absoluteStamp, relativeTime } from "@/components/dashboard/format";
import { ProspectStatusChip, prospectName } from "@/components/outreach/ProspectRow";
import { Button } from "@/components/ui";

import { ProspectEditForm } from "./ProspectForms";
import { PROSPECT_STATUSES } from "./types";
import type { Prospect, ProspectUpdate } from "./types";

/**
 * One row of the `/prospects` list, with its inline controls.
 *
 * The visual grammar is the outreach list's (`components/outreach/ProspectRow.tsx`): the email in
 * mono, name · company muted, the status chip on the right, a relative age. The row ADDS the
 * owner's controls the read-only outreach page deliberately refuses:
 *
 *   * a status `<select>` that PATCHes `{status}` on change — the chip then paints the SERVER's
 *     answer (this component holds no optimistic copy of the row);
 *   * an Edit form for every other field (email / names / company / title / source), sending only
 *     what changed;
 *   * a TWO-STEP delete: the first press only reveals the confirm block, and the confirm button is
 *     the only control that calls `DELETE`.
 *
 * A null optional value renders the app-wide em dash (never an empty cell); an unknown stored
 * status word is offered as its own option in the select, so opening the control cannot silently
 * rewrite a value this UI does not know.
 */
export function ProspectRowView({
  prospect,
  busy,
  editing,
  confirming,
  onStartEdit,
  onCancelEdit,
  onSave,
  onStatus,
  onStartDelete,
  onCancelDelete,
  onConfirmDelete,
}: {
  prospect: Prospect;
  busy: boolean;
  editing: boolean;
  confirming: boolean;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onSave: (changes: ProspectUpdate) => void;
  onStatus: (next: string) => void;
  onStartDelete: () => void;
  onCancelDelete: () => void;
  onConfirmDelete: () => void;
}) {
  const statusOptions = [...PROSPECT_STATUSES];
  if (prospect.status !== null && !statusOptions.includes(prospect.status)) {
    statusOptions.unshift(prospect.status);
  }

  return (
    <li
      data-testid={`prospect-row-${prospect.id}`}
      className="space-y-1 border-b border-line px-3 py-2.5 last:border-b-0"
    >
      <div className="flex flex-wrap items-center gap-3">
        <p
          data-testid="prospect-email"
          className="min-w-0 truncate font-mono text-sm text-ink"
          title={prospect.email}
        >
          {prospect.email}
        </p>
        <ProspectStatusChip status={prospect.status} data-testid="prospect-status" />
        <select
          id={`prospect-status-${prospect.id}`}
          data-testid="prospect-status-select"
          aria-label={`Status for ${prospect.email}`}
          className="border border-line bg-surface px-1 py-0.5 text-xs text-ink"
          value={prospect.status ?? ""}
          onChange={(e) => {
            if (e.target.value !== "") onStatus(e.target.value);
          }}
          disabled={busy}
        >
          {prospect.status === null ? (
            <option value="">—</option>
          ) : null}
          {statusOptions.map((status) => (
            <option key={status} value={status}>
              {status}
            </option>
          ))}
        </select>
        <span className="ml-auto flex gap-2">
          <Button
            size="sm"
            onClick={onStartEdit}
            disabled={busy}
          >
            Edit
          </Button>
          <Button
            size="sm"
            onClick={onStartDelete}
            disabled={busy}
          >
            Delete
          </Button>
        </span>
      </div>

      <p className="truncate text-xs text-ink-muted" title={prospect.title ?? undefined}>
        <span data-testid="prospect-name">{prospectName(prospect)}</span>
        {" · "}
        <span data-testid="prospect-company">{prospect.company ?? "—"}</span>
        {" · "}
        <span data-testid="prospect-source">{prospect.source ?? "—"}</span>
      </p>

      <div className="flex flex-wrap gap-3 ui-label text-ink-muted">
        <span data-testid="prospect-title">{prospect.title ?? "—"}</span>
        <span
          data-testid="prospect-age"
          title={prospect.created_at ? absoluteStamp(prospect.created_at) : undefined}
        >
          added {relativeTime(prospect.created_at)}
        </span>
      </div>

      {confirming ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm text-ink-muted">
            Delete {prospect.email}? This removes the row from the prospect table.
          </span>
          <Button
            size="sm"
            data-testid="prospect-delete-confirm"
            onClick={onConfirmDelete}
            disabled={busy}
          >
            Confirm delete
          </Button>
          <Button size="sm" onClick={onCancelDelete} disabled={busy}>
            Cancel
          </Button>
        </div>
      ) : null}

      {editing ? (
        <ProspectEditForm
          row={prospect}
          busy={busy}
          onSave={onSave}
          onCancel={onCancelEdit}
        />
      ) : null}
    </li>
  );
}
