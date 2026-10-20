"use client";

import { useMemo, useState } from "react";

import { ProspectCreateForm } from "@/components/prospects/ProspectForms";
import { ProspectRowView } from "@/components/prospects/ProspectRowView";
import { PROSPECT_STATUSES } from "@/components/prospects/types";
import type { Prospect, ProspectCreate, ProspectUpdate } from "@/components/prospects/types";
import { errorText, useProspectList } from "@/components/prospects/useProspects";
import { Button } from "@/components/ui";
import { api } from "@/lib/api";

/**
 * Prospects — the owner's CRM over the `prospects` table (V27/L5).
 *
 * Before this page the table was READ-ONLY from the UI (`/outreach`): the owner could see what the
 * outreach agent found and could not add, correct or remove a row. Every control here calls a REAL
 * route and nothing else (`backend/app/api/v1/prospects.py`):
 *
 *   GET    /prospects?search=&status=&limit=  -> {items, total}                (:66)
 *   POST   /prospects                         -> 201 the stored row           (:101)
 *   GET    /prospects/{id}                    -> the stored row               (:117)
 *   PATCH  /prospects/{id}                    -> the stored row, provided keys (:129)
 *   DELETE /prospects/{id}                    -> 204                          (:146)
 *
 * Rules rendered, never re-invented:
 *   - the SERVER's answer is the only truth an action paints: create merges the response body
 *     (so the normalised, lowercased address is what appears, not the typed one), a status edit
 *     repaints from the PATCH response, and a refused action leaves the row as it was;
 *   - a duplicate/blank address is `E_VALIDATION` with `context.field="email"`, rendered through
 *     `ApiError.display` — never a generic apology;
 *   - delete is TWO steps: the first press only opens the confirm block;
 *   - loading / failed read / empty are three disjoint states, and a FILTERED empty result says
 *     so instead of claiming the table is empty;
 *   - an empty table still offers the add form — the one control that can change it.
 *
 * Nothing here sends email, verifies an address, scrapes or enriches: the surface is local CRUD.
 */

/** The read's page size — the server's own maximum page for this route is 500. */
const LIST_LIMIT = 100;

