"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui";

import { PROSPECT_STATUSES } from "./types";
import type { Prospect, ProspectCreate, ProspectUpdate } from "./types";

/**
 * The add / edit forms of the `/prospects` page (`backend/app/api/v1/prospects.py`).
 *
 *   POST   /prospects        `ProspectCreate`  -> 201 the stored row
 *   PATCH  /prospects/{id}   `ProspectUpdate`  -> 200 the stored row (`exclude_unset`)
 *
 * Every submit calls a REAL route and the row the SERVER answers with is the only thing the page
 * trusts afterwards — the typed value is never painted as if it were stored. The email is
 * deliberately sent as typed (untrimmed): the server trims + lowercases before the unique insert,
 * and the test pins that the page shows the SERVER's normalised address, not the draft.
 *
 * Field labels are the operator's words; the inputs carry the exact wire field names nowhere —
 * `toBody` / `changesFrom` are the one place a draft becomes a request body.
 */

export type ProspectDraft = {
  email: string;
  first_name: string;
  last_name: string;
  company: string;
  title: string;
  source: string;
  status: string;
};

const EMPTY_DRAFT: ProspectDraft = {
  email: "",
  first_name: "",
  last_name: "",
  company: "",
  title: "",
  source: "",
  status: "",
};

/** One definition of the field set, so create and edit cannot drift. */
export function ProspectFields({
  idPrefix,
  draft,
  onChange,
  withStatus,
}: {
  idPrefix: string;
  draft: ProspectDraft;
  onChange: (patch: Partial<ProspectDraft>) => void;
  /** The create form offers the status; the edit form's status lives on the row's own select. */
  withStatus: boolean;
}) {
  return (
    <div className="space-y-3">
      <div>
        <label className="ui-label text-ink-muted" htmlFor={`${idPrefix}-email`}>
          Email
        </label>
        <input
          id={`${idPrefix}-email`}
          className="mt-1 w-full border border-line bg-surface px-2 py-1 font-mono text-sm text-ink"
          value={draft.email}
          onChange={(e) => onChange({ email: e.target.value })}
        />
      </div>
      <div className="flex flex-wrap gap-3">
        <div className="min-w-[10rem] flex-1">
          <label className="ui-label text-ink-muted" htmlFor={`${idPrefix}-first-name`}>
            First name
          </label>
          <input
            id={`${idPrefix}-first-name`}
            className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
            value={draft.first_name}
            onChange={(e) => onChange({ first_name: e.target.value })}
          />
        </div>
        <div className="min-w-[10rem] flex-1">
          <label className="ui-label text-ink-muted" htmlFor={`${idPrefix}-last-name`}>
            Last name
          </label>
          <input
            id={`${idPrefix}-last-name`}
            className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
            value={draft.last_name}
            onChange={(e) => onChange({ last_name: e.target.value })}
          />
        </div>
      </div>
      <div className="flex flex-wrap gap-3">
        <div className="min-w-[10rem] flex-1">
          <label className="ui-label text-ink-muted" htmlFor={`${idPrefix}-company`}>
            Company
          </label>
          <input
            id={`${idPrefix}-company`}
            className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
            value={draft.company}
            onChange={(e) => onChange({ company: e.target.value })}
          />
        </div>
        <div className="min-w-[10rem] flex-1">
          <label className="ui-label text-ink-muted" htmlFor={`${idPrefix}-title`}>
            Title
          </label>
          <input
            id={`${idPrefix}-title`}
            className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
            value={draft.title}
            onChange={(e) => onChange({ title: e.target.value })}
          />
        </div>
        <div className="min-w-[10rem] flex-1">
          <label className="ui-label text-ink-muted" htmlFor={`${idPrefix}-source`}>
            Source
          </label>
          <input
            id={`${idPrefix}-source`}
            className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
            value={draft.source}
            onChange={(e) => onChange({ source: e.target.value })}
          />
        </div>
      </div>
      {withStatus ? (
        <div>
          <label className="ui-label text-ink-muted" htmlFor={`${idPrefix}-status`}>
            Status
          </label>
          <select
            id={`${idPrefix}-status`}
            className="mt-1 border border-line bg-surface px-2 py-1 text-sm text-ink"
            value={draft.status}
            onChange={(e) => onChange({ status: e.target.value })}
          >
            <option value="">Default (new)</option>
            {PROSPECT_STATUSES.map((status) => (
              <option key={status} value={status}>
                {status}
              </option>
            ))}
          </select>
        </div>
      ) : null}
    </div>
  );
}

/** A blank optional box is NOT a value: it is omitted from the create body, so the service stores
 *  NULL instead of an empty string (the column's nullable contract). */
