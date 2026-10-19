/**
 * WAVE V22 / seat L2 — the `/phone` THREAD CONVERSATION view.
 *
 * The route has served `GET /api/v1/phone/messages?thread_id=…` since H-P1, but the page only ever
 * showed the thread LIST (and the `?thread=` deep link, a route change). This suite pins the missing
 * in-page view BEFORE it exists (RED leg: raw/01-red.txt — no `phone-thread-open` control, no panel).
 *
 * Wire truth consumed (read-only live GET against the running deployment, values withheld):
 *   {ok:true, data:{messages:[{when, dir, who, text, thread_id}]}} — newest first, `thread_id`
 *   filters exactly one thread. `when` is `str|null`: `filetime_to_local` answers None for a
 *   0/absent store timestamp, so a null is a LEGAL value (the V19 media fix), never a refusal.
 *
 * The laws pinned here:
 *   - CLICK-ONLY: mounting the page reads no thread. The conversation read fires on selection,
 *     with that thread's id, once per open (plus an explicit retry after a refusal).
 *   - NO FABRICATED ROWS: loading, empty ("No messages in this thread."), a NAMED refusal with its
 *     code + fix hint, and the page's own PHONE LINK UNAVAILABLE block for E_PHONE_UNAVAILABLE.
 *   - IN-MEMORY ONLY: no storage, no console; closing the panel drops every row it held, and a
 *     late answer after unmount renders nothing.
 *   - the wire's newest-first order and its own `when` wording render VERBATIM.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn() }, API_KEY: "test-key" };
});

jest.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

jest.mock("next/link", () => ({
  __esModule: true,
  default: function MockLink(props: { href: string; children: React.ReactNode } & Record<string, unknown>) {
    const { href, children, ...rest } = props;
    return (
      <a href={href} {...(rest as Record<string, string>)}>
        {children}
      </a>
    );
  },
}));

import { ApiError, api } from "@/lib/api";
import PhonePage from "@/app/phone/page";
import { messageWhenText, parseMessages } from "@/components/phone/phoneApi";

const mockApiGet = api.get as jest.Mock;
const ok = (data: unknown) => ({ ok: true, data });

const SUMMARY = {
  store_status: "ok",
  unread_total: 2,
  threads: [
    { thread_id: 11, with: "Ada Lovelace", unread: 2, last_when: "Today 14:02", last_text: "preview eleven" },
    { thread_id: 12, with: "Grace Hopper", unread: 0, last_when: "Yesterday 09:10", last_text: "preview twelve" },
  ],
  missed_calls_today: [],
};

/** The page's own (pre-existing) recent-window read — deliberately NOT thread-scoped. */
const RECENT = [
  { when: "Today 09:00", dir: "in", who: "Ada Lovelace", text: "recent window row", thread_id: 11 },
];

const THREAD_11 = [
  { when: "Today 14:02", dir: "in", who: "Ada Lovelace", text: "eleven newest", thread_id: 11 },
  { when: null, dir: "out", who: "me", text: "eleven older, no clock", thread_id: 11 },
];

const THREAD_12 = [
  { when: "Yesterday 09:10", dir: "in", who: "Grace Hopper", text: "twelve only", thread_id: 12 },
];

const THREAD_PATH_11 = "/phone/messages?limit=100&thread_id=11";

let threadAnswers: Record<number, unknown> = {};
let deferredThread: { promise: Promise<unknown>; resolve: (value: unknown) => void } | null = null;

function answer(value: unknown): Promise<unknown> {
  return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
}