export default function ProspectsPage() {
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [reload, setReload] = useState(0);

  // The read path IS the query: one source for what the server was asked, so the rows, the count
  // and the empty-state copy can never describe different filters.
  const path = useMemo(() => {
    const params = new URLSearchParams({ limit: String(LIST_LIMIT) });
    if (search !== "") params.set("search", search);
    if (status !== "") params.set("status", status);
    return `/prospects?${params.toString()}`;
  }, [search, status]);

  const { rows, total, error, loading, setRows, setTotal } = useProspectList(path, reload);

  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [createdId, setCreatedId] = useState<number | null>(null);
  const [editId, setEditId] = useState<number | null>(null);
  const [deleteId, setDeleteId] = useState<number | null>(null);

  const filtered = search !== "" || status !== "";

  /** Replace one server-answered row in place — the write routes return the stored row. */
  function mergeProspect(updated: Prospect) {
    setRows((current) =>
      current === null ? current : current.map((row) => (row.id === updated.id ? updated : row)),
    );
  }

  async function create(body: ProspectCreate) {
    setBusy("create");
    setActionError(null);
    try {
      const created = await api.post<Prospect>("/prospects", body);
      setCreatedId(created.id);
      if (filtered) {
        // A row that may not match the ACTIVE filter: only a read can say whether it belongs in
        // this page, so the filtered view is re-read instead of prepending a row that might not
        // match and inflating a total that describes the filter.
        setReload((n) => n + 1);
        return;
      }
      // Newest-first order, and only on top of a list that was really read.
      setRows((current) => (current === null ? current : [created, ...current]));
      setTotal((current) => (current === null ? current : current + 1));
    } catch (e: unknown) {
      setActionError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function setStatusFor(row: Prospect, next: string) {
    setBusy(`status-${row.id}`);
    setActionError(null);
    try {
      const updated = await api.patch<Prospect>(`/prospects/${row.id}`, { status: next });
      if (filtered) {
        // The status IS the filter's subject: a changed row may leave the page, so the filtered
        // view is re-read rather than painting a row the filter no longer selects.
        setReload((n) => n + 1);
        return;
      }
      mergeProspect(updated);
    } catch (e: unknown) {
      setActionError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function save(row: Prospect, changes: ProspectUpdate) {
    setBusy(`save-${row.id}`);
    setActionError(null);
    try {
      const updated = await api.patch<Prospect>(`/prospects/${row.id}`, changes);
      if (filtered) {
        // Same rule as the status edit: an active filter decides membership, so the read decides.
        setEditId(null);
        setReload((n) => n + 1);
        return;
      }
      mergeProspect(updated);
      setEditId(null);
    } catch (e: unknown) {
      setActionError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function remove(row: Prospect) {
    setBusy(`delete-${row.id}`);
    setActionError(null);
    try {
      // 204 No Content: the route names no body, so the list drops the row it was told to delete.
      await api.delete(`/prospects/${row.id}`);
      setRows((current) => (current === null ? current : current.filter((r) => r.id !== row.id)));
      setTotal((current) => (current === null ? current : Math.max(0, current - 1)));
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
        <h1 className="text-3xl font-bold text-ink">Prospects</h1>
        <p className="ui-label text-ink-muted">
          The owner&apos;s prospect list — add, correct and remove a row. Emails are stored trimmed
          and lowercased; a duplicate address is refused by name.
        </p>
      </div>

      {actionError !== null ? (
        <p role="alert" className="text-sm text-alert" data-testid="prospects-action-error">
          {actionError}
        </p>
      ) : null}

      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          setSearch(searchDraft.trim());
        }}
      >
        <div className="min-w-[14rem] flex-1">
          <label className="ui-label text-ink-muted" htmlFor="prospects-search">
            Search prospects
          </label>
          <input
            id="prospects-search"
            className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
            value={searchDraft}
            onChange={(e) => setSearchDraft(e.target.value)}
            placeholder="email, name or company"
          />
        </div>
        <Button type="submit">Search</Button>
        <div>
          <label className="ui-label text-ink-muted" htmlFor="prospects-status-filter">
            Filter by status
          </label>
          <select
            id="prospects-status-filter"
            className="mt-1 border border-line bg-surface px-2 py-1 text-sm text-ink"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            <option value="">All statuses</option>
            {PROSPECT_STATUSES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>
        <Button onClick={() => setReload((n) => n + 1)} disabled={busy !== null}>
          Refresh
        </Button>
      </form>

      {/* A count of what THIS read measured: the page length, and — when the server declared it —
          the full match count. Never the page length dressed up as a total. */}
      {rows !== null ? (
        <p className="ui-label text-ink-muted" data-testid="prospects-count">
          {total !== null && total > rows.length
            ? `${rows.length} of ${total} prospects`
            : `${rows.length} ${rows.length === 1 ? "prospect" : "prospects"}`}
        </p>
      ) : null}

      {loading ? (
        <div className="space-y-2" data-testid="prospects-loading" aria-busy="true">
          <p className="ui-label text-ink-muted">Loading…</p>
          {Array.from({ length: 3 }).map((_, index) => (
            <div
              key={index}
              className="flex items-center justify-between gap-3 border border-line px-3 py-3"
            >
              <div className="h-3 w-2/5 animate-pulse bg-surface-strong" />
              <div className="h-3 w-16 animate-pulse bg-surface-strong" />
            </div>
          ))}
        </div>
      ) : null}

      {!loading && error !== null ? (
        <p role="alert" className="text-sm text-alert" data-testid="prospects-read-error">
          Could not load the prospects: {error}
        </p>
      ) : null}

      {!loading && error === null && rows !== null && rows.length === 0 ? (
        <p className="text-sm text-ink-muted" data-testid="prospects-empty">
          {filtered
            ? "No prospects match this search or status filter. Clear them to see every prospect."
            : "No prospects yet. Add the first one below — the email address is the only required field."}
        </p>
      ) : null}

      {error === null && rows !== null && rows.length > 0 ? (
        <ul className="card-surface" data-testid="prospects-list">
          {rows.map((row) => (
            <ProspectRowView
              key={row.id}
              prospect={row}
              busy={busy !== null}
              editing={editId === row.id}
              confirming={deleteId === row.id}
              onStartEdit={() => {
                setEditId(row.id);
                setDeleteId(null);
                setActionError(null);
              }}
              onCancelEdit={() => setEditId(null)}
              onSave={(changes) => void save(row, changes)}
              onStatus={(next) => void setStatusFor(row, next)}
              onStartDelete={() => {
                setDeleteId(row.id);
                setEditId(null);
                setActionError(null);
              }}
              onCancelDelete={() => setDeleteId(null)}
              onConfirmDelete={() => void remove(row)}
            />
          ))}
        </ul>
      ) : null}

      <ProspectCreateForm
        busy={busy === "create"}
        lastCreatedId={createdId}
        onSubmit={(body) => void create(body)}
      />
    </div>
  );
}
