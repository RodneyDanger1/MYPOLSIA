"use client";

import { useState } from "react";

import { NOT_MEASURED, absoluteStamp } from "@/components/dashboard/format";
import { countText, usdText } from "@/components/finance/format";
import { InvoiceCreateForm } from "@/components/invoices/InvoiceCreateForm";
import { errorText, useInvoiceList } from "@/components/invoices/useInvoices";
import { Button, Card, Chip } from "@/components/ui";
import { api } from "@/lib/api";
import type { Invoice, InvoiceCreate } from "@/lib/api";

/**
 * Invoices — the billing surface the backend shipped in V9 with no UI (wiring audit F2).
 *
 * Every control calls a REAL route and nothing else (`backend/app/api/v1/invoices.py`):
 *   GET   /invoices?limit=&status=   -> the bare-array list (:85; status is the service's
 *                                       `InvoiceStatus` filter — an illegal word refuses by name)
 *   POST  /invoices                  -> create; the server assigns number + draft (:64)
 *   POST  /invoices/{id}/send        -> render + firewalled delivery; flips to sent ONLY on a
 *                                       confirmed provider delivery (:103)
 *   POST  /invoices/{id}/void        -> terminal void from draft/sent/overdue (:122)
 *
 * Honesty rules, in code:
 *   - money/date fields are `number | null` / `datetime | None` on the wire; null renders the em
 *     dash (`usdText`/`NOT_MEASURED`), a measured 0 still renders `$0.00` — null != 0;
 *   - a refused action (E_CONFIG / E_SANDBOX_BLOCKED / E_RATE_LIMITED / E_INTEGRATION /
 *     E_VALIDATION / E_NOT_FOUND) renders `ApiError.display` — code + message + how_to_fix —
 *     and the row keeps the SERVER's last state (never an optimistic flip);
 *   - Send is offered only on a DRAFT row and Void only from draft/sent/overdue — the client-side
 *     mirror of `LEGAL_TRANSITIONS` (`backend/app/services/invoice_service.py:77-83`); paid and
 *     void are terminal. A row whose status is NOT a word the matrix knows keeps both buttons
 *     ENABLED: the client must not invent a refusal it cannot know, so the backend stays the
 *     arbiter there. Every disabled button carries its reason as visible row text (never only a
 *     tooltip), and a refusal the server returns is still rendered as `ApiError.display`;
 *   - an action's answer (each route returns the `InvoiceResponse`) replaces the row it names —
 *     no follow-up read is invented for data the action already returned;
 *   - loading / failed-read / empty are three disjoint states, each stated in its own words.
 */

/** The server's own default bound for the list read (`invoices.py:88`, le=500). */
const INVOICE_LIMIT = 100;

/** The frozen status vocabulary (`app/models/invoice.InvoiceStatus`) — the filter passes the word
 *  through to the server, which refuses an illegal one by name. */
const STATUS_FILTERS = ["draft", "sent", "paid", "void", "overdue"] as const;

/** The backend's transition matrix, mirrored (`invoice_service.LEGAL_TRANSITIONS`):
 *  draft -> {sent, void}, sent -> {paid, overdue, void}, overdue -> {paid, void}; paid/void are
 *  terminal. These sets gate the CONTROLS only — the server is still the arbiter of every call. */
const SENDABLE: ReadonlySet<string> = new Set(["draft"]);
const VOIDABLE: ReadonlySet<string> = new Set(["draft", "sent", "overdue"]);
/** The words this client knows. A status outside them is not a refusal the client can predict. */
const KNOWN_STATUSES: ReadonlySet<string> = new Set(STATUS_FILTERS);

/** Why Send is closed on this row — the backend's own rule, in the UI's words. */
function sendClosedReason(status: string): string {
  return `only a draft invoice can be sent — this row is ${status} (the dunning ladder owns follow-ups on a sent invoice)`;
}

/** Why Send and Void are both closed: nothing leaves a terminal state. */
function terminalReason(status: string): string {
  return `send and void are closed: ${status} is a terminal state — the server has no legal transition out of it`;
}

/** The bill-to display name, safely off the free-form `bill_to` JSON (null -> em dash). */
function billToName(row: Invoice): string {
  const billTo = row.bill_to;
  const name = billTo !== null && typeof billTo === "object" ? billTo["name"] : null;
  return typeof name === "string" && name.trim() !== "" ? name : NOT_MEASURED;
}

/** One free-form line item, in the item's own fields: description, quantity and amount when the
 *  object carries them; a NAMED absence when it carries none (never `undefined`, never a `NaN`
 *  price, never a blank row that reads as an item with no content). Booleans, arrays and non-string
 *  descriptions are not display claims. */
