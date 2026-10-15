"use client";

import { useState } from "react";

import { NOT_MEASURED, absoluteStamp } from "@/components/dashboard/format";
import { usdText } from "@/components/finance/format";
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
 *   - Send is offered on every row and the backend is the arbiter: an illegal transition comes
 *     back as its named E_VALIDATION refusal rather than a client-side guess at the matrix;
 *   - an action's answer (each route returns the `InvoiceResponse`) replaces the row it names —
 *     no follow-up read is invented for data the action already returned;
 *   - loading / failed-read / empty are three disjoint states, each stated in its own words.
 */

/** The server's own default bound for the list read (`invoices.py:88`, le=500). */
const INVOICE_LIMIT = 100;

/** The frozen status vocabulary (`app/models/invoice.InvoiceStatus`) — the filter passes the word
 *  through to the server, which refuses an illegal one by name. */
const STATUS_FILTERS = ["draft", "sent", "paid", "void", "overdue"] as const;

/** The bill-to display name, safely off the free-form `bill_to` JSON (null -> em dash). */
function billToName(row: Invoice): string {
  const billTo = row.bill_to;
  const name = billTo !== null && typeof billTo === "object" ? billTo["name"] : null;
  return typeof name === "string" && name.trim() !== "" ? name : NOT_MEASURED;
}

export default function InvoicesPage() {
  const [statusFilter, setStatusFilter] = useState("");
  const path =
    statusFilter === ""
      ? `/invoices?limit=${INVOICE_LIMIT}`
      : `/invoices?limit=${INVOICE_LIMIT}&status=${encodeURIComponent(statusFilter)}`;

  const { rows, error, loading, setRows } = useInvoiceList(path);
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

      {!loading && error === null && rows !== null && rows.length > 0 ? (
        <Card title="All invoices" data-testid="invoices-list">
          <div className="divide-y divide-line">
            {rows.map((row) => (
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
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" onClick={() => void send(row)} disabled={busy !== null}>
                    Send
                  </Button>
                  {voidingId === row.id ? (
                    <>
                      <Button
                        size="sm"
                        data-testid={`invoice-void-confirm-${row.id}`}
                        onClick={() => void voidInvoice(row)}
                        disabled={busy !== null}
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
                      disabled={busy !== null}
                    >
                      Void
                    </Button>
                  )}
                </div>
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
            ))}
          </div>
        </Card>
      ) : null}

      <InvoiceCreateForm busy={busy === "create"} error={createError} created={createdNumber} onSubmit={(body) => void create(body)} />
    </div>
  );
}
