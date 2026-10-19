import { test, expect, type Page } from "@playwright/test";

/**
 * V20/L3 — the V19 codes panel, pinned end to end (it was jsdom-only until now).
 *
 * The panel is the ONLY surface where verification codes can appear, and every one of its laws is
 * observable in the browser, so each test pins one:
 *
 *   1. FETCH-ON-CLICK ONLY — mounting `/phone` must issue ZERO `GET /api/v1/phone/codes` requests;
 *      one click on "Read verification codes" must issue exactly ONE, with the panel's stated
 *      query (`limit=10&within_hours=24` by default).
 *   2. MASKED BY DEFAULT — rows render a fixed three-dot run plus the code's last two characters
 *      (`•••93`), and neither full code appears anywhere in the panel's text.
 *   3. REVEAL / COPY / ESC — Reveal shows the full code (`aria-pressed`), Escape re-masks it, Copy
 *      writes the FULL value while the row still shows the mask and re-masks immediately; a second
 *      Escape collapses the panel and clears every row.
 *   4. THE WINDOW SELECTOR CHANGES THE QUERY — 1 h sends `within_hours=1`; All sends `limit` only
 *      (the route's own "no window"). Changing it clears rows and fires no request by itself.
 *   5. NAMED REFUSAL — a coded API refusal renders inline with its code, message and fix hint; no
 *      row is fabricated. A store-unavailable summary keeps the page's one 503 block and never
 *      mounts the panel.
 *
 * Network discipline: every product read is a real GET. Only `GET /phone/codes` is intercepted —
 * it is the OWNER-AUDITED route (one `activity_log` row per call), so the spec asserts its shape
 * without writing audit rows into the live store, and the interception is how the request count and
 * query string are pinned. No POST is issued by this file.
 *
 * A refusal is a 422 `{detail:{code,message,how_to_fix}}` — exactly the shape FastAPI emits — rather
 * than a transport abort: "route-abort" produces `Failed to fetch`, which would prove the panel has
 * an error state but NOT that a NAMED refusal renders by code. The named-refusal contract is the
 * one with product meaning, so it is the one pinned here.
 */

test.use({ permissions: ["clipboard-read", "clipboard-write"] });

const CODES_GLOB = "**/api/v1/phone/codes**";

/** Two real-shaped rows: one timestamped, one with the wire's legal `when: null`. */
const CODES_BODY = {
  ok: true,
  data: {
    codes: [
      {
        when: "2026-10-18T20:20:17+00:00",
        from: "Bank",
        code: "148293",
        snippet: "Your verification code is 148293 — never share it.",
      },
      {
        when: null,
        from: "Acme",
        code: "997712",
        snippet: "Acme code 997712",
      },
    ],
    dropped: 0,
  },
};

/** The refusal shape `lib/api.ts` parses from FastAPI's `HTTPException(detail={code,…})`. */
const NAMED_REFUSAL = {
  detail: {
    code: "E_VALIDATION",
    message: "within_hours must be one of 1, 6, or 24",
    how_to_fix: "send one of the supported windows",
  },
};

/** Every `/phone/codes` request the page makes, in order. Attached before `goto`. */
function watchCodes(page: Page): string[] {
  const seen: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/v1/phone/codes")) seen.push(request.url());
  });
  return seen;
}

/** Fulfil the audited codes route; GET only, with the CORS header a cross-origin client needs. */
async function stubCodes(page: Page, body: unknown, status = 200): Promise<void> {
  await page.route(CODES_GLOB, async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(body),
    });
  });
}

/** Open `/phone` and wait for the page gate to mount the panel (summary `store_status: "ok"`). */
async function openPhone(page: Page): Promise<void> {
  await page.goto("/phone");
  await expect(page.getByTestId("phone-codes")).toBeVisible();
  await expect(page.getByTestId("phone-codes-read")).toBeEnabled();
}

