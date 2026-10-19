import { test, expect, type Locator } from "@playwright/test";
import { API_BASE, PREFIX } from "./_helpers";

/**
 * V10/H2 — invoices v1 on the wire (V9/B4), against the live API.
 *
 * The one documented WRITE of this seat's specs (V10-D5): the spec creates a DRAFT fixture
 * invoice, walks the real surface — it must appear in the list with its INV number — then drives
 * the send, whose EXPECTED outcome today is the named refusal (sender identity unset on the
 * company row: sender_legal_name / sender_postal_address are blank — measured live before this
 * spec pinned E_CONFIG), proves the row STAYED a labelled draft, and VOIDS the fixture as its
 * reversible cleanup. The void runs in a `finally`, so a failed assertion above cannot strand a
 * live draft (void is legal from draft AND from sent — the unexpected-success path stays clean).
 *
 * Every response body is printed to the run log as raw evidence, and the fixtures carry the
 * suite's `[e2e-fixture]` prefix so any residual row is attributable.
 */

type InvoiceRow = {
  id: number;
  number: string;
  status: string;
  currency: string;
  total: number | null;
  bill_to: Record<string, unknown> | null;
  line_items: unknown[] | null;
};

// A fixed, test-shaped body: everything the router accepts, nothing it requires beyond meaning.
const FIXTURE_BODY = {
  bill_to: { name: `${PREFIX} invoice spec`, email: "e2e-fixture@example.invalid" },
  line_items: [{ description: `${PREFIX} lab line item`, amount: 10.0 }],
  currency: "USD",
  subtotal: 10.0,
  tax: 0.0,
  total: 10.0,
};

test.describe("Invoices (wire)", () => {
  test("draft create → list shows it → send refused BY NAME (identity unset) → still draft → void cleanup (V10 lab)", async ({
    request,
  }) => {
    const created = await request.post(`${API_BASE}/api/v1/invoices`, { data: FIXTURE_BODY });
    expect(created.status()).toBe(201);
    const draft = (await created.json()) as InvoiceRow;
    console.log("[invoices-spec] create:", JSON.stringify(draft));
    expect(draft.status).toBe("draft");
    expect(draft.number).toMatch(/^INV-\d{4}-\d{3}$/);

    try {
      // The list shows the draft with its INV number.
      const list = await request.get(`${API_BASE}/api/v1/invoices`);
      expect(list.status()).toBe(200);
      const rows = (await list.json()) as InvoiceRow[];
      const listed = rows.find((row) => row.id === draft.id);
      expect(listed, "the created draft must appear in GET /invoices").toBeTruthy();
      expect(listed?.number).toBe(draft.number);
      expect(listed?.status).toBe("draft");

      // The send's expected outcome TODAY: the sender identity is unset, so the send must refuse
      // BY NAME — never a success, never an unnamed failure (the envelope's error object).
      const send = await request.post(`${API_BASE}/api/v1/invoices/${draft.id}/send`, { data: {} });
      const refusal = (await send.json()) as {
        ok: boolean;
        error: { code: string; message: string; how_to_fix?: string };
      };
      console.log("[invoices-spec] send:", send.status(), JSON.stringify(refusal));
      expect(send.status()).toBeGreaterThanOrEqual(400);
      expect(refusal.ok).toBe(false);
      // The send's FIRST gate depends on environment truth, and both outcomes are named refusals:
      // with SANDBOX_MODE on (the owner's live stack), the sandbox refuses before anything else; with
      // it off (CI's seed), the unset sender identity refuses. Never a success, never an unnamed
      // failure — which gate fires first is the environment's business, so both are accepted by name.
      expect(["E_SANDBOX_BLOCKED", "E_CONFIG"]).toContain(refusal.error.code);
      if (refusal.error.code === "E_SANDBOX_BLOCKED") {
        expect(refusal.error.message).toMatch(/sandbox/i);
      } else {
        expect(refusal.error.message).toMatch(/sender identity/i);
      }

      // The refusal left the row a labelled draft.
      const after = await request.get(`${API_BASE}/api/v1/invoices/${draft.id}`);
      expect(after.status()).toBe(200);
      const afterBody = (await after.json()) as InvoiceRow;
      console.log("[invoices-spec] after refused send:", JSON.stringify(afterBody));
      expect(afterBody.status).toBe("draft");
    } finally {
      // Reversible cleanup — void the fixture row whatever happened above, and prove the void landed.
      const voided = await request.post(`${API_BASE}/api/v1/invoices/${draft.id}/void`, {
        data: { reason: `${PREFIX} lab cleanup` },
      });
      expect(voided.status()).toBe(200);
      const voidBody = (await voided.json()) as InvoiceRow;
      console.log("[invoices-spec] void:", JSON.stringify(voidBody));
      expect(voidBody.status).toBe("void");
    }
  });
});

/**
 * V20/L3 — the V19 FE contract debt, pinned in the browser: a TERMINAL invoice row (paid/void)
 * keeps Send and Void DISABLED and states the reason as visible row text — never a bare disabled
 * control, never an optimistic action.
 *
 * Reads only: the page performs its own `GET /invoices`; this spec issues no create/send/void. It
 * derives the terminal row from the LIVE list (the lab seeds five void rows) and, if the list
 * carries none, SKIPS BY NAME instead of inventing a row — the disabled-with-reason contract needs
 * a real terminal row to be meaningful.
 */
test.describe("Invoices (UI contract)", () => {
  test("terminal rows disable Send and Void and state why", async ({ page }) => {
    await page.goto("/invoices");
    await expect(
      page
        .getByTestId("invoices-list")
        .or(page.getByTestId("invoices-empty"))
        .or(page.getByTestId("invoices-read-error")),
    ).toBeVisible();

    const rows = page.locator('[data-testid^="invoice-row-"]');
    const total = await rows.count();
    let terminal: Locator | null = null;
    let terminalStatus = "";
    for (let i = 0; i < total; i += 1) {
      const row = rows.nth(i);
      const status = (await row.getByTestId("invoice-status").innerText()).trim().toLowerCase();
      if (status === "paid" || status === "void") {
        terminal = row;
        terminalStatus = status;
        break;
      }
    }
    test.skip(
      terminal === null,
      `no terminal (paid/void) invoice row in the live list (${total} rows) — ` +
        "the disabled-with-reason contract needs a real terminal row",
    );

    const row = terminal as Locator;
    await expect(row.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
    await expect(row.getByRole("button", { name: "Void", exact: true })).toBeDisabled();

    // The reason is VISIBLE row text, not only a tooltip.
    const reason = row.locator('[data-testid^="invoice-terminal-reason-"]');
    await expect(reason).toBeVisible();
    await expect(reason).toContainText("send and void are closed");
    await expect(reason).toContainText(`${terminalStatus} is a terminal state`);

    // …and the row still shows the server's own status, untouched by the controls.
    expect((await row.getByTestId("invoice-status").innerText()).trim().toLowerCase()).toBe(
      terminalStatus,
    );
  });
});