/** The page's reads, plus one answer per thread id — any other path rejects loudly. */
function routePage() {
  mockApiGet.mockImplementation((path: string) => {
    if (path === "/phone/summary") return Promise.resolve(ok(SUMMARY));
    if (path === "/phone/messages?limit=100") return Promise.resolve(ok({ messages: RECENT }));
    if (path === "/phone/calls?limit=50") return Promise.resolve(ok({ calls: [] }));
    const match = /^\/phone\/messages\?limit=100&thread_id=(\d+)$/.exec(path);
    if (match) {
      const id = Number(match[1]);
      if (deferredThread) return deferredThread.promise;
      const value = threadAnswers[id];
      if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
      return answer(value);
    }
    return Promise.reject(new Error(`unexpected read: ${path}`));
  });
}

/** Every thread-scoped messages read the page made — the idle pin asserts this stays empty. */
const threadCalls = () =>
  mockApiGet.mock.calls
    .map((call) => String(call[0]))
    .filter((path) => path.includes("thread_id"));

beforeEach(() => {
  mockApiGet.mockReset();
  threadAnswers = {
    11: ok({ messages: THREAD_11 }),
    12: ok({ messages: THREAD_12 }),
  };
  deferredThread = null;
  routePage();
});

/** Click one thread row's action and hand back the panel that replaced the list. */
async function openConversation(name: string): Promise<HTMLElement> {
  const open = await screen.findByRole("button", { name: `Open conversation with ${name}` });
  fireEvent.click(open);
  return screen.getByTestId("phone-thread-view");
}

/* ── idle → select: the read happens on CLICK, never on mount ─────────────────────────────── */

describe("phone thread view — fetch on select only", () => {
  it("reads no thread on mount; clicking a row reads exactly that thread", async () => {
    render(<PhonePage />);

    await screen.findByTestId("phone-threads");
    await screen.findByTestId("phone-messages");
    expect(threadCalls()).toEqual([]);
    expect(screen.queryByTestId("phone-thread-view")).toBeNull();

    const open = screen.getByRole("button", { name: "Open conversation with Ada Lovelace" });
    fireEvent.click(open);

    await waitFor(() => expect(threadCalls()).toEqual([THREAD_PATH_11]));
    const view = screen.getByTestId("phone-thread-view");
    expect(await within(view).findAllByTestId("phone-thread-message")).toHaveLength(2);
    // The list is replaced by the conversation; back/close is the way out.
    expect(screen.queryByTestId("phone-threads")).toBeNull();
  });

  it("renders the wire order and wording (when/dir/who/text) with an honest newest-N note", async () => {
    render(<PhonePage />);
    const view = await openConversation("Ada Lovelace");

    const rows = await within(view).findAllByTestId("phone-thread-message");
    expect(rows).toHaveLength(2);
    // Newest first, exactly the order the store answered — nothing re-sorted client-side.
    expect(rows[0]).toHaveTextContent("Today 14:02");
    expect(rows[0]).toHaveTextContent("in");
    expect(rows[0]).toHaveTextContent("Ada Lovelace");
    expect(rows[0]).toHaveTextContent("eleven newest");
    expect(rows[1]).toHaveTextContent("eleven older, no clock");
    // A legal `when:null` is the app-wide unknown — never blank, never "Invalid Date".
    expect(rows[1]).toHaveTextContent("time unknown");
    expect(view.textContent ?? "").not.toContain("Invalid Date");

    const note = within(view).getByTestId("phone-thread-window");
    expect(note).toHaveTextContent("Showing the newest 2");
    expect(note).toHaveTextContent("100");
  });
});

/* ── the four states, honestly ────────────────────────────────────────────────────────────── */

