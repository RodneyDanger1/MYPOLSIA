"use client";

import { useEffect, useState } from "react";

import { Button, Card } from "@/components/ui";
import type { InvoiceCreate } from "@/lib/api";

/**
 * The create form of the /invoices page — `POST /invoices` (`backend/app/api/v1/invoices.py` :64).
 *
 * The body it builds is exactly the `InvoiceCreateRequest` wire shape (:33-49): a field the
 * operator left EMPTY is OMITTED (absent stays absent — the service stores what it was given and
 * never fabricates a 0 from a blank input), and `bill_to` rides only when a name or email was
 * typed. A non-blank `net_days` ALWAYS rides the body (S2-F3, wave v14): the request model's own
 * `ge=0, le=365` refusal is the honest answer for an out-of-range value — the form must not drop
 * the operator's stated terms and let the service substitute its default under a success notice.
 * `number`, `status` and the derived `due_date` are the SERVER's to assign; the form never
 * invents them.
 *
 * V22/L3 (W1 GAP-2): the form also owns the LINE-ITEM editor. `POST /invoices` accepts
 * `line_items`, and before this the UI could not send any — an owner could only create lump-sum
 * bills. A blank row is omitted; a touched row must name its description and carry numeric
 * qty/price (validated here, before the POST, so nothing malformed is silently dropped).
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
  /** W1 GAP-2: the itemisation editor — string drafts, one row per item. A completely blank row
   *  is omitted; a partially filled row must name its description and carry numeric qty/price. */
  const [itemDrafts, setItemDrafts] = useState<
    Array<{ description: string; quantity: string; unitPrice: string }>
  >([]);
  const [itemError, setItemError] = useState<string | null>(null);

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
      setItemDrafts([]);
      setItemError(null);
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
    setItemError(null);

    // W1 GAP-2: line items — a blank row is omitted; a touched row must be complete enough to be
    // a real item (description + numeric qty/price). Validation happens BEFORE the POST, so a
    // malformed item is refused by name instead of being silently dropped or stored as junk.
    const lineItems: Record<string, unknown>[] = [];
    for (let index = 0; index < itemDrafts.length; index += 1) {
      const draft = itemDrafts[index];
      const description = draft.description.trim();
      const quantity = draft.quantity.trim();
      const unitPrice = draft.unitPrice.trim();
      if (description === "" && quantity === "" && unitPrice === "") continue;
      if (description === "") {
        setItemError(`Line item ${index + 1} needs a description — nothing was sent.`);
        return;
      }
      const row: Record<string, unknown> = { description };
      if (quantity !== "") {
        const parsed = Number(quantity);
        if (!Number.isFinite(parsed)) {
          setItemError(`Line item ${index + 1} quantity must be a number — nothing was sent.`);
          return;
        }
        row.quantity = parsed;
      }
      if (unitPrice !== "") {
        const parsed = Number(unitPrice);
        if (!Number.isFinite(parsed)) {
          setItemError(`Line item ${index + 1} unit price must be a number — nothing was sent.`);
          return;
        }
        row.unit_price = parsed;
      }
      lineItems.push(row);
    }
    if (lineItems.length > 0) body.line_items = lineItems;

    const billTo: Record<string, unknown> = {};
    if (name.trim() !== "") billTo["name"] = name.trim();
    if (email.trim() !== "") billTo["email"] = email.trim();
    if (Object.keys(billTo).length > 0) body.bill_to = billTo;

    const net = money(netDays);
    // Non-blank net days ALWAYS ride the body (S2-F3, wave v14): an out-of-range value is refused
    // BY NAME by the request model (`InvoiceCreateRequest.net_days`, `ge=0, le=365`, →
    // E_VALIDATION) and that refusal is rendered above — it is never silently dropped so the
    // service can apply its own default.
    if (net !== undefined) body.net_days = net;

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

        {/* W1 GAP-2: the itemisation editor. Rows are drafts; a blank row is omitted on submit,
            and a malformed row is refused by name (never silently dropped). */}
        <div className="border-t border-line pt-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="ui-label text-ink">Line items</p>
              <p className="ui-label mt-1 text-ink-muted">
                Optional — itemise the bill, or leave it empty for a lump sum.
              </p>
            </div>
            <Button
              size="sm"
              onClick={() =>
                setItemDrafts((current) => [
                  ...current,
                  { description: "", quantity: "", unitPrice: "" },
                ])
              }
            >
              Add line item
            </Button>
          </div>

          {itemDrafts.length > 0 ? (
            <ul className="mt-2 space-y-2">
              {itemDrafts.map((draft, index) => (
                <li key={index} className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_80px_100px_auto]">
                  <input
                    aria-label={`Line item ${index + 1} description`}
                    className="w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
                    placeholder="Description"
                    value={draft.description}
                    onChange={(e) =>
                      setItemDrafts((current) =>
                        current.map((row, i) =>
                          i === index ? { ...row, description: e.target.value } : row,
                        ),
                      )
                    }
                  />
                  <input
                    aria-label={`Line item ${index + 1} quantity`}
                    inputMode="numeric"
                    className="w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
                    placeholder="Qty"
                    value={draft.quantity}
                    onChange={(e) =>
                      setItemDrafts((current) =>
                        current.map((row, i) =>
                          i === index ? { ...row, quantity: e.target.value } : row,
                        ),
                      )
                    }
                  />
                  <input
                    aria-label={`Line item ${index + 1} unit price`}
                    inputMode="decimal"
                    className="w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
                    placeholder="Unit price"
                    value={draft.unitPrice}
                    onChange={(e) =>
                      setItemDrafts((current) =>
                        current.map((row, i) =>
                          i === index ? { ...row, unitPrice: e.target.value } : row,
                        ),
                      )
                    }
                  />
                  <Button
                    size="sm"
                    aria-label={`Remove line item ${index + 1}`}
                    onClick={() =>
                      setItemDrafts((current) => current.filter((_, i) => i !== index))
                    }
                  >
                    Remove
                  </Button>
                </li>
              ))}
            </ul>
          ) : null}

          {itemError !== null ? (
            <p role="alert" className="mt-1 text-sm text-alert" data-testid="invoice-items-error">
              {itemError}
            </p>
          ) : null}
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