test.describe("V19 codes panel (live FE)", () => {
  test("idle issues zero codes requests; one click = one request with limit=10&within_hours=24", async ({
    page,
  }) => {
    const seen = watchCodes(page);
    await stubCodes(page, CODES_BODY);
    await openPhone(page);

    // The panel states the fetch-on-click law…
    await expect(page.getByTestId("phone-codes-audit-note")).toContainText(
      "Nothing is read until you ask.",
    );
    await expect(page.getByTestId("phone-codes-limit-note")).toContainText("Up to the newest 10 codes");
    await expect(page.getByTestId("phone-codes-window-24")).toHaveAttribute("aria-pressed", "true");

    // …and the network log proves it: waiting past several paint ticks changes nothing.
    await page.waitForTimeout(1_000);
    expect(seen, "idle /phone must not read the audited codes route").toHaveLength(0);

    await page.getByTestId("phone-codes-read").click();
    await expect(page.getByTestId("phone-code-row")).toHaveCount(2);
    await page.waitForTimeout(300);
    expect(seen, "one click = one audit row = one request").toHaveLength(1);

    const url = new URL(seen[0]);
    expect(url.pathname).toBe("/api/v1/phone/codes");
    expect(url.searchParams.get("limit")).toBe("10");
    expect(url.searchParams.get("within_hours")).toBe("24");

    // Masked by default: fixed three dots + the last two characters, never the full value.
    const rows = page.getByTestId("phone-code-row");
    await expect(rows.nth(0).getByTestId("phone-code-value")).toHaveText("•••93");
    await expect(rows.nth(1).getByTestId("phone-code-value")).toHaveText("•••12");
    await expect(rows.nth(1)).toContainText("time unknown");

    const text = await page.getByTestId("phone-codes").innerText();
    expect(text).not.toContain("148293");
    expect(text).not.toContain("997712");
    // The snippet carries the code in clear text and must never be rendered beside the mask.
    expect(text).not.toContain("never share it");
  });

  test("Reveal shows the full code; ESC and Copy re-mask; Copy writes the full value", async ({
    page,
  }) => {
    const seen = watchCodes(page);
    await stubCodes(page, CODES_BODY);
    await openPhone(page);
    await page.getByTestId("phone-codes-read").click();
    await expect(page.getByTestId("phone-code-row")).toHaveCount(2);

    const first = page.getByTestId("phone-code-row").first();
    const value = first.getByTestId("phone-code-value");
    const reveal = first.getByTestId("phone-code-reveal");

    await expect(value).toHaveText("•••93");
    await reveal.click();
    await expect(value).toHaveText("148293");
    await expect(reveal).toHaveAttribute("aria-pressed", "true");
    await expect(reveal).toHaveAttribute("aria-label", "Hide code from Bank");

    // Escape belongs to the reveal first: mask it, keep the panel open.
    await page.keyboard.press("Escape");
    await expect(value).toHaveText("•••93");
    await expect(reveal).toHaveAttribute("aria-pressed", "false");

    // Copy is the FULL code even while the row shows the mask, and re-masks immediately.
    await reveal.click();
    await expect(value).toHaveText("148293");
    await first.getByTestId("phone-code-copy").click();
    await expect(value).toHaveText("•••93");
    await expect(first.getByTestId("phone-code-copied")).toBeVisible();
    const clipboard = await page.evaluate(() => navigator.clipboard.readText());
    expect(clipboard, "Copy must carry the full code, not the mask").toBe("148293");

    // With nothing revealed, Escape collapses the panel and clears every row.
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("phone-codes-collapsed")).toBeVisible();
    await expect(page.getByTestId("phone-code-row")).toHaveCount(0);

    // All of that was one read.
    expect(seen).toHaveLength(1);
  });

  test("the window selector changes the query: 1 h → within_hours=1, All → no window param", async ({
    page,
  }) => {
    const seen = watchCodes(page);
    await stubCodes(page, CODES_BODY);
    await openPhone(page);

    // Changing the window CLEARS the rows and fires nothing — the next read is explicit.
    await page.getByTestId("phone-codes-window-1").click();
    await expect(page.getByTestId("phone-codes-window-1")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("phone-code-row")).toHaveCount(0);
    expect(seen).toHaveLength(0);

    await page.getByTestId("phone-codes-read").click();
    await expect(page.getByTestId("phone-code-row")).toHaveCount(2);
    let url = new URL(seen.at(-1) as string);
    expect(url.searchParams.get("limit")).toBe("10");
    expect(url.searchParams.get("within_hours")).toBe("1");

    // "All" is the route's own no-window read: `limit` only.
    await page.getByTestId("phone-codes-window-all").click();
    await expect(page.getByTestId("phone-code-row")).toHaveCount(0);
    await page.getByTestId("phone-codes-read").click();
    await expect(page.getByTestId("phone-code-row")).toHaveCount(2);
    url = new URL(seen.at(-1) as string);
    expect(url.searchParams.get("limit")).toBe("10");
    expect(url.searchParams.has("within_hours")).toBe(false);
    expect(seen).toHaveLength(2);
  });

  test("a named refusal renders inline with its code, message and fix hint", async ({ page }) => {
    const seen = watchCodes(page);
    await stubCodes(page, NAMED_REFUSAL, 422);
    await openPhone(page);

    await page.getByTestId("phone-codes-read").click();
    const alert = page.getByTestId("phone-codes-error");
    await expect(alert).toBeVisible();
    await expect(alert).toContainText("Could not read codes:");
    await expect(alert).toContainText("E_VALIDATION");
    await expect(alert).toContainText("within_hours must be one of 1, 6, or 24");
    await expect(alert).toContainText("send one of the supported windows");
    // A refused read never renders rows (and never a silent empty state).
    await expect(page.getByTestId("phone-code-row")).toHaveCount(0);
    await expect(page.getByTestId("phone-codes-empty")).toHaveCount(0);
    expect(seen).toHaveLength(1);
  });

  test("store-unavailable summary keeps the page's 503 block and never mounts the codes panel", async ({
    page,
  }) => {
    const seen = watchCodes(page);
    await page.route("**/api/v1/phone/summary**", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        headers: { "access-control-allow-origin": "*" },
        body: JSON.stringify({
          detail: {
            code: "E_PHONE_UNAVAILABLE",
            message: "the phone-link store is not mounted",
            how_to_fix: "mount the store and retry",
          },
        }),
      });
    });

    await page.goto("/phone");
    await expect(page.getByTestId("phone-unavailable")).toBeVisible();
    await expect(page.getByTestId("phone-codes")).toHaveCount(0);
    expect(seen).toHaveLength(0);
  });
});