function lineItemText(item: unknown, index: number): string {
  const fallback = `Item ${index + 1} — no description, quantity or amount recorded`;
  if (item === null || typeof item !== "object" || Array.isArray(item)) return fallback;
  const record = item as Record<string, unknown>;
  const description =
    typeof record.description === "string" && record.description.trim() !== ""
      ? record.description
      : null;
  const qtyRaw = record.quantity ?? record.qty;
  const qty = typeof qtyRaw === "number" && Number.isFinite(qtyRaw) ? countText(qtyRaw) : null;
  const amountRaw = record.amount ?? record.unit_price ?? record.price ?? record.total;
  const amount =
    typeof amountRaw === "number" && Number.isFinite(amountRaw) ? usdText(amountRaw) : null;
  if (description === null && qty === null && amount === null) return fallback;
  return [description ?? `Item ${index + 1}`, qty !== null ? `qty ${qty}` : null, amount]
    .filter((part): part is string => part !== null)
    .join(" · ");
}

/** The row's read-only itemisation. The list read already carries `line_items` (the SAME schema
 *  the create/get routes serve), so this opens locally and issues no request. `null` and `[]` are
 *  "nothing was recorded" and draw no expander — an empty shell would imply items exist. */
function InvoiceLineItems({ row }: { row: Invoice }) {
  const items = Array.isArray(row.line_items) ? row.line_items : [];
  if (items.length === 0) return null;
  return (
    <details data-testid={`invoice-items-${row.id}`} className="text-xs text-ink-muted">
      <summary className="ui-label cursor-pointer text-link">Items ({items.length})</summary>
      <ul className="mt-1 space-y-1" data-testid={`invoice-items-list-${row.id}`}>
        {items.map((item, index) => (
          <li key={index}>{lineItemText(item, index)}</li>
        ))}
      </ul>
    </details>
  );
}

