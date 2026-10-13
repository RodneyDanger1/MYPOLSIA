// wait-fixed.cjs — readiness poll for a (re)built standalone frontend behind the stack proxy.
// NOT a test (the runner only matches *.spec.ts).  Moved into e2e/ from the G-e2e campaign lane
// (A-H11), where it polled through the owner's rebuild window (wait-fixed.log: try 39 -> READY).
//
// READY means the browser's own view of /tasks shows LIVE data:
//   * at least one task row is rendered, AND
//   * the PAGE ITSELF fetched /api/v1/tasks successfully (a 200 response was observed for the page's
//     own request), AND
//   * no request of the page returned 5xx.
// Why the API-read condition: the deployed standalone build the G-e2e campaign tested served every
// data page as a frozen build-time snapshot — a 200 with baked-in empty states and NO API call at
// all.  A status-200 on /tasks is therefore NOT a readiness signal; the page's own successful API
// read is.  (The campaign-era version additionally required >=5 rows and one of two specific task
// titles — that campaign state is gone; the count floor is 1 and the title literals are deliberately
// dropped, per A-H11's "maintained tool" instruction.  Consequence, stated: on a truly empty DB a
// live server cannot be told from a frozen build by rows — the API-read signal is what separates
// them, and with zero rows this poll stays NOT READY.)
//
// Run it through wait-fixed.sh (node spawned by bash, stdin redirected — a direct `node ...` in a
// background session dies with "stdin is not a tty" on this host):
//     BASE_URL=http://localhost bash wait-fixed.sh
// Env: BASE_URL    frontend under test (default http://localhost)
//      WAIT_PATH   path polled (default /tasks)
//      TRIES       attempts before giving up (default 70)
//      INTERVAL_MS sleep between attempts (default 8000)
//      WAIT_URL    full URL override for BASE_URL+WAIT_PATH (used by the lane's self-checks)
// Exit: 0 = READY (printed with the evidence), 1 = not ready after TRIES attempts.
const { chromium } = require("@playwright/test");

const BASE = process.env.BASE_URL || "http://localhost";
const TARGET = process.env.WAIT_URL || BASE + (process.env.WAIT_PATH || "/tasks");
const TRIES = Number(process.env.TRIES || 70);
const INTERVAL_MS = Number(process.env.INTERVAL_MS || 8000);

(async () => {
  const browser = await chromium.launch();
  try {
    for (let i = 1; i <= TRIES; i++) {
      const page = await browser.newPage();
      const bad = [];
      let apiOk = false;
      page.on("response", (r) => {
        if (r.status() >= 500) bad.push(`${r.status()} ${r.url().replace(BASE, "")}`);
        if (r.url().includes("/api/v1/tasks") && r.status() === 200) apiOk = true;
      });
      try {
        await page.goto(TARGET, { waitUntil: "networkidle", timeout: 20000 });
      } catch {
        /* goto may fail mid-swap; treated as not-ready */
      }
      const rows = await page.locator("main div.bg-gray-800").count().catch(() => -1);
      console.log(`try ${i}: rows=${rows} api=${apiOk ? "ok" : "no"} 5xx=[${bad.slice(0, 3).join(", ")}]`);
      await page.close();
      if (rows >= 1 && apiOk && bad.length === 0) {
        console.log("READY: serves live task rows (the page fetched /api/v1/tasks itself)");
        process.exit(0);
      }
      await new Promise((r) => setTimeout(r, INTERVAL_MS));
    }
  } finally {
    await browser.close();
  }
  console.log(`NOT READY after ${TRIES} tries`);
  process.exit(1);
})();
