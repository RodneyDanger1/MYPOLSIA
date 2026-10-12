// Diagnostic, NOT a test: `npx playwright test` only matches *.spec.ts, so this never runs in the gate.
// Purpose: when an e2e assertion fails, show what the browser ACTUALLY did — console errors, failed
// requests, and every /api/ response code — plus what the page rendered. Run it with:
//     cd e2e && node _diag-browser.cjs                          # vs BASE_URL (default http://localhost)
//     cd e2e && BASE_URL=http://localhost:3002 node _diag-browser.cjs   # vs another frontend
//     cd e2e && node _diag-browser.cjs /tasks /memory           # specific pages only
// (It must live in this directory so `require("@playwright/test")` resolves against e2e/node_modules.)
//
// The default list covers ALL NINE routes, including the six data pages the specs cover
// (/tasks /social /outreach /ads /finance /memory) — when one of those specs fails, the fastest read
// of "what the browser really got" is this script on that page.
const { chromium } = require("@playwright/test");

const BASE = process.env.BASE_URL || "http://localhost";

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  page.on("console", (m) => console.log(`CONSOLE[${m.type()}] ${m.text().slice(0, 200)}`));
  page.on("pageerror", (e) => console.log("PAGEERROR", String(e).slice(0, 200)));
  page.on("requestfailed", (r) => console.log("REQUESTFAILED", r.url(), r.failure()?.errorText));
  page.on("response", (r) => {
    if (r.url().includes("/api/")) console.log("API", r.status(), r.url().replace(BASE, ""));
  });

  const defaultPages = [
    "/dashboard",
    "/agents",
    "/tasks",
    "/social",
    "/outreach",
    "/ads",
    "/finance",
    "/memory",
    "/settings",
  ];
  for (const path of process.argv.slice(2).length ? process.argv.slice(2) : defaultPages) {
    console.log(`\n=========== ${path} ===========`);
    await page
      .goto(`${BASE}${path}`, { waitUntil: "networkidle" })
      .catch((e) => console.log("GOTO ERR", e.message.slice(0, 140)));
    await page.waitForTimeout(2000);
    const main = (await page.textContent("main").catch(() => null)) || "(no main)";
    console.log("MAIN TEXT:", JSON.stringify(main.replace(/\s+/g, " ").slice(0, 300)));
    console.log("BUTTONS:", await page.getByRole("button").allTextContents().catch(() => []));
    console.log("HEADINGS:", await page.getByRole("heading").allTextContents().catch(() => []));
    console.log(
      "INPUTS:",
      await page
        .getByRole("textbox")
        .evaluateAll((els) => els.map((el) => ({ placeholder: el.getAttribute("placeholder"), value: el.value })))
        .catch(() => []),
    );
    // Data-page shape: how many cards/rows the DOM actually holds (a frozen build shows 0 forever).
    console.log("CARDS main div.bg-gray-800:", await page.locator("main div.bg-gray-800").count().catch(() => -1));
    console.log("TABLE ROWS tbody tr:", await page.locator("tbody tr").count().catch(() => -1));
  }
  await browser.close();
})();
