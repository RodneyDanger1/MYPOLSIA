import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { InvoiceCreateForm } from "@/components/invoices/InvoiceCreateForm";

/**
 * V22/L3 — W1 GAP-2: invoice line-item ENTRY.
 *
 * `POST /invoices` accepts `line_items`, but the create form never sent them, so an owner working
 * only in the UI could not itemise a bill. The form now owns a repeating description / qty /
 * unit-price editor and sends ONE `line_items` array of the rows the operator actually filled.
 *
 * Pinned here:
 *  - a filled row reaches `onSubmit` as `line_items: [{description, quantity, unit_price}]`;
 *  - a completely blank row is omitted (nothing is invented from an untouched editor);
 *  - a non-numeric qty/price is refused BEFORE submit, by name (never silently dropped).
 */

async function openEditor(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Add line item" }));
}

function row(index: number) {
  return {
    description: screen.getByLabelText(`Line item ${index + 1} description`),
    quantity: screen.getByLabelText(`Line item ${index + 1} quantity`),
    unitPrice: screen.getByLabelText(`Line item ${index + 1} unit price`),
  };
}

describe("InvoiceCreateForm — line items (W1 GAP-2)", () => {
  it("sends the filled row as a real line_items array", async () => {
    const onSubmit = jest.fn();
    render(<InvoiceCreateForm busy={false} error={null} created={null} onSubmit={onSubmit} />);
    const user = userEvent.setup();

    await openEditor(user);
    const r = row(0);
    await user.type(r.description, "Logo design");
    await user.type(r.quantity, "2");
    await user.type(r.unitPrice, "750");
    await user.click(screen.getByRole("button", { name: "Create invoice" }));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0].line_items).toEqual([
      { description: "Logo design", quantity: 2, unit_price: 750 },
    ]);
  });

  it("a blank row is omitted — no fabricated item", async () => {
    const onSubmit = jest.fn();
    render(<InvoiceCreateForm busy={false} error={null} created={null} onSubmit={onSubmit} />);
    const user = userEvent.setup();

    await openEditor(user);
    await user.click(screen.getByRole("button", { name: "Create invoice" }));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0]).not.toHaveProperty("line_items");
  });

  it("a non-numeric quantity is refused by name before submit", async () => {
    const onSubmit = jest.fn();
    render(<InvoiceCreateForm busy={false} error={null} created={null} onSubmit={onSubmit} />);
    const user = userEvent.setup();

    await openEditor(user);
    const r = row(0);
    await user.type(r.description, "Logo design");
    await user.type(r.quantity, "two");
    await user.click(screen.getByRole("button", { name: "Create invoice" }));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/Line item 1 quantity must be a number/i);
  });
});
