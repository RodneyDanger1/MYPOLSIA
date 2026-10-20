import { test, expect } from "@playwright/test";

/**
 * V28 / L4 — the owner's prospect CRM (`/prospects`, V27/L5) through the served stack.
 *
 * The page is the owner's write surface over `prospects`; before V27 the table was read-only from
 * the UI. Every assertion below is derived from the SERVER's answer:
 *
 *   POST   /api/v1/prospects        -> 201 the stored row (email trimmed + lowercased)
 *   GET    /api/v1/prospects?search= -> {items, total}  (the page's own read)
 *   PATCH  /api/v1/prospects/{id}   -> 200 the stored row
 *   DELETE /api/v1/prospects/{id}   -> 204
 *   unknown id                      -> 404 E_NOT_FOUND
 *   duplicate address               -> 422 E_VALIDATION, context.field = "email"
 *
 * FRESH-DB SAFETY.  Nothing here assumes the table is empty: the count/empty assertions are scoped
 * to one uniquely-named fixture (or to a search that cannot match), and every expectation is
 * recomputed from the API response in the same test.  The fixture is `e2e-fixture-`-prefixed on
 * purpose — that is the predicate `e2e/_sweep-fixtures.py` already sweeps if a run dies before its
 * own cleanup.
 */

type ProspectRow = {
  id: number;
  email: string;
  first_name: string | null;
  last_name: string | null;
  company: string | null;
  title: string | null;
  source: string | null;
  status: string | null;
  created_at: string | null;
};

type ProspectPage = { items: ProspectRow[]; total: number };

/** A per-run, per-test address that cannot collide with a previous run or another lane. */
function fixtureEmail(tag: string): string {
  const stamp = `${Date.now()}-${Math.floor(Math.random() * 1_000_000)}`;
  return `e2e-fixture-v28-${tag}-${stamp}@example.com`;
}