export default function InvoicesPage() {
  const [statusFilter, setStatusFilter] = useState("");
  const path =
    statusFilter === ""
      ? `/invoices?limit=${INVOICE_LIMIT}`
      : `/invoices?limit=${INVOICE_LIMIT}&status=${encodeURIComponent(statusFilter)}`;

  const { rows, error, loading, total, setRows } = useInvoiceList(path);
  const [busy, setBusy] = useState<string | null>(null);
  const [rowError, setRowError] = useState<{ id: number; text: string } | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createdNumber, setCreatedNumber] = useState<string | null>(null);
  const [voidingId, setVoidingId] = useState<number | null>(null);

  /** Merge one server-answered invoice into the list: replace in place when the list already
   *  holds it (send/void), prepend when it is new (create — the list is newest-first). When a
   *  status filter is active and the row no longer matches it, the row LEAVES the list — that is
   *  what the next unfiltered read would say, not an invention. */
  function mergeInvoice(updated: Invoice) {
    setRows((current) => {
      if (current === null) return current;
      const matches = statusFilter === "" || updated.status === statusFilter;
      const withoutRow = current.filter((row) => row.id !== updated.id);
      if (!matches) return withoutRow;
      const existed = current.some((row) => row.id === updated.id);
      return existed
        ? current.map((row) => (row.id === updated.id ? updated : row))
        : [updated, ...withoutRow];
    });
  }

  async function create(body: InvoiceCreate) {
    setBusy("create");
    setCreateError(null);
    try {
      const created = await api.post<Invoice>("/invoices", body);
      setCreatedNumber(created.number);
      mergeInvoice(created);
    } catch (e: unknown) {
      setCreateError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function send(row: Invoice) {
    setBusy(`send-${row.id}`);
    setRowError(null);
    try {
      // No cover note on this surface yet: the optional InvoiceSendRequest body rides empty
      // (`invoices.py:52-56`), never a fabricated note.
      const updated = await api.post<Invoice>(`/invoices/${row.id}/send`, {});
      mergeInvoice(updated);
    } catch (e: unknown) {
      setRowError({ id: row.id, text: errorText(e) });
    } finally {
      setBusy(null);
    }
  }

  async function voidInvoice(row: Invoice) {
    setBusy(`void-${row.id}`);
    setRowError(null);
    try {
      const updated = await api.post<Invoice>(`/invoices/${row.id}/void`, {});
      mergeInvoice(updated);
    } catch (e: unknown) {
      setRowError({ id: row.id, text: errorText(e) });
    } finally {
      setBusy(null);
      setVoidingId(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-2 border-b border-line pb-1">
        <div className="mr-auto">
          <h1 className="text-3xl font-bold text-ink">Invoices</h1>
          <p className="ui-label text-ink-muted">
            Numbers, draft status and due dates are assigned by the server; a blank money field
            stays unmeasured.
          </p>
        </div>
        <div>
          <select
            aria-label="Status filter"
            className="border border-line bg-surface px-2 py-1 text-sm text-ink"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            <option value="">All statuses</option>
            {STATUS_FILTERS.map((status) => (
              <option key={status} value={status}>
                {status}
              </option>
            ))}
          </select>
        </div>
      </div>

      {loading ? (
        <p className="ui-label text-ink-muted" aria-busy="true" data-testid="invoices-loading">
          Loading…
        </p>
      ) : null}

      {!loading && error !== null ? (
        <p role="alert" className="text-sm text-alert" data-testid="invoices-read-error">
          Could not load the invoice list: {error}
        </p>
      ) : null}

      {!loading && error === null && rows !== null && rows.length === 0 ? (
        <p className="text-sm text-ink-muted" data-testid="invoices-empty">
          No invoices yet — create one below; the server assigns its number (INV-YYYY-NNN).
        </p>
      ) : null}

      {/* W1 GAP-1a: the list is capped at INVOICE_LIMIT; when the response declared a larger
          total, say both numbers. Before this, 101+ invoices presented the first 100 as the whole
          table with no marker at all. */}
      {!loading && error === null && rows !== null && total !== null && total > rows.length ? (
        <p className="ui-label text-ink-muted" data-testid="invoices-truncation">
          Showing the first {rows.length} of {total} invoices — the API caps this read at{" "}
          {INVOICE_LIMIT} per request.
        </p>
      ) : null}

      {!loading && error === null && rows !== null && rows.length > 0 ? (
        <Card title="All invoices" data-testid="invoices-list">
          <div className="divide-y divide-line">
            {rows.map((row) => {
              // The client-side mirror of the server's matrix. `!knownStatus` keeps an unknown
              // word OUT of the guess: both controls stay live and the backend decides.
              const knownStatus = KNOWN_STATUSES.has(row.status);
              const canSend = !knownStatus || SENDABLE.has(row.status);
              const canVoid = !knownStatus || VOIDABLE.has(row.status);
              const terminal = knownStatus && !canVoid;
              return (
              <div key={row.id} data-testid={`invoice-row-${row.id}`} className="space-y-1 py-2">
                <div className="flex flex-wrap items-baseline gap-3">
                  <span className="font-bold text-ink" data-testid="invoice-number">
                    {row.number}
                  </span>
                  <Chip variant="soft" data-testid="invoice-status">
                    {row.status}
                  </Chip>
                  <span className="ml-auto text-sm text-ink" data-testid="invoice-total">
                    {usdText(row.total)}
                  </span>
                </div>
                <div className="flex flex-wrap gap-3 ui-label text-ink-muted">
                  <span data-testid="invoice-bill-to">{billToName(row)}</span>
                  <span>
                    Issued <span data-testid="invoice-issued">{row.issue_date}</span>
                  </span>
                  <span>
                    Due <span data-testid="invoice-due">{row.due_date}</span>
                  </span>
                  <span>
                    Paid{" "}
                    <span data-testid="invoice-paid">
                      {row.paid_at !== null ? absoluteStamp(row.paid_at) : NOT_MEASURED}
                    </span>
                  </span>
                </div>
                {row.late_fee_note !== null && row.late_fee_note !== "" ? (
                  <p className="text-xs text-ink-muted">Late fee: {row.late_fee_note}</p>
                ) : null}
                <InvoiceLineItems row={row} />
                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    onClick={() => void send(row)}
                    disabled={busy !== null || !canSend}
                    title={canSend ? undefined : terminal ? terminalReason(row.status) : sendClosedReason(row.status)}
                  >
                    Send
                  </Button>
                  {voidingId === row.id ? (
                    <>
                      <Button
                        size="sm"
                        data-testid={`invoice-void-confirm-${row.id}`}
                        onClick={() => void voidInvoice(row)}
                        disabled={busy !== null || !canVoid}
                        title={canVoid ? undefined : terminalReason(row.status)}
                      >
                        Confirm void
                      </Button>
                      <Button size="sm" onClick={() => setVoidingId(null)} disabled={busy !== null}>
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <Button
                      size="sm"
                      onClick={() => {
                        setVoidingId(row.id);
                        setRowError(null);
                      }}
                      disabled={busy !== null || !canVoid}
                      title={canVoid ? undefined : terminalReason(row.status)}
                    >
                      Void
                    </Button>
                  )}
                </div>
                {/* The reason is VISIBLE row text, not only a tooltip: a disabled control with no
                    stated cause is the same lie as an enabled one that always refuses. */}
                {!canSend && !terminal ? (
                  <p
                    className="ui-label text-ink-muted"
                    data-testid={`invoice-send-reason-${row.id}`}
                  >
                    {sendClosedReason(row.status)}
                  </p>
                ) : null}
                {terminal ? (
                  <p
                    className="ui-label text-ink-muted"
                    data-testid={`invoice-terminal-reason-${row.id}`}
                  >
                    {terminalReason(row.status)}
                  </p>
                ) : null}
                {rowError !== null && rowError.id === row.id ? (
                  // The refusal, by name: ApiError.display — the row keeps the SERVER's last state.
                  <p
                    role="alert"
                    className="text-sm text-alert"
                    data-testid={`invoice-action-error-${row.id}`}
                  >
                    {rowError.text}
                  </p>
                ) : null}
              </div>
              );
            })}
          </div>
        </Card>
      ) : null}

      <InvoiceCreateForm busy={busy === "create"} error={createError} created={createdNumber} onSubmit={(body) => void create(body)} />
    </div>
  );
}
