// Diagnostic, NOT a test: `npx playwright test` only matches *.spec.ts, so this never runs in the gate.
// Purpose: when an e2e assertion fails, show what the browser ACTUALLY did — console errors, failed
// requests, and every /api/ response code — plus what the page rendered. Run it with:
//     cd e2e && node _diag-browser.cjs
// (It must live in this directory so `require("@playwright/test")` resolves against e2e/node_modules.)
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

  for (const path of process.argv.slice(2).length ? process.argv.slice(2) : ["/settings", "/agents"]) {
    console.log(`\n=========== ${path} ===========`);
    await page
      .goto(`${BASE}${path}`, { waitUntil: "networkidle" })
      .catch((e) => console.log("GOTO ERR", e.message.slice(0, 140)));
    await page.waitForTimeout(2000);
    const main = (await page.textContent("main").catch(() => null)) || "(no main)";
    console.log("MAIN TEXT:", JSON.stringify(main.replace(/\s+/g, " ").slice(0, 300)));
    console.log("BUTTONS:", await page.getByRole("button").allTextContents().catch(() => []));
    console.log("HEADINGS:", await page.getByRole("heading").allTextContents().catch(() => []));
  }
  await browser.close();
})();
