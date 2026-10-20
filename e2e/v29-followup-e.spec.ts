import { test, expect, type Page } from "@playwright/test";

import { API_BASE, PREFIX } from "./_helpers";

/**
 * V29 follow-up E — LIVE integrated verification of the four V29 surfaces that a parser/module
 * edit can silently break:
 *
 *   1. the dashboard SIGNALS widget — its fetch (`GET /signals?limit=6`), the wire shape
 *      (`items`/`sources`/`note`/`untrusted`), the third-party-link safety attributes, and its
 *      honest empty / named-failure / Retry states;
 *   2. the SITES row trust badge — a RECORDED custom domain really asks `GET /trust/domain` and
 *      renders the server's verdict (`TLS <n>d` / `DNS FAIL` / `CHECK FAILED`), while a site with
 *      no domain keeps `LOCAL` and makes NO trust request;
 *   3. INVOICE currency display — a non-USD row shows its own code (`EUR 1,234.56`), a display-only
 *      `≈ $X` line at the backend's rate, and `≈ —` (never 1:1) when the rate read is refused;
 *   4. the CAPABILITY payload — exactly 17 registry tools, the five V29 additions included, each
 *      with a description, and the `/capabilities` page rendering that same 17-name set.
 *
 * READ-ONLY except one reversible fixture: the currency test creates a DRAFT EUR invoice and VOIDS
 * it in `finally` (the `invoices.spec.ts` pattern). All rows carry the suite's `[e2e-fixture]`
 * prefix so any residue is attributable and swept by the suite-end sweep.
 *
 * Console errors are part of every assertion: each test watches `console` (type "error") and
 * `pageerror`, and fails with the collected text. The two deliberately-failed reads (a 500 signals
 * answer and a refused FX rate) are allowed to produce the browser's own resource-load line; that
 * expected line is filtered by URL, nothing else is.
 */

type SignalItem = {
  id: string;
  source: string;
  title: string;
  url: string;
  published_at: string | null;
};

type SignalSource = {
  id: string;
  label: string;
  ok: boolean;
  reason: string | null;
  count: number;
  cached: boolean;
  fetched_at: string | null;
};

type SignalsPayload = {
  items: SignalItem[];
  sources: SignalSource[];
  fetched_at: string;
  note: string;
  untrusted: boolean;
};

type SiteRow = {
  slug: string;
  title: string;
  custom_domain?: string | null;
};

type InvoiceRow = {
  id: number;
  number: string;
  status: string;
  currency: string;
  total: number | null;
};

type DomainHealth = {
  host: string;
  dns: { status: number | null; reason: string | null };
  tls: { ok: boolean; days_left: number | null; not_after: string | null; reason: string | null };
  reason: string | null;
};

/** The V29 registry contract, pinned by name: 17 tools (12 pre-V29 + the five this wave added). */
const EXPECTED_TOOLS = [
  "code_list",
  "code_read",
  "code_write",
  "domain_health",
  "fx_convert",
  "github_branch",
  "github_commit",
  "github_pr",
  "github_repo",
  "link_status",
  "macro_snapshot",
  "phone_read",
  "pricing_catalog",
  "research_fetch",
  "signals_read",
  "web_fetch",
  "web_search",
];

const V29_TOOLS = ["research_fetch", "fx_convert", "signals_read", "domain_health", "link_status"];

/**
 * Collect real console errors and uncaught page errors for one page.
 *
 * `ignored` matches the deliberate failures this spec injects: Chromium emits its own
 * "Failed to load resource: … 500/503" line for a refused read, which is the expected consequence
 * of the error-state test, not an application error. Each pattern runs against `"<text> <url>"`,
 * so a filter must name BOTH the resource-load wording and the injected route; a genuine
 * console.error from the app still fails the test.
 */
function watchConsole(page: Page, ignored: RegExp[] = []): string[] {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const text = message.text();
    const url = message.location().url ?? "";
    const line = url ? `${text} [${url}]` : text;
    if (ignored.some((rx) => rx.test(line))) return;
    errors.push(line);
  });
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  return errors;
}

