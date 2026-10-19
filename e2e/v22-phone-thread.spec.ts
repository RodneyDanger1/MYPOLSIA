import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * V22/L4 — the `/phone` THREAD CONVERSATION panel, pinned end to end in a real browser.
 *
 * The backend has served `GET /api/v1/phone/messages?thread_id=…` since H-P1; until V22/L2 the page
 * only rendered the thread LIST. `PhoneThreadView` is the missing conversation, and the laws worth
 * pinning are about WHEN it reads and what it claims:
 *
 *   1. CLICK-ONLY, ONE READ — mounting `/phone` issues NO thread-scoped messages read (the page's
 *      own recent-window read, `?limit=100` with no `thread_id`, is a separate pre-existing surface
 *      and is untouched); opening one thread issues exactly ONE read carrying THAT `thread_id`.
 *   2. THE STORE'S ROWS — the thread's messages render in the wire's newest-first order with
 *      `when`/`dir`/`who`/`text`; a legal `null when` renders as `time unknown` (never an invented
 *      instant). The window note states how many rows arrived.
 *   3. HONEST EMPTIES — a successful read with zero rows says "No messages in this thread." and
 *      renders no fabricated row; it is NOT the refusal state and NOT the unavailable state.
 *   4. A REFUSAL IS NAMED — a 503 `{ok:false,error:{…}}` renders the shared alert carrying
 *      `code: message — how_to_fix`; it never degrades into "no messages". Retry performs a real
 *      second read and the conversation recovers.
 *   5. STORE-UNAVAILABLE IS ITS OWN STATE — `E_PHONE_UNAVAILABLE` reuses the page's 503 block and
 *      is never shown as an empty thread.
 *   6. CLOSE RETURNS TO THE LIST — the panel unmounts, the list is back, and no conversation row
 *      survives; re-opening another thread starts from that thread's own answer.
 *
 * Environment independence: every phone read the page makes is stubbed at the route layer with
 * live-shaped payloads (`{ok:true,data:{…}}`; the messages row keys are exactly the live ones —
 * `when` is `str|null` by service contract). No store, config or lab DB is required, and only reads
 * are intercepted; this file issues NO POST.
 */

const SUMMARY_BODY = {
  ok: true,
  data: {
    store_status: "ok",
    unread_total: 2,
    threads: [
      {
        thread_id: 11,
        with: "Ada Lovelace",
        unread: 0,
        last_when: "2026-10-18T20:20:17+00:00",
        last_text: "See you at nine.",
      },
      {
        // A legal wire value (filetime 0/absent): the row states "time unknown" rather than inventing
        // an instant, and this spec pins that too.
        thread_id: 12,
        with: "Grace Hopper",
        unread: 2,
        last_when: null,
        last_text: "",
      },
      {
        thread_id: 13,
        with: "Katherine Johnson",
        unread: 0,
        last_when: "2026-10-17T08:00:00+00:00",
        last_text: "Confirmed.",
      },
      {
        thread_id: 14,
        with: "Mary Jackson",
        unread: 0,
        last_when: "2026-10-16T12:00:00+00:00",
        last_text: "On my way.",
      },
    ],
    missed_calls_today: [],
  },
};

/** The real read of one thread — newest first, exactly as the service answers. */
const ADA_ROWS = [
  {
    when: "2026-10-18T20:20:17+00:00",
    dir: "in",
    who: "Ada Lovelace",
    text: "See you at nine.",
    thread_id: 11,
  },
  {
    when: "2026-10-18T20:18:02+00:00",
    dir: "out",
    who: "me",
    text: "Nine works — I will bring the notes.",
    thread_id: 11,
  },
  {
    when: null,
    dir: "in",
    who: "Ada Lovelace",
    text: "Bring the printed tables too.",
    thread_id: 11,
  },
];

/** What thread 13 answers once the refusal is lifted by Retry. */
const KATHERINE_ROWS = [
  {
    when: "2026-10-17T08:00:00+00:00",
    dir: "in",
    who: "Katherine Johnson",
    text: "Confirmed for Thursday.",
    thread_id: 13,
  },
];