function createBody(draft: ProspectDraft): ProspectCreate {
  const body: ProspectCreate = { email: draft.email };
  if (draft.first_name.trim() !== "") body.first_name = draft.first_name.trim();
  if (draft.last_name.trim() !== "") body.last_name = draft.last_name.trim();
  if (draft.company.trim() !== "") body.company = draft.company.trim();
  if (draft.title.trim() !== "") body.title = draft.title.trim();
  if (draft.source.trim() !== "") body.source = draft.source.trim();
  if (draft.status !== "") body.status = draft.status;
  return body;
}

export function ProspectCreateForm({
  busy,
  lastCreatedId,
  onSubmit,
}: {
  busy: boolean;
  /** The id of the last created row (the server's answer); a change clears the draft. */
  lastCreatedId: number | null;
  onSubmit: (body: ProspectCreate) => void;
}) {
  const [draft, setDraft] = useState<ProspectDraft>(EMPTY_DRAFT);
  const [refusal, setRefusal] = useState<string | null>(null);

  useEffect(() => {
    if (lastCreatedId !== null) {
      setDraft(EMPTY_DRAFT);
      setRefusal(null);
    }
  }, [lastCreatedId]);

  function submit() {
    if (draft.email.trim() === "") {
      // An address is the row's identity and its unique key: state the refusal here, send nothing.
      setRefusal("Enter the prospect's email address — nothing was sent.");
      return;
    }
    setRefusal(null);
    onSubmit(createBody(draft));
  }

  return (
    <section className="card-surface" data-testid="prospect-create">
      <header className="flex items-end gap-2 border-b border-line px-3 py-2">
        <div className="mr-auto">
          <h2 className="text-base font-bold leading-tight text-ink">Add a prospect</h2>
        </div>
      </header>
      <div className="px-3 py-3">
        <ProspectFields
          idPrefix="prospect-create"
          draft={draft}
          withStatus
          onChange={(patch) => {
            setRefusal(null);
            setDraft((current) => ({ ...current, ...patch }));
          }}
        />
        <div className="mt-3 flex items-center gap-3">
          <Button variant="glossy" onClick={submit} disabled={busy}>
            {busy ? "Creating…" : "Create prospect"}
          </Button>
          <span className="text-xs text-ink-muted">
            Stored locally. Nothing is emailed and no address is verified or enriched.
          </span>
        </div>
        {refusal !== null ? (
          <p className="mt-2 text-sm text-alert" data-testid="prospect-create-refusal">
            {refusal}
          </p>
        ) : null}
      </div>
    </section>
  );
}

/** The PATCH body from a draft: ONLY the fields whose value really changed. An emptied box is an
 *  explicit `null` (clear the column), never `""` — the two mean different things at rest. */
export function changesFrom(row: Prospect, draft: ProspectDraft): ProspectUpdate {
  const changes: ProspectUpdate = {};
  if (draft.email.trim() !== row.email) changes.email = draft.email.trim();
  if (draft.first_name.trim() !== (row.first_name ?? "")) {
    changes.first_name = draft.first_name.trim() === "" ? null : draft.first_name.trim();
  }
  if (draft.last_name.trim() !== (row.last_name ?? "")) {
    changes.last_name = draft.last_name.trim() === "" ? null : draft.last_name.trim();
  }
  if (draft.company.trim() !== (row.company ?? "")) {
    changes.company = draft.company.trim() === "" ? null : draft.company.trim();
  }
  if (draft.title.trim() !== (row.title ?? "")) {
    changes.title = draft.title.trim() === "" ? null : draft.title.trim();
  }
  if (draft.source.trim() !== (row.source ?? "")) {
    changes.source = draft.source.trim() === "" ? null : draft.source.trim();
  }
  return changes;
}

export function ProspectEditForm({
  row,
  busy,
  onSave,
  onCancel,
}: {
  row: Prospect;
  busy: boolean;
  onSave: (changes: ProspectUpdate) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<ProspectDraft>(() => ({
    email: row.email,
    first_name: row.first_name ?? "",
    last_name: row.last_name ?? "",
    company: row.company ?? "",
    title: row.title ?? "",
    source: row.source ?? "",
    status: row.status ?? "",
  }));
  const [refusal, setRefusal] = useState<string | null>(null);

  function save() {
    if (draft.email.trim() === "") {
      setRefusal("Enter the prospect's email address — nothing was saved.");
      return;
    }
    const changes = changesFrom(row, draft);
    if (Object.keys(changes).length === 0) {
      // Nothing changed: there is no PATCH to make. Close rather than send an empty write.
      onCancel();
      return;
    }
    setRefusal(null);
    onSave(changes);
  }

  return (
    <div
      data-testid="prospect-edit-form"
      className="mt-2 border border-line bg-surface px-2 py-2"
    >
      <ProspectFields
        idPrefix={`prospect-edit-${row.id}`}
        draft={draft}
        withStatus={false}
        onChange={(patch) => {
          setRefusal(null);
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
      {refusal !== null ? (
        <p className="mt-2 text-sm text-alert" data-testid={`prospect-edit-refusal-${row.id}`}>
          {refusal}
        </p>
      ) : null}
    </div>
  );
}