test.describe("V29 follow-up E — live integrated surfaces", () => {
  test("Signals widget: live payload shape, limit=6 fetch, safe links, named source failures", async ({
    page,
    request,
  }) => {
    const errors = watchConsole(page);
    const signalRequests: string[] = [];
    page.on("request", (req) => {
      if (req.url().includes("/api/v1/signals")) signalRequests.push(req.url());
    });

    // The live wire contract first — the widget renders THIS answer, not a fixture.
    const read = await request.get(`${API_BASE}/api/v1/signals?limit=6`);
    expect(read.status()).toBe(200);
    const body = (await read.json()) as SignalsPayload;

    expect(Array.isArray(body.items), "items must be an array").toBe(true);
    expect(Array.isArray(body.sources), "sources must be an array").toBe(true);
    expect(body.items.length).toBeLessThanOrEqual(6);
    expect(body.untrusted).toBe(true);
    expect(typeof body.note === "string" && body.note.length > 20).toBe(true);
    expect(Number.isFinite(Date.parse(body.fetched_at))).toBe(true);

    // The allowlist is fixed server-side: HN + TechCrunch only, and a failure carries its reason.
    expect(new Set(body.sources.map((source) => source.id))).toEqual(new Set(["hn", "tc"]));
    for (const source of body.sources) {
      expect(typeof source.label === "string" && source.label.length > 0).toBe(true);
      expect(typeof source.ok).toBe("boolean");
      expect(Number.isInteger(source.count) && source.count >= 0).toBe(true);
      expect(typeof source.cached).toBe("boolean");
      if (source.ok) {
        expect(source.reason, `${source.id}: an OK source must not carry a reason`).toBeNull();
      } else {
        expect(
          typeof source.reason === "string" && source.reason.startsWith("E_"),
          `${source.id}: a failed source must carry a NAMED reason`,
        ).toBe(true);
      }
    }
    for (const item of body.items) {
      expect(typeof item.id === "string" && item.id.length > 0).toBe(true);
      expect(typeof item.title === "string" && item.title.trim().length > 0).toBe(true);
      expect(item.url).toMatch(/^https?:\/\//);
      expect(item.published_at === null || Number.isFinite(Date.parse(item.published_at))).toBe(
        true,
      );
    }

    await page.goto("/dashboard");
    const widget = page.getByTestId("signals-widget");
    await expect(widget).toBeVisible();

    // Wait for the ANSWERED state (links, the honest empty, or a named failure) — never a blank card.
    await expect(
      widget
        .locator("a")
        .first()
        .or(widget.getByText(/No headlines right now/))
        .or(widget.getByRole("alert")),
    ).toBeVisible();
    await expect(widget.getByRole("alert")).toHaveCount(0);

    // The fetch this card owns: exactly the documented limit, not a default or an unbounded read.
    expect(
      signalRequests.some((url) => /\/api\/v1\/signals\?limit=6$/.test(url)),
      `expected a /signals?limit=6 request, saw: ${signalRequests.join(", ") || "none"}`,
    ).toBe(true);

    // A feed failure is named in the card; an all-OK read shows no failure list at all.
    const failed = body.sources.filter((source) => !source.ok);
    const failureList = widget.getByTestId("signals-source-failures");
    if (failed.length === 0) {
      await expect(failureList).toHaveCount(0);
    } else {
      await expect(failureList).toBeVisible();
      for (const source of failed) {
        await expect(failureList).toContainText(`${source.label} unavailable — ${source.reason}`);
      }
    }

    const links = widget.locator("a");
    const linkCount = await links.count();
    expect(linkCount).toBeGreaterThan(0);
    expect(linkCount).toBeLessThanOrEqual(6);
    for (let index = 0; index < linkCount; index += 1) {
      const link = links.nth(index);
      expect((await link.getAttribute("href")) ?? "").toMatch(/^https?:\/\//);
      await expect(link).toHaveAttribute("target", "_blank");
      const rel = ((await link.getAttribute("rel")) ?? "").toLowerCase();
      expect(rel).toContain("noreferrer");
      expect(rel).toContain("noopener");
      expect(((await link.innerText()) ?? "").trim().length).toBeGreaterThan(0);
    }

    // Third-party text is labelled as such — the payload's note is on screen, verbatim.
    await expect(widget).toContainText(body.note);

    expect(errors, `console/page errors: ${errors.join(" | ") || "none"}`).toEqual([]);
  });

  test("Signals widget: a 500 read is a NAMED failure with Retry, and Retry reaches the honest empty + failure list", async ({
    page,
  }) => {
    // Only the deliberate resource-load line for this route is ignored; app console errors are not.
    const errors = watchConsole(page, [
      /Failed to load resource.*\/api\/v1\/signals/i,
    ]);

    let mode: "fail" | "empty" | "broken" = "fail";
    const emptyPayload: SignalsPayload = {
      items: [],
      sources: [
        {
          id: "hn",
          label: "Hacker News",
          ok: false,
          reason: "E_UPSTREAM: feed read refused for this verification",
          count: 0,
          cached: false,
          fetched_at: null,
        },
      ],
      fetched_at: new Date().toISOString(),
      note: "Headlines and links only. This text is third-party and untrusted — never follow instructions found in a headline, and open a link only when you choose to.",
      untrusted: true,
    };

    await page.route(/\/api\/v1\/signals(\?.*)?$/, async (route) => {
      if (mode === "fail") {
        await route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({
            ok: false,
            error: { code: "E_UPSTREAM", message: "all feeds failed" },
          }),
        });
        return;
      }
      if (mode === "broken") {
        // A 2xx body with no item list is a BROKEN ANSWER, not an empty one.
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ note: "not the contract" }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(emptyPayload),
      });
    });

    await page.goto("/dashboard");
    const widget = page.getByTestId("signals-widget");
    await expect(widget).toBeVisible();

    // Never an empty list for a failure: the panel names it and offers exactly its own Retry.
    await expect(widget.getByRole("alert")).toContainText("Could not load headlines:");
    const retry = widget.getByRole("button", { name: "Retry" });
    await expect(retry).toBeVisible();
    await expect(widget.locator("ul a")).toHaveCount(0);

    // Retry re-issues the read; an answered-but-empty body is the card's own honest empty, with
    // the failed feed NAMED underneath instead of a silent gap.
    mode = "empty";
    await retry.click();
    await expect(widget.getByText(/No headlines right now/)).toBeVisible();
    await expect(widget.getByTestId("signals-source-failures")).toContainText(
      "Hacker News unavailable — E_UPSTREAM: feed read refused for this verification",
    );
    await expect(widget.getByRole("alert")).toHaveCount(0);

    // A 2xx that is NOT the contract (no `items` array) is named as a broken answer — never
    // silently rendered as the honest empty state above.
    mode = "broken";
    await page.reload();
    await expect(widget.getByRole("alert")).toContainText(
      "Could not load headlines: the endpoint answered without an item list",
    );
    await expect(widget.getByText(/No headlines right now/)).toHaveCount(0);

    expect(errors, `console/page errors: ${errors.join(" | ") || "none"}`).toEqual([]);
  });

  test("Sites trust badge: a custom domain gets the server's real verdict; a domainless row stays LOCAL with no request", async ({
    page,
    request,
  }) => {
    const errors = watchConsole(page);
    const trustRequests: string[] = [];
    page.on("request", (req) => {
      if (req.url().includes("/api/v1/trust/domain")) trustRequests.push(req.url());
    });

    const sitesRead = await request.get(`${API_BASE}/api/v1/sites`);
    expect(sitesRead.status()).toBe(200);
    const sites = (await sitesRead.json()) as SiteRow[];
    expect(Array.isArray(sites)).toBe(true);

    const withDomain = sites.find(
      (site) => typeof site.custom_domain === "string" && site.custom_domain.trim() !== "",
    );
    const withoutDomain = sites.find(
      (site) => !site.custom_domain || site.custom_domain.trim() === "",
    );
    test.skip(
      withDomain === undefined,
      `no site with a recorded custom_domain in the live list (${sites.length} rows) — the badge needs one`,
    );
    const domainRow = withDomain as SiteRow;
    const host = (domainRow.custom_domain as string).trim();

    // The server answer this badge must render — its own health read, not a fabricated claim.
    const healthRead = await request.get(
      `${API_BASE}/api/v1/trust/domain?host=${encodeURIComponent(host)}`,
    );
    expect(healthRead.status()).toBe(200);
    const health = (await healthRead.json()) as DomainHealth;

    let expectedState: "tls" | "dns" | "failed";
    let expectedLabel: string;
    let expectedTitle: string;
    if (health.dns.status !== null && health.dns.status !== 0) {
      expectedState = "dns";
      expectedLabel = "DNS FAIL";
      expectedTitle =
        health.dns.reason ?? health.reason ?? `DNS status ${health.dns.status}`;
    } else if (!health.tls.ok) {
      expectedState = "failed";
      expectedLabel = "CHECK FAILED";
      expectedTitle =
        health.tls.reason ?? health.reason ?? "the certificate check failed";
    } else if (health.tls.days_left === null) {
      expectedState = "failed";
      expectedLabel = "CHECK FAILED";
      expectedTitle = health.tls.reason ?? "the certificate expiry was not readable";
    } else if (health.tls.days_left < 0) {
      expectedState = "failed";
      expectedLabel = "TLS EXPIRED";
      expectedTitle = health.tls.not_after
        ? `The certificate expired on ${health.tls.not_after}`
        : "The certificate has expired";
    } else {
      expectedState = "tls";
      expectedLabel = `TLS ${health.tls.days_left}d`;
      expectedTitle = health.tls.not_after
        ? `Certificate expires ${health.tls.not_after}`
        : "The certificate is valid; its expiry was not returned";
    }

    await page.goto("/sites");
    await expect(
      page
        .getByTestId("sites-list")
        .or(page.getByTestId("sites-empty"))
        .or(page.getByTestId("sites-error")),
    ).toBeVisible();

    const row = page.getByTestId(`site-row-${domainRow.slug}`);
    await expect(row).toBeVisible();
    const badge = row.getByTestId("site-health-badge");
    await expect(badge).toBeVisible();
    await expect(badge).toHaveAttribute("data-host", host);
    await expect(badge).toHaveAttribute("data-state", expectedState);
    await expect(badge).toHaveText(expectedLabel);
    await expect(badge).toHaveAttribute("title", expectedTitle);

    // A LOCAL row has nothing to check: LOCAL is shown and the badge is absent (no invented check).
    if (withoutDomain !== undefined) {
      const localRow = page.getByTestId(`site-row-${withoutDomain.slug}`);
      await expect(localRow).toBeVisible();
      await expect(localRow.getByTestId(`site-local-${withoutDomain.slug}`)).toHaveText("LOCAL");
      await expect(localRow.getByTestId("site-health-badge")).toHaveCount(0);
    }

    // Every trust read names a non-empty host — a LOCAL row never fires `host=`.
    for (const url of trustRequests) {
      const queriedHost = new URL(url).searchParams.get("host");
      expect(
        queriedHost !== null && queriedHost.trim().length > 0,
        `trust read without a host: ${url}`,
      ).toBe(true);
    }

    expect(errors, `console/page errors: ${errors.join(" | ") || "none"}`).toEqual([]);
  });

  test("Sites trust badge: a TLS verdict and a malformed body both render honestly", async ({
    page,
    request,
  }) => {
    const errors = watchConsole(page);

    const sitesRead = await request.get(`${API_BASE}/api/v1/sites`);
    expect(sitesRead.status()).toBe(200);
    const sites = (await sitesRead.json()) as SiteRow[];
    const withDomain = sites.find(
      (site) => typeof site.custom_domain === "string" && site.custom_domain.trim() !== "",
    );
    test.skip(
      withDomain === undefined,
      `no site with a recorded custom_domain in the live list (${sites.length} rows) — the badge needs one`,
    );
    const domainRow = withDomain as SiteRow;
    const host = (domainRow.custom_domain as string).trim();

    // Route-pinned so BOTH parser branches are exercised on the real page without inventing a
    // second live domain: a healthy certificate, then a body that is not a health result at all.
    let mode: "tls" | "malformed" = "tls";
    const notAfter = "2026-12-25T22:56:35+00:00";
    await page.route(/\/api\/v1\/trust\/domain(\?.*)?$/, async (route) => {
      if (mode === "malformed") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ host }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          host,
          dns: { a: ["104.20.23.154"], aaaa: [], status: 0, reason: null },
          tls: { ok: true, not_after: notAfter, days_left: 66, reason: null },
          reason: null,
          checked_at: new Date().toISOString(),
          cached: false,
        }),
      });
    });

    await page.goto("/sites");
    const row = page.getByTestId(`site-row-${domainRow.slug}`);
    await expect(row).toBeVisible();
    const badge = row.getByTestId("site-health-badge");

    await expect(badge).toHaveAttribute("data-state", "tls");
    await expect(badge).toHaveText("TLS 66d");
    await expect(badge).toHaveAttribute("title", `Certificate expires ${notAfter}`);

    // A 2xx body without `dns`/`tls` sections is CHECK FAILED — never a fabricated healthy badge.
    mode = "malformed";
    await page.reload();
    const reloaded = page.getByTestId(`site-row-${domainRow.slug}`).getByTestId("site-health-badge");
    await expect(reloaded).toHaveAttribute("data-state", "failed");
    await expect(reloaded).toHaveText("CHECK FAILED");
    await expect(reloaded).toHaveAttribute(
      "title",
      "the server returned no health result for this domain",
    );

    expect(errors, `console/page errors: ${errors.join(" | ") || "none"}`).toEqual([]);
  });

  test("Invoice currency: EUR keeps its own code, converts display-only at the backend rate, and shows ≈ — when the rate is refused", async ({
    page,
    request,
  }) => {
    const errors = watchConsole(page, [
      /Failed to load resource.*\/api\/v1\/economics\/convert/i,
    ]);
    const fixture = {
      bill_to: { name: `${PREFIX} v29 currency`, email: "e2e-fixture@example.invalid" },
      line_items: [
        { description: `${PREFIX} EUR line item`, amount: 1234.56, quantity: 1 },
      ],
      currency: "EUR",
      subtotal: 1234.56,
      tax: 0.0,
      total: 1234.56,
    };

    const createdResponse = await request.post(`${API_BASE}/api/v1/invoices`, { data: fixture });
    expect(createdResponse.status()).toBe(201);
    const created = (await createdResponse.json()) as InvoiceRow;
    expect(created.status).toBe("draft");
    expect(created.currency).toBe("EUR");
    expect(created.total).toBe(1234.56);

    try {
      // The same rate the page will read; the UI's ≈ line must be this arithmetic, not a guess.
      const quoteRead = await request.get(
        `${API_BASE}/api/v1/economics/convert?amount=1&from=EUR&to=USD`,
      );
      expect(quoteRead.status()).toBe(200);
      const quote = (await quoteRead.json()) as { rate: number; date: string | null };
      expect(quote.rate).toBeGreaterThan(0);
      const usdValue = (created.total as number) * quote.rate;
      const expectedUsd = `≈ $${usdValue.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`;

      await page.goto("/invoices");
      await expect(
        page
          .getByTestId("invoices-list")
          .or(page.getByTestId("invoices-empty"))
          .or(page.getByTestId("invoices-read-error")),
      ).toBeVisible();

      const row = page.getByTestId(`invoice-row-${created.id}`);
      await expect(row).toBeVisible();

      // The stored total is stated in ITS currency — never a dollar sign on a EUR invoice.
      const total = row.getByTestId("invoice-total");
      await expect(total).toContainText("EUR 1,234.56");
      expect((await total.innerText()).trim().startsWith("$")).toBe(false);

      // …plus the display-only conversion, with the rate, its market date and the no-rewrite note.
      const converted = row.getByTestId("invoice-converted");
      await expect(converted).toBeVisible();
      await expect(converted).toHaveText(expectedUsd);
      const title = (await converted.getAttribute("title")) ?? "";
      expect(title).toContain(`1 EUR = ${quote.rate} USD`);
      if (quote.date) expect(title).toContain(`ECB reference rate dated ${quote.date}`);
      expect(title).toContain("display only; the stored EUR total is unchanged");

      // Line items are formatted in the row's currency too (V29 closed the `$` there as well).
      await expect(row.getByTestId(`invoice-items-list-${created.id}`)).toContainText(
        "EUR 1,234.56",
      );

      // A USD row in the same list keeps the pre-V29 dollar shape and gets no conversion line.
      const listRead = await request.get(`${API_BASE}/api/v1/invoices`);
      expect(listRead.status()).toBe(200);
      const liveRows = (await listRead.json()) as InvoiceRow[];
      const usdRow = liveRows.find(
        (candidate) => String(candidate.currency).toUpperCase() === "USD" && candidate.id !== created.id,
      );
      if (usdRow !== undefined) {
        const usdLocator = page.getByTestId(`invoice-row-${usdRow.id}`);
        await expect(usdLocator).toBeVisible();
        await expect(usdLocator.getByTestId("invoice-total")).toContainText("$");
        await expect(usdLocator.getByTestId("invoice-converted")).toHaveCount(0);
      }

      // A REFUSED rate is not 1:1: the row still states EUR and the conversion becomes the dash.
      await page.route(/\/api\/v1\/economics\/convert(\?.*)?$/, async (route) => {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({
            ok: false,
            error: { code: "E_UPSTREAM", message: "rate read refused for this verification" },
          }),
        });
      });
      await page.reload();
      const reloadedRow = page.getByTestId(`invoice-row-${created.id}`);
      await expect(reloadedRow).toBeVisible();
      await expect(reloadedRow.getByTestId("invoice-total")).toContainText("EUR 1,234.56");
      const refused = reloadedRow.getByTestId("invoice-converted");
      await expect(refused).toHaveText("≈ —");
      await expect(refused).toHaveAttribute(
        "title",
        /no USD rate could be read for EUR — the stored EUR total is unchanged/,
      );
    } finally {
      // Reversible cleanup — void the tagged draft whatever happened above, and prove it landed.
      const voided = await request.post(`${API_BASE}/api/v1/invoices/${created.id}/void`, {
        data: { reason: `${PREFIX} v29 followup-e cleanup` },
      });
      expect(voided.status()).toBe(200);
      const voidBody = (await voided.json()) as InvoiceRow;
      expect(voidBody.status).toBe("void");
    }

    expect(errors, `console/page errors: ${errors.join(" | ") || "none"}`).toEqual([]);
  });

  test("Capabilities: the payload is exactly 17 tools (five V29 additions) and the page renders that same set", async ({
    page,
    request,
  }) => {
    const errors = watchConsole(page);

    const read = await request.get(`${API_BASE}/api/v1/ops/capabilities`);
    expect(read.status()).toBe(200);
    const body = (await read.json()) as {
      agents: { agent_type: string; tools: { name: string; description: string }[] }[];
      registry_tools: { name: string; description: string }[];
    };

    expect(Array.isArray(body.registry_tools), "registry_tools must be an array").toBe(true);
    const names = body.registry_tools.map((tool) => tool.name);
    expect(names, "the V29 capability payload is a 17-tool contract").toHaveLength(17);
    expect(new Set(names).size, "registry tool names must be unique").toBe(17);
    expect([...names].sort()).toEqual(EXPECTED_TOOLS);
    for (const tool of V29_TOOLS) expect(names).toContain(tool);

    for (const tool of body.registry_tools) {
      expect(typeof tool.description, `${tool.name}: description must be a string`).toBe("string");
      expect(tool.description.trim().length, `${tool.name}: description must not be empty`).toBeGreaterThan(
        0,
      );
    }

    // The per-agent half of the payload may only name tools the registry actually holds.
    expect(body.agents.length).toBeGreaterThan(0);
    for (const agent of body.agents) {
      for (const tool of agent.tools) {
        expect(names, `${agent.agent_type}: unknown tool ${tool.name}`).toContain(tool.name);
      }
    }

    await page.goto("/capabilities");
    const count = page.getByTestId("capabilities-count");
    await expect(count).toBeVisible();
    await expect(count).toContainText("17 registry tools");
    await expect(page.getByTestId("capabilities-error")).toHaveCount(0);

    const registry = page.getByTestId("capabilities-registry");
    await expect(registry).toBeVisible();
    const rendered = await registry.locator("li code").allInnerTexts();
    expect(rendered).toHaveLength(17);
    expect([...rendered].sort()).toEqual(EXPECTED_TOOLS);
    for (const tool of V29_TOOLS) await expect(registry.getByText(tool, { exact: true })).toBeVisible();

    expect(errors, `console/page errors: ${errors.join(" | ") || "none"}`).toEqual([]);
  });
});