/** The refusal shape `lib/api.ts` parses (`ApiError.display` = `code: message — how_to_fix`). */
const REFUSAL_BODY = {
  ok: false,
  error: {
    code: "E_DB",
    message: "the phone store read failed",
    how_to_fix: "check DATABASE_URL and that PostgreSQL/Redis are reachable",
  },
};

/** The capability absence: the store is not mounted in this deployment. */
const UNAVAILABLE_BODY = {
  ok: false,
  error: {
    code: "E_PHONE_UNAVAILABLE",
    message: "the phone-link store is not available in this deployment",
    how_to_fix: "pair the phone in the automation Chrome and retry",
  },
};

type ThreadAnswer = "rows" | "empty" | "refuse" | "unavailable";

type PhoneState = {
  /** thread_id -> what that thread's read answers. Flipped mid-test to prove Retry re-reads. */
  answers: Record<number, ThreadAnswer>;
  /** Every `/phone/messages` URL the page requested, in order. */
  messageReads: string[];
  /** Every URL the page requested, in order (the mount law reads this). */
  allReads: string[];
};

function threadAnswer(threadId: number, state: PhoneState): ThreadAnswer {
  return state.answers[threadId] ?? "empty";
}

async function stubPhoneReads(page: Page, state: PhoneState): Promise<void> {
  await page.route("**/api/v1/phone/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    // A GET with X-API-Key is preflighted cross-origin; answer the preflight in kind.
    if (request.method() === "OPTIONS") {
      await route.fulfill({
        status: 204,
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-methods": "GET, OPTIONS",
          "access-control-allow-headers": "content-type, x-api-key",
        },
      });
      return;
    }
    if (request.method() !== "GET") {
      await route.continue();
      return;
    }

    state.allReads.push(request.url());

    const fulfill = (status: number, body: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        headers: { "access-control-allow-origin": "*" },
        body: JSON.stringify(body),
      });

    if (path === "/api/v1/phone/summary") {
      await fulfill(200, SUMMARY_BODY);
      return;
    }
    if (path === "/api/v1/phone/calls") {
      await fulfill(200, { ok: true, data: { calls: [] } });
      return;
    }
    if (path === "/api/v1/phone/media") {
      await fulfill(200, { ok: true, data: { parts: [], truncated: false } });
      return;
    }
    if (path === "/api/v1/phone/messages") {
      state.messageReads.push(request.url());
      const raw = url.searchParams.get("thread_id");
      if (raw === null) {
        // The page's own recent window (no thread): a plain successful empty read.
        await fulfill(200, { ok: true, data: { messages: [] } });
        return;
      }
      const threadId = Number(raw);
      const answer = threadAnswer(threadId, state);
      if (answer === "refuse") {
        await fulfill(503, REFUSAL_BODY);
        return;
      }
      if (answer === "unavailable") {
        await fulfill(503, UNAVAILABLE_BODY);
        return;
      }
      if (answer === "rows") {
        await fulfill(200, {
          ok: true,
          data: { messages: threadId === 11 ? ADA_ROWS : KATHERINE_ROWS },
        });
        return;
      }
      await fulfill(200, { ok: true, data: { messages: [] } });
      return;
    }

    // Anything else the phone page might read: a named 404 rather than reaching the live stack.
    await fulfill(404, {
      ok: false,
      error: { code: "E_NOT_FOUND", message: `no stub for ${path}`, how_to_fix: "" },
    });
  });
}

/** The row element for one named thread in the list. */
function threadRow(page: Page, name: string) {
  return page.getByTestId("phone-thread-row").filter({ hasText: name });
}

/** Open one thread through its row's `Open conversation` control. */
async function openThread(page: Page, name: string): Promise<void> {
  await threadRow(page, name).getByTestId("phone-thread-open").click();
  await expect(page.getByTestId("phone-thread-view")).toBeVisible();
}