test.describe("V28/L4 — prospect CRM (/prospects)", () => {
  test("creates, searches, refuses a duplicate 422, PATCHes and deletes a prospect", async ({
    page,
    request,
  }) => {
    const email = fixtureEmail("crud");
    const token = email.split("@")[0]!;
    const company = `V28 E2E Co ${Date.now()}`;
    let createdId: number | null = null;
    let deletedId: number | null = null;

    try {
      // What the read says BEFORE any fixture exists — the page's empty copy is only asserted
      // when the server declares an empty table (a fresh CI DB), never on a populated lab.
      const before = await request.get("/api/v1/prospects?limit=1");
      expect(before.status(), await before.text()).toBe(200);
      const beforeBody = (await before.json()) as ProspectPage;

      await page.goto("/prospects");
      await expect(page.getByRole("heading", { name: "Prospects" })).toBeVisible();

      if (beforeBody.total === 0) {
        await expect(page.getByTestId("prospects-empty")).toContainText(
          "No prospects yet. Add the first one below",
        );
      }

      // ── create through the real form ────────────────────────────────────────────────────────
      // The typed address is deliberately MIXED CASE with padding: the page must paint the
      // SERVER's normalised address, not the draft.
      await page.locator("#prospect-create-email").fill(`  ${email.toUpperCase()}  `);
      await page.locator("#prospect-create-first-name").fill("Ada");
      await page.locator("#prospect-create-last-name").fill("Lovelace");
      await page.locator("#prospect-create-company").fill(company);

      const [createdResponse] = await Promise.all([
        page.waitForResponse(
          (response) =>
            response.request().method() === "POST" &&
            new URL(response.url()).pathname === "/api/v1/prospects",
        ),
        page.getByRole("button", { name: "Create prospect" }).click(),
      ]);
      expect(createdResponse.status(), await createdResponse.text()).toBe(201);
      const created = (await createdResponse.json()) as ProspectRow;
      createdId = created.id;
      expect(created.email, "the server stores the address trimmed + lowercased").toBe(email);
      expect(created.first_name).toBe("Ada");
      expect(created.status).toBe("new");

      const row = page.getByTestId(`prospect-row-${created.id}`);
      await expect(row).toBeVisible();
      await expect(row.getByTestId("prospect-email")).toHaveText(email);
      await expect(row.getByTestId("prospect-name")).toHaveText("Ada Lovelace");
      await expect(row.getByTestId("prospect-company")).toHaveText(company);
      await expect(row.getByTestId("prospect-status")).toHaveText("new");

      // ── search ──────────────────────────────────────────────────────────────────────────────
      await page.locator("#prospects-search").fill(token);
      const [searchResponse] = await Promise.all([
        page.waitForResponse(
          (response) =>
            response.request().method() === "GET" &&
            new URL(response.url()).pathname === "/api/v1/prospects" &&
            response.url().includes("search="),
        ),
        page.getByRole("button", { name: "Search" }).click(),
      ]);
      expect(searchResponse.status()).toBe(200);
      const searched = (await searchResponse.json()) as ProspectPage;
      expect(searched.total, "the unique token must select exactly the fixture").toBe(1);
      expect(searched.items).toHaveLength(1);
      expect(searched.items[0]!.id).toBe(created.id);
      await expect(page.getByTestId("prospects-count")).toHaveText("1 prospect");
      await expect(page.getByTestId("prospects-list").locator("li")).toHaveCount(1);

      // ── duplicate: the named 422 refusal on the wire ────────────────────────────────────────
      const duplicate = await request.post("/api/v1/prospects", {
        data: { email: email.toUpperCase() },
      });
      expect(duplicate.status(), await duplicate.text()).toBe(422);
      expect(duplicate.headers()["x-polsia-error-code"]).toBe("E_VALIDATION");
      const duplicateBody = (await duplicate.json()) as {
        error: { code: string; message: string; context?: { field?: string } };
      };
      expect(duplicateBody.error.code).toBe("E_VALIDATION");
      expect(duplicateBody.error.context?.field).toBe("email");
      expect(duplicateBody.error.message).toContain("already exists");

      // ── duplicate: the form renders the server's refusal and adds nothing ───────────────────
      const rowsBeforeDuplicate = await page.getByTestId("prospects-list").locator("li").count();
      await page.locator("#prospect-create-email").fill(email.toUpperCase());
      await page.getByRole("button", { name: "Create prospect" }).click();
      await expect(page.getByTestId("prospects-action-error")).toContainText("already exists");
      await expect(page.getByTestId("prospects-list").locator("li")).toHaveCount(
        rowsBeforeDuplicate,
      );
      await expect(page.getByTestId("prospects-count")).toHaveText("1 prospect");

      // ── PATCH: the row's own status control, then the stored value read back ────────────────
      const [patchResponse] = await Promise.all([
        page.waitForResponse(
          (response) =>
            response.request().method() === "PATCH" &&
            new URL(response.url()).pathname === `/api/v1/prospects/${created.id}`,
        ),
        row.getByTestId("prospect-status-select").selectOption("contacted"),
      ]);
      expect(patchResponse.status(), await patchResponse.text()).toBe(200);
      const patched = (await patchResponse.json()) as ProspectRow;
      expect(patched.status).toBe("contacted");
      await expect(row.getByTestId("prospect-status")).toHaveText("contacted");

      const readBack = await request.get(`/api/v1/prospects/${created.id}`);
      expect(readBack.status()).toBe(200);
      expect(((await readBack.json()) as ProspectRow).status).toBe("contacted");

      // ── DELETE is TWO steps: the first press only arms the confirm block ────────────────────
      await row.getByRole("button", { name: "Delete" }).click();
      const confirmDelete = row.getByTestId("prospect-delete-confirm");
      await expect(confirmDelete).toBeVisible();
      expect(
        (await request.get(`/api/v1/prospects/${created.id}`)).status(),
        "opening the confirm block must not delete the row",
      ).toBe(200);

      deletedId = created.id;
      const [deleteResponse] = await Promise.all([
        page.waitForResponse(
          (response) =>
            response.request().method() === "DELETE" &&
            new URL(response.url()).pathname === `/api/v1/prospects/${created.id}`,
        ),
        confirmDelete.click(),
      ]);
      expect(deleteResponse.status()).toBe(204);
      await expect(row).toHaveCount(0);
      await expect(page.getByTestId("prospects-count")).toHaveText("0 prospects");
      await expect(page.getByTestId("prospects-empty")).toContainText(
        "No prospects match this search or status filter.",
      );
      createdId = null;

      // ── 404: a deleted id is honest on read, write and a second delete ──────────────────────
      const gone = await request.get(`/api/v1/prospects/${deletedId}`);
      expect(gone.status()).toBe(404);
      expect(gone.headers()["x-polsia-error-code"]).toBe("E_NOT_FOUND");
      expect((await gone.json()).error.code).toBe("E_NOT_FOUND");

      const secondDelete = await request.delete(`/api/v1/prospects/${deletedId}`);
      expect(secondDelete.status()).toBe(404);
      expect(secondDelete.headers()["x-polsia-error-code"]).toBe("E_NOT_FOUND");

      const patchGone = await request.patch(`/api/v1/prospects/${deletedId}`, {
        data: { status: "contacted" },
      });
      expect(patchGone.status()).toBe(404);
    } finally {
      if (createdId !== null) {
        await request.delete(`/api/v1/prospects/${createdId}`).catch(() => undefined);
      }
    }
  });

  test("a filtered read that matches nothing says it is filtered, and offers the add form", async ({
    page,
    request,
  }) => {
    const api = await request.get("/api/v1/prospects?limit=1");
    expect(api.status(), await api.text()).toBe(200);
    const body = (await api.json()) as ProspectPage;

    await page.goto("/prospects");
    await expect(page.getByRole("heading", { name: "Prospects" })).toBeVisible();
    await expect(page.getByTestId("prospects-count")).toBeVisible();

    if (body.total === 0) {
      // The fresh-DB sentence: an empty table is NOT an error and NOT a void — it is one
      // honest sentence plus the one control that can change it.
      await expect(page.getByTestId("prospects-empty")).toHaveCount(1);
      await expect(page.getByTestId("prospects-empty")).toContainText(
        "No prospects yet. Add the first one below",
      );
    }

    // A term that cannot exist: the FILTERED empty copy must not claim the table is empty.
    const missing = `no-such-prospect-${Date.now()}-${Math.floor(Math.random() * 1_000_000)}`;
    await page.locator("#prospects-search").fill(missing);
    const [response] = await Promise.all([
      page.waitForResponse(
        (candidate) =>
          candidate.request().method() === "GET" &&
          new URL(candidate.url()).pathname === "/api/v1/prospects" &&
          candidate.url().includes(`search=${missing}`),
      ),
      page.getByRole("button", { name: "Search" }).click(),
    ]);
    expect(response.status()).toBe(200);
    const filtered = (await response.json()) as ProspectPage;
    expect(filtered.total).toBe(0);
    expect(filtered.items).toEqual([]);

    await expect(page.getByTestId("prospects-empty")).toHaveCount(1);
    await expect(page.getByTestId("prospects-empty")).toContainText(
      "No prospects match this search or status filter.",
    );
    await expect(page.getByTestId("prospects-count")).toHaveText("0 prospects");
    // An empty result never hides the add form — it is the only control that can change it.
    await expect(page.getByTestId("prospect-create")).toBeVisible();
    await expect(page.getByTestId("prospects-read-error")).toHaveCount(0);
  });
});
