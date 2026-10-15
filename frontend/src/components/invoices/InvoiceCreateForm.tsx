"use client";

import { useEffect, useState } from "react";

import { Button, Card } from "@/components/ui";
import type { InvoiceCreate } from "@/lib/api";

/**
 * The create form of the /invoices page — `POST /invoices` (`backend/app/api/v1/invoices.py` :64).
 *
 * The body it builds is exactly the `InvoiceCreateRequest` wire shape (:33-49): a field the
 * operator left empty is OMITTED (absent stays absent — the service stores what it was given and
 * never fabricates a 0 from a blank input), and `bill_to` rides only when a name or email was
 * typed. `number`, `status` and the derived `due_date` are the SERVER's to assign; the form never
 * invents them.
 */
export function InvoiceCreateForm({
  busy,
  error,
  created,
  onSubmit,
}: {
  busy: boolean;
  error: string | null;
  /** The number of the last created invoice (the server's answer), or null. */
  created: string | null;
  onSubmit: (body: InvoiceCreate) => void;
}) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [netDays, setNetDays] = useState("30");
  const [subtotal, setSubtotal] = useState("");
  const [tax, setTax] = useState("");
  const [total, setTotal] = useState("");
  const [paymentLink, setPaymentLink] = useState("");

  // A created invoice is the server's row; the next create starts from clean inputs (net-days
  // stays — it is a standing operator preference, not part of the created invoice's identity).
  useEffect(() => {
    if (created !== null) {
      setName("");
      setEmail("");
      setSubtotal("");
      setTax("");
      setTotal("");
      setPaymentLink("");
    }
  }, [created]);

  /** Non-empty input parsed to a finite number; blank (or junk) is OMITTED, never 0. */
  function money(raw: string): number | undefined {
    const text = raw.trim();
    if (text === "") return undefined;
    const value = Number(text);
    return Number.isFinite(value) ? value : undefined;
  }

  function submit() {
    const body: InvoiceCreate = {};
    const billTo: Record<string, unknown> = {};
    if (name.trim() !== "") billTo["name"] = name.trim();
    if (email.trim() !== "") billTo["email"] = email.trim();
    if (Object.keys(billTo).length > 0) body.bill_to = billTo;

    const net = money(netDays);
    if (net !== undefined && Number.isInteger(net) && net >= 0 && net <= 365) body.net_days = net;

    const subtotalValue = money(subtotal);
    if (subtotalValue !== undefined) body.subtotal = subtotalValue;
    const taxValue = money(tax);
    if (taxValue !== undefined) body.tax = taxValue;
    const totalValue = money(total);
    if (totalValue !== undefined) body.total = totalValue;
    if (paymentLink.trim() !== "") body.payment_link = paymentLink.trim();

    onSubmit(body);
  }

  return (
    <Card title="New invoice" data-testid="invoice-create">
      <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="ui-label text-ink-muted" htmlFor="invoice-create-name">
              Bill to name
            </label>
            <input
              id="invoice-create-name"
              className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div>
            <label className="ui-label text-ink-muted" htmlFor="invoice-create-email">
              Bill to email
            </label>
            <input
              id="invoice-create-email"
              className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div>
            <label className="ui-label text-ink-muted" htmlFor="invoice-create-net-days">
              Net days
            </label>
            <input
              id="invoice-create-net-days"
              type="number"
              min={0}
              max={365}
              className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
              value={netDays}
              onChange={(e) => setNetDays(e.target.value)}
            />
          </div>
          <div>
            <label className="ui-label text-ink-muted" htmlFor="invoice-create-payment-link">
              Payment link
            </label>
            <input
              id="invoice-create-payment-link"
              className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
              value={paymentLink}
              onChange={(e) => setPaymentLink(e.target.value)}
            />
          </div>
          <div>
            <label className="ui-label text-ink-muted" htmlFor="invoice-create-subtotal">
              Subtotal
            </label>
            <input
              id="invoice-create-subtotal"
              type="number"
              className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
              value={subtotal}
              onChange={(e) => setSubtotal(e.target.value)}
            />
          </div>
          <div>
            <label className="ui-label text-ink-muted" htmlFor="invoice-create-tax">
              Tax
            </label>
            <input
              id="invoice-create-tax"
              type="number"
              className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
              value={tax}
              onChange={(e) => setTax(e.target.value)}
            />
          </div>
          <div>
            <label className="ui-label text-ink-muted" htmlFor="invoice-create-total">
              Total
            </label>
            <input
              id="invoice-create-total"
              type="number"
              className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
              value={total}
              onChange={(e) => setTotal(e.target.value)}
            />
          </div>
        </div>

        <div className="flex items-center gap-3">
          <Button variant="glossy" onClick={submit} disabled={busy}>
            {busy ? "Creating…" : "Create invoice"}
          </Button>
          <span className="text-xs text-ink-muted">
            The server assigns the number (INV-YYYY-NNN) and the draft status; a blank money field
            stays unmeasured.
          </span>
        </div>

        {created !== null ? (
          <p className="text-sm text-ink" data-testid="invoice-created-note">
            Created {created} — the server assigned its number.
          </p>
        ) : null}
        {error !== null ? (
          <p role="alert" className="text-sm text-alert" data-testid="invoice-create-error">
            {error}
          </p>
        ) : null}
      </div>
    </Card>
  );
}