async function openPhone(page: Page, state: PhoneState): Promise<void> {
  await stubPhoneReads(page, state);
  await page.goto("/phone");
  await expect(page.getByRole("heading", { name: "Phone" })).toBeVisible();
  await expect(page.getByTestId("phone-thread-row")).toHaveCount(4);
}

function newState(): PhoneState {
  return {
    answers: { 11: "rows", 12: "empty", 13: "refuse", 14: "unavailable" },
    messageReads: [],
    allReads: [],
  };
}

/** The thread-scoped reads for one id. */
function readsFor(state: PhoneState, threadId: number): string[] {
  return state.messageReads.filter(
    (raw) => new URL(raw).searchParams.get("thread_id") === String(threadId),
  );
}

test.describe("V22 phone thread view (live FE)", () => {
  test("mount issues NO thread read; one click = one read for that thread, rendered in wire order", async ({
    page,
  }) => {
    const state = newState();
    await openPhone(page, state);

    // 1. No conversation is mounted and NO read carries a thread id on mount.
    await expect(page.getByTestId("phone-thread-view")).toHaveCount(0);
    const threaded = state.messageReads.filter(
      (raw) => new URL(raw).searchParams.get("thread_id") !== null,
    );
    expect(threaded, "mounting /phone must not read any thread's messages").toHaveLength(0);
    // The list itself is the store's own summary.
    await expect(threadRow(page, "Ada Lovelace")).toContainText("See you at nine.");
    // A null last_when is stated as unknown, never as an invented time.
    await expect(threadRow(page, "Grace Hopper")).toContainText("time unknown");

    // 2. One click -> exactly ONE read, carrying THAT thread_id.
    await openThread(page, "Ada Lovelace");
    await expect(page.getByTestId("phone-thread-heading")).toHaveText("Thread #11 — Ada Lovelace");

    const adaReads = readsFor(state, 11);
    expect(adaReads, "opening a thread must issue exactly one read").toHaveLength(1);
    const read = new URL(adaReads[0]);
    expect(read.pathname).toBe("/api/v1/phone/messages");
    expect(read.searchParams.get("thread_id")).toBe("11");
    // The window the panel requests is the page's own documented window.
    expect(read.searchParams.get("limit")).toBe("100");
    // Opening one thread does not read another.
    expect(readsFor(state, 12)).toHaveLength(0);
    expect(readsFor(state, 13)).toHaveLength(0);

    // 3. The rows are the store's: newest first, fields verbatim, null `when` as "time unknown".
    const rows = page.getByTestId("phone-thread-message");
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toContainText("See you at nine.");
    await expect(rows.nth(1)).toContainText("Nine works — I will bring the notes.");
    await expect(rows.nth(2)).toContainText("Bring the printed tables too.");
    await expect(rows.nth(0)).toContainText("2026-10-18T20:20:17+00:00");
    await expect(rows.nth(0)).toContainText("in");
    await expect(rows.nth(0)).toContainText("Ada Lovelace");
    await expect(rows.nth(2), "a legal null when is stated, never invented").toContainText(
      "time unknown",
    );

    // 4. The window note states what arrived — not a claim about the whole store.
    await expect(page.getByTestId("phone-thread-window")).toContainText(
      "Showing the newest 3 messages",
    );

    // 5. No empty / refusal / unavailable copy stands next to real rows.
    await expect(page.getByTestId("phone-thread-empty")).toHaveCount(0);
    await expect(page.getByTestId("phone-thread-error")).toHaveCount(0);
    await expect(page.getByTestId("phone-thread-unavailable")).toHaveCount(0);
  });

  test("a successful empty thread says so, with no fabricated row and no window note", async ({
    page,
  }) => {
    const state = newState();
    await openPhone(page, state);
    await openThread(page, "Grace Hopper");

    await expect(page.getByTestId("phone-thread-heading")).toHaveText("Thread #12 — Grace Hopper");
    await expect(page.getByTestId("phone-thread-empty")).toHaveText("No messages in this thread.");
    await expect(page.getByTestId("phone-thread-message")).toHaveCount(0);
    await expect(page.getByTestId("phone-thread-window")).toHaveCount(0);
    await expect(page.getByTestId("phone-thread-error")).toHaveCount(0);
    await expect(page.getByTestId("phone-thread-unavailable")).toHaveCount(0);
    expect(readsFor(state, 12)).toHaveLength(1);
  });

  test("a 503 refusal is named inline, never 'no messages'; Retry re-reads and recovers", async ({
    page,
  }) => {
    const state = newState();
    await openPhone(page, state);
    await openThread(page, "Katherine Johnson");

    const alert = page.getByTestId("phone-thread-error");
    await expect(alert).toBeVisible();
    await expect(alert).toContainText("Could not read this thread");
    // The shared refusal vocabulary, verbatim: `code: message — how_to_fix`.
    await expect(alert).toContainText(
      "E_DB: the phone store read failed — check DATABASE_URL and that PostgreSQL/Redis are reachable",
    );
    // A failed read is NEVER an empty thread.
    await expect(page.getByTestId("phone-thread-empty")).toHaveCount(0);
    await expect(page.getByTestId("phone-thread-message")).toHaveCount(0);
    // The client's bounded retry ladder (2 retries on 5xx) means >1 request before the user retries.
    const before = readsFor(state, 13).length;
    expect(before).toBeGreaterThan(0);

    // Retry goes back through the wire, and the conversation recovers on the real payload.
    state.answers[13] = "rows";
    await page.getByTestId("phone-thread-retry").click();
    await expect(alert).toHaveCount(0);
    await expect(page.getByTestId("phone-thread-message")).toHaveCount(1);
    await expect(page.getByTestId("phone-thread-message")).toContainText("Confirmed for Thursday.");
    expect(readsFor(state, 13).length, "Retry must issue a fresh read").toBeGreaterThan(before);
  });

  test("E_PHONE_UNAVAILABLE reuses the 503 block and is not an empty thread", async ({ page }) => {
    const state = newState();
    await openPhone(page, state);
    await openThread(page, "Mary Jackson");

    const unavailable = page.getByTestId("phone-thread-unavailable");
    await expect(unavailable).toBeVisible();
    await expect(unavailable).toContainText("E_PHONE_UNAVAILABLE");
    await expect(unavailable).toContainText("pair the phone in the automation Chrome and retry");
    await expect(page.getByTestId("phone-thread-empty")).toHaveCount(0);
    await expect(page.getByTestId("phone-thread-message")).toHaveCount(0);
    await expect(page.getByTestId("phone-thread-error")).toHaveCount(0);
  });

  test("close returns to the list, drops every row, and the next thread starts from its own answer", async ({
    page,
  }) => {
    const state = newState();
    await openPhone(page, state);

    await openThread(page, "Ada Lovelace");
    await expect(page.getByTestId("phone-thread-message")).toHaveCount(3);

    await page.getByTestId("phone-thread-close").click();
    await expect(page.getByTestId("phone-thread-view")).toHaveCount(0);
    await expect(page.getByTestId("phone-thread-message")).toHaveCount(0);
    await expect(page.getByTestId("phone-threads")).toBeVisible();
    await expect(page.getByTestId("phone-thread-row")).toHaveCount(4);
    // Closing is not a read.
    expect(readsFor(state, 11)).toHaveLength(1);

    // Re-opening a DIFFERENT thread shows only that thread's answer — no stale conversation.
    await openThread(page, "Grace Hopper");
    await expect(page.getByTestId("phone-thread-empty")).toBeVisible();
    await expect(page.getByTestId("phone-thread-message")).toHaveCount(0);
    expect(readsFor(state, 11), "the closed thread is not re-read or re-shown").toHaveLength(1);
    expect(readsFor(state, 12)).toHaveLength(1);
  });
});