describe("phone thread view — loading / empty / refusal / unavailable", () => {
  it("shows a busy loading state until the read answers", async () => {
    let resolve!: (value: unknown) => void;
    deferredThread = {
      promise: new Promise((done) => {
        resolve = done;
      }),
      resolve: (value) => resolve(value),
    };

    render(<PhonePage />);
    const view = await openConversation("Ada Lovelace");

    expect(within(view).getByTestId("phone-thread-loading")).toHaveAttribute("aria-busy", "true");
    expect(within(view).queryAllByTestId("phone-thread-message")).toHaveLength(0);

    await act(async () => {
      resolve(ok({ messages: THREAD_11 }));
    });

    expect(await within(view).findAllByTestId("phone-thread-message")).toHaveLength(2);
    expect(within(view).queryByTestId("phone-thread-loading")).toBeNull();
  });

  it("states an empty thread by name — no fabricated rows, no window note", async () => {
    threadAnswers[11] = ok({ messages: [] });

    render(<PhonePage />);
    const view = await openConversation("Ada Lovelace");

    expect(await within(view).findByTestId("phone-thread-empty")).toHaveTextContent(
      "No messages in this thread.",
    );
    expect(within(view).queryAllByTestId("phone-thread-message")).toHaveLength(0);
    expect(within(view).queryByTestId("phone-thread-window")).toBeNull();
    expect(within(view).queryByTestId("phone-thread-error")).toBeNull();
  });

  it("names a refusal inline (code + message + fix hint) and retries on demand", async () => {
    threadAnswers[11] = new ApiError(
      500,
      "the store read failed",
      "E_PHONE_READ",
      "check the phone store and retry",
    );

    render(<PhonePage />);
    const view = await openConversation("Ada Lovelace");

    const alert = await within(view).findByTestId("phone-thread-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("E_PHONE_READ");
    expect(alert).toHaveTextContent("the store read failed");
    expect(alert).toHaveTextContent("check the phone store and retry");
    expect(within(view).queryAllByTestId("phone-thread-message")).toHaveLength(0);
    expect(within(view).queryByTestId("phone-thread-empty")).toBeNull();

    threadAnswers[11] = ok({ messages: THREAD_11 });
    fireEvent.click(within(view).getByTestId("phone-thread-retry"));

    expect(await within(view).findAllByTestId("phone-thread-message")).toHaveLength(2);
    expect(threadCalls()).toEqual([THREAD_PATH_11, THREAD_PATH_11]);
    expect(within(view).queryByTestId("phone-thread-error")).toBeNull();
  });

  it("E_PHONE_UNAVAILABLE renders the page's ONE PHONE LINK UNAVAILABLE block", async () => {
    threadAnswers[11] = new ApiError(
      503,
      "the phone store is not mounted",
      "E_PHONE_UNAVAILABLE",
      "set PHONE_LINK_DB_DIR and restart",
    );

    render(<PhonePage />);
    const view = await openConversation("Ada Lovelace");

    const block = await within(view).findByTestId("phone-thread-unavailable");
    expect(block).toHaveTextContent("PHONE LINK UNAVAILABLE");
    expect(block).toHaveTextContent("E_PHONE_UNAVAILABLE");
    expect(block).toHaveTextContent("set PHONE_LINK_DB_DIR");
    expect(within(view).queryAllByTestId("phone-thread-message")).toHaveLength(0);
    expect(within(view).queryByTestId("phone-thread-empty")).toBeNull();
    expect(within(view).queryByRole("alert")).toBeNull();
  });

  it("refuses a wrong-typed row BY NAME — a numeric `when` never renders as a time", async () => {
    threadAnswers[11] = ok({
      messages: [{ when: 17, dir: "in", who: "Ada Lovelace", text: "bad when", thread_id: 11 }],
    });

    render(<PhonePage />);
    const view = await openConversation("Ada Lovelace");

    const alert = await within(view).findByTestId("phone-thread-error");
    expect(alert).toHaveTextContent("string-or-null when");
    expect(within(view).queryAllByTestId("phone-thread-message")).toHaveLength(0);
    expect(view.textContent ?? "").not.toContain("bad when");
  });
});

/* ── close / unmount: nothing held ───────────────────────────────────────────────────────── */

describe("phone thread view — clearing state", () => {
  it("back returns to the list and DROPS every conversation row", async () => {
    render(<PhonePage />);
    const view = await openConversation("Ada Lovelace");
    await within(view).findAllByTestId("phone-thread-message");
    expect(view.textContent ?? "").toContain("eleven newest");

    fireEvent.click(within(view).getByTestId("phone-thread-close"));

    expect(screen.queryByTestId("phone-thread-view")).toBeNull();
    expect(screen.getByTestId("phone-threads")).toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toContain("eleven newest");

    // Re-opening ANY thread starts from loading — no row from the previous conversation survives.
    const second = await openConversation("Grace Hopper");
    expect(within(second).getByTestId("phone-thread-loading")).toBeInTheDocument();
    expect(within(second).queryByText("eleven newest")).toBeNull();
    expect(await within(second).findByText("twelve only")).toBeInTheDocument();
  });

  it("unmounting mid-read clears the state — a late answer renders nothing", async () => {
    let resolve!: (value: unknown) => void;
    deferredThread = {
      promise: new Promise((done) => {
        resolve = done;
      }),
      resolve: (value) => resolve(value),
    };
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => undefined);

    try {
      const page = render(<PhonePage />);
      await openConversation("Ada Lovelace");
      page.unmount();

      await act(async () => {
        resolve(ok({ messages: THREAD_11 }));
      });

      expect(screen.queryByTestId("phone-thread-view")).toBeNull();
      expect(document.body.textContent ?? "").not.toContain("eleven newest");
      expect(errorSpy).not.toHaveBeenCalled();
    } finally {
      errorSpy.mockRestore();
    }
  });

  it("touches no storage API and logs nothing across a conversation", async () => {
    const setItem = jest.spyOn(Storage.prototype, "setItem");
    const getItem = jest.spyOn(Storage.prototype, "getItem");
    const removeItem = jest.spyOn(Storage.prototype, "removeItem");
    const log = jest.spyOn(console, "log").mockImplementation(() => undefined);
    const info = jest.spyOn(console, "info").mockImplementation(() => undefined);
    const debug = jest.spyOn(console, "debug").mockImplementation(() => undefined);

    try {
      render(<PhonePage />);
      const view = await openConversation("Ada Lovelace");
      await within(view).findAllByTestId("phone-thread-message");
      fireEvent.click(within(view).getByTestId("phone-thread-close"));

      expect(setItem).not.toHaveBeenCalled();
      expect(getItem).not.toHaveBeenCalled();
      expect(removeItem).not.toHaveBeenCalled();
      expect(log).not.toHaveBeenCalled();
      expect(info).not.toHaveBeenCalled();
      expect(debug).not.toHaveBeenCalled();
    } finally {
      setItem.mockRestore();
      getItem.mockRestore();
      removeItem.mockRestore();
      log.mockRestore();
      info.mockRestore();
      debug.mockRestore();
    }
  });
});

/* ── the parser contract, pinned without the UI ───────────────────────────────────────────── */

describe("parseMessages — the `when` nullability contract", () => {
  it("accepts a legal null `when` and keeps it null (no fabricated time)", () => {
    const rows = parseMessages({
      messages: [{ when: null, dir: "in", who: "Ada", text: "hi", thread_id: 11 }],
    });

    expect(rows).toHaveLength(1);
    expect(rows[0].when).toBeNull();
    expect(messageWhenText(rows[0].when)).toBe("time unknown");
  });

  it("still refuses a non-string, non-null `when`, by name", () => {
    expect(() =>
      parseMessages({
        messages: [{ when: 17, dir: "in", who: "Ada", text: "hi", thread_id: 11 }],
      }),
    ).toThrow(/string-or-null when/);
  });

  it("refuses a body without a messages array, by name", () => {
    expect(() => parseMessages({ rows: [] })).toThrow(/messages array/);
  });

  it("refuses a wrong-typed thread_id instead of rendering an undefined thread", () => {
    expect(() =>
      parseMessages({
        messages: [{ when: "Today", dir: "in", who: "A", text: "t", thread_id: "11" }],
      }),
    ).toThrow(/numeric thread_id/);
  });
});
