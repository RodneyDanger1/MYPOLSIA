/**
 * WAVE V16 / seat W3 — the phone UI family, H-P1 consumers (dashboard card + `/phone` page +
 * download). Written BEFORE the components exist (RED leg raw in
 * wave-v16/w3-phone-ui/raw/): pre-fix every import here fails module resolution, and the pins
 * describe the exact behaviour the implementation must exhibit.
 *
 * Wire truth consumed (frozen H-P1, E5 implements it in parallel): all routes `GET /api/v1/phone/*`
 * answer the envelope `{ok, data}`; a missing store is the named 503 `E_PHONE_UNAVAILABLE`; a
 * summary whose `store_status` is `"unavailable"` is an ANSWER, not data. Honesty law held here:
 *   - no fabricated rows/counts — an unavailable/absent store renders ONE named state;
 *   - `null` never renders as `0` — a null `unread_total` is the malformed-read state, not "0 unread";
 *   - a null `last_when` prints "time unknown", never an invented instant;
 *   - `when` strings render VERBATIM (the server's own wording — no client re-parse);
 *   - empty copy prints only after a successful, genuinely empty read;
 *   - the download is the V15 blob pattern (`SitePackageSection`): raw fetch + blob, saved name =
 *     the server's own `Content-Disposition`, auth from the client's single credential source.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() }, API_KEY: "test-key" };
});

/** The mutable search string the `useSearchParams` mock answers from. */
const mockSearch = { thread: null as string | null };

jest.mock("next/navigation", () => ({
  useSearchParams: () => {
    const params = new URLSearchParams();
    if (mockSearch.thread !== null) params.set("thread", mockSearch.thread);
    return params;
  },
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
import { PhoneCard } from "@/components/dashboard/PhoneCard";
import PhonePage from "@/app/phone/page";

const mockApiGet = api.get as jest.Mock;

const FETCH_MOCK = jest.fn();
let anchorClick: jest.SpyInstance;
let clicked: Array<{ download: string; href: string }> = [];

beforeEach(() => {
  mockApiGet.mockReset();
  mockSearch.thread = null;
  FETCH_MOCK.mockReset();
  global.fetch = FETCH_MOCK as unknown as typeof fetch;
  clicked = [];
  anchorClick = jest
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ download: this.download, href: this.href });
    });
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    writable: true,
    value: jest.fn(() => "blob:phone-export"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    writable: true,
    value: jest.fn(),
  });
});

afterEach(() => {
  anchorClick.mockRestore();
});

/* ── fixtures: the exact H-P1 wire shapes, envelopes included ──────────────────────────────── */

const ok = (data: unknown) => ({ ok: true, data });

const SUMMARY_DATA = {
  store_status: "ok",
  unread_total: 7,
  threads: [
    { thread_id: 11, with: "Ada Lovelace", unread: 3, last_when: "Today 14:02", last_text: "Are we still on for dinner?" },
    { thread_id: 12, with: "Grace Hopper", unread: 0, last_when: "Yesterday 09:10", last_text: "Thanks!" },
    { thread_id: 13, with: "Alan Turing", unread: 2, last_when: null, last_text: "Call me back" },
    { thread_id: 14, with: "Katherine Johnson", unread: 0, last_when: "Mon 08:00", last_text: "ok" },
  ],
  missed_calls_today: [
    { who: "Ada Lovelace", num_masked: "+1••• ••• 0197", when: "Today 13:58" },
    { who: "Grace Hopper", num_masked: "+1••• ••• 4488", when: "Today 11:20" },
  ],
};

const MESSAGES_DATA = [
  { when: "Today 14:02", dir: "in", who: "Ada Lovelace", text: "Are we still on for dinner?", thread_id: 11 },
  { when: "Today 13:40", dir: "out", who: "me", text: "Bringing the spreadsheet.", thread_id: 11 },
  { when: "Yesterday 09:10", dir: "in", who: "Grace Hopper", text: "Thanks!", thread_id: 12 },
];

const CALLS_DATA = [
  { when: "Today 13:58", type: "missed", who: "Ada Lovelace", num_masked: "+1••• ••• 0197", dur_s: null },
  { when: "Today 09:00", type: "incoming", who: "Grace Hopper", num_masked: "+1••• ••• 4488", dur_s: 64 },
];

const REFUSAL = () =>
  new ApiError(
    503,
    "the phone store is not mounted",
    "E_PHONE_UNAVAILABLE",
    "set PHONE_LINK_DB_DIR to the phone-link store directory and restart",
  );

/* ── the dashboard card ─────────────────────────────────────────────────────────────────────── */

describe("dashboard Phone card (H-P1 consumer)", () => {
  function answerCard(overrides: Record<string, unknown> = {}) {
    const paths: Record<string, unknown> = { "/phone/summary": ok(SUMMARY_DATA), ...overrides };
    mockApiGet.mockImplementation((path: string) => {
      const value = paths[path];
      if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
      return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
    });
  }

  it("renders the envelope: unread total, the newest 3 threads with a labelled truncation, missed calls today", async () => {
    answerCard();

    render(<PhoneCard />);

    expect(await screen.findByTestId("phone-unread-total")).toHaveTextContent("7 unread");
    const threads = within(screen.getByTestId("phone-card-threads")).getAllByTestId("phone-card-thread");
    expect(threads).toHaveLength(3);
    expect(threads[0]).toHaveTextContent("Ada Lovelace");
    expect(threads[0]).toHaveTextContent("Are we still on for dinner?");
    expect(threads[0]).toHaveTextContent("3 unread");
    // The 4th thread is NOT silently dropped — the truncation is stated.
    expect(screen.getByTestId("phone-card-threads-more")).toHaveTextContent(
      "Showing the 3 most recent threads of 4",
    );
    expect(screen.queryByText("Katherine Johnson")).toBeNull();

    const missed = within(screen.getByTestId("phone-card-missed")).getAllByTestId("phone-card-missed-row");
    expect(missed).toHaveLength(2);
    expect(missed[0]).toHaveTextContent("Ada Lovelace");
    expect(missed[0]).toHaveTextContent("+1••• ••• 0197");
    expect(missed[0]).toHaveTextContent("Today 13:58");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("prints a null last_when as time unknown — never an invented instant", async () => {
    answerCard();

    render(<PhoneCard />);

    const threads = within(await screen.findByTestId("phone-card-threads")).getAllByTestId("phone-card-thread");
    expect(threads[2]).toHaveTextContent("Alan Turing");
    expect(threads[2]).toHaveTextContent("time unknown");
  });

  it("E_PHONE_UNAVAILABLE renders the honest unavailable state — no rows, no counts", async () => {
    answerCard({ "/phone/summary": REFUSAL() });

    render(<PhoneCard />);

    const block = await screen.findByTestId("phone-card-unavailable");
    expect(block).toHaveTextContent("PHONE LINK UNAVAILABLE");
    expect(block).toHaveTextContent("E_PHONE_UNAVAILABLE");
    expect(block).toHaveTextContent("the phone store is not mounted");
    expect(block).toHaveTextContent("set PHONE_LINK_DB_DIR");
    // Nothing fabricated: no thread list, no missed list, no unread count anywhere.
    expect(screen.queryByTestId("phone-card-threads")).toBeNull();
    expect(screen.queryByTestId("phone-card-missed")).toBeNull();
    expect(screen.queryByTestId("phone-unread-total")).toBeNull();
    expect(screen.queryByText(/\d+\s+unread/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a 200 whose store_status is unavailable is the same honest state", async () => {
    answerCard({
      "/phone/summary": ok({ ...SUMMARY_DATA, store_status: "unavailable" }),
    });

    render(<PhoneCard />);

    const block = await screen.findByTestId("phone-card-unavailable");
    expect(block).toHaveTextContent("store_status");
    expect(block).toHaveTextContent("unavailable");
    // A store that says it is unavailable must not have its (meaningless) counts rendered.
    expect(screen.queryByTestId("phone-unread-total")).toBeNull();
    expect(screen.queryByTestId("phone-card-threads")).toBeNull();
  });

  it("a null unread_total is NOT 0 — the malformed read is named and no count is printed", async () => {
    answerCard({ "/phone/summary": ok({ ...SUMMARY_DATA, unread_total: null }) });

    render(<PhoneCard />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the phone summary");
    expect(alert).toHaveTextContent("numeric unread_total");
    expect(screen.queryByTestId("phone-unread-total")).toBeNull();
    expect(screen.queryByText(/0 unread/)).toBeNull();
    expect(screen.queryByTestId("phone-card-threads")).toBeNull();
  });

  it("a failed read is named — never the empty copy", async () => {
    answerCard({ "/phone/summary": new Error("E_HTTP_500: boom") });

    render(<PhoneCard />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the phone summary: E_HTTP_500: boom",
    );
    expect(screen.queryByText(/No threads in the phone store/)).toBeNull();
  });

  it("links to the /phone page", async () => {
    answerCard();

    render(<PhoneCard />);

    const link = await screen.findByRole("link", { name: /phone/i });
    expect(link).toHaveAttribute("href", "/phone");
  });
});

/* ── the /phone page ────────────────────────────────────────────────────────────────────────── */

describe("/phone page (H-P1 consumer)", () => {
  function answerPage(overrides: Record<string, unknown> = {}) {
    const paths: Record<string, unknown> = {
      "/phone/summary": ok(SUMMARY_DATA),
      "/phone/messages?limit=100": ok({ messages: MESSAGES_DATA }),
      "/phone/calls?limit=50": ok({ calls: CALLS_DATA }),
      ...overrides,
    };
    mockApiGet.mockImplementation((path: string) => {
      const value = paths[path];
      if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
      return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
    });
  }

  it("reads all three routes and renders their envelopes honestly", async () => {
    answerPage();

    render(<PhonePage />);

    // Threads index from the summary (all four — the page has room the card does not).
    const threads = within(await screen.findByTestId("phone-threads")).getAllByTestId("phone-thread-row");
    expect(threads).toHaveLength(4);
    expect(threads[0]).toHaveTextContent("Ada Lovelace");
    expect(threads[0].querySelector("a")).toHaveAttribute("href", "/phone?thread=11");
    // Null last_when again prints as time unknown here.
    expect(threads[2]).toHaveTextContent("time unknown");

    // Recent messages: exactly the loaded window, rows carrying the wire's own wording.
    const messages = within(screen.getByTestId("phone-messages")).getAllByTestId("phone-message-row");
    expect(messages).toHaveLength(3);
    expect(messages[0]).toHaveTextContent("Are we still on for dinner?");
    expect(messages[0]).toHaveTextContent("in");
    expect(screen.getByTestId("phone-messages-window")).toHaveTextContent("100");

    // Calls: dur_s 64 -> "64s"; a null duration is neither 0 nor invented.
    const calls = within(screen.getByTestId("phone-calls")).getAllByTestId("phone-call-row");
    expect(calls).toHaveLength(2);
    expect(calls[0]).toHaveTextContent("missed");
    expect(calls[0]).toHaveTextContent("+1••• ••• 0197");
    expect(calls[1]).toHaveTextContent("64s");
    expect(calls[0]).toHaveTextContent("—");

    expect(mockApiGet).toHaveBeenCalledWith("/phone/summary");
    expect(mockApiGet).toHaveBeenCalledWith("/phone/messages?limit=100");
    expect(mockApiGet).toHaveBeenCalledWith("/phone/calls?limit=50");
  });

  it("?thread=5 reads that thread by id and renders only it, with a back link", async () => {
    mockSearch.thread = "5";
    answerPage({
      "/phone/messages?limit=100&thread_id=5": ok({
        messages: [
          { when: "Today 12:00", dir: "in", who: "Thread Five", text: "first", thread_id: 5 },
          { when: "Today 12:05", dir: "out", who: "me", text: "second", thread_id: 5 },
        ],
      }),
    });

    render(<PhonePage />);

    expect(mockApiGet).toHaveBeenCalledWith("/phone/messages?limit=100&thread_id=5");
    expect(mockApiGet).not.toHaveBeenCalledWith("/phone/messages?limit=100");
    const messages = within(await screen.findByTestId("phone-messages")).getAllByTestId("phone-message-row");
    expect(messages).toHaveLength(2);
    expect(messages[0]).toHaveTextContent("first");
    // Only THIS thread's messages: the other threads' texts appear nowhere in the message list
    // (the thread index's last_text previews are a summary surface, checked separately above).
    expect(within(screen.getByTestId("phone-messages")).queryByText("Are we still on for dinner?")).toBeNull();
    expect(screen.getByRole("heading", { name: /Thread #5/ })).toBeInTheDocument();
    expect(screen.getByTestId("phone-thread-back")).toHaveAttribute("href", "/phone");
  });

  it("a junk ?thread= param is the documented no-op — no thread read, no claim", async () => {
    mockSearch.thread = "abc";
    answerPage();

    render(<PhonePage />);

    await screen.findByTestId("phone-messages");
    expect(mockApiGet).toHaveBeenCalledWith("/phone/messages?limit=100");
    expect(mockApiGet.mock.calls.every((call) => !String(call[0]).includes("thread_id"))).toBe(true);
    expect(screen.queryByRole("heading", { name: /Thread #/ })).toBeNull();
  });

  it("search filters the LOADED window and says what it filtered", async () => {
    answerPage();

    render(<PhonePage />);

    const rows = within(await screen.findByTestId("phone-messages")).getAllByTestId("phone-message-row");
    expect(rows).toHaveLength(3);

    fireEvent.change(screen.getByTestId("phone-search"), { target: { value: "dinner" } });
    const filtered = within(screen.getByTestId("phone-messages")).getAllByTestId("phone-message-row");
    expect(filtered).toHaveLength(1);
    expect(filtered[0]).toHaveTextContent("Are we still on for dinner?");
    expect(screen.getByTestId("phone-search-note")).toHaveTextContent("1 of 3");

    fireEvent.change(screen.getByTestId("phone-search"), { target: { value: "zzz-no-match" } });
    expect(screen.queryAllByTestId("phone-message-row")).toHaveLength(0);
    expect(screen.getByTestId("phone-search-empty")).toBeInTheDocument();
  });

  it("missed-only re-reads calls with missed_only=true", async () => {
    answerPage({
      "/phone/calls?limit=50&missed_only=true": ok({
        calls: [{ when: "Today 13:58", type: "missed", who: "Ada Lovelace", num_masked: "+1••• ••• 0197", dur_s: null }],
      }),
    });

    render(<PhonePage />);
    await screen.findByTestId("phone-calls");

    fireEvent.click(screen.getByTestId("phone-calls-missed-only"));

    await waitFor(() =>
      expect(mockApiGet).toHaveBeenCalledWith("/phone/calls?limit=50&missed_only=true"),
    );
    const calls = within(screen.getByTestId("phone-calls")).getAllByTestId("phone-call-row");
    expect(calls).toHaveLength(1);
    expect(calls[0]).toHaveTextContent("Ada Lovelace");
  });

  it("download saves the server's bytes under the server's OWN filename (V15 blob pattern)", async () => {
    answerPage();
    FETCH_MOCK.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      headers: {
        get: (name: string) =>
          name.toLowerCase() === "content-disposition"
            ? 'attachment; filename="phone-export-2026-10-15.zip"'
            : null,
      },
      blob: () => Promise.resolve(new Blob(["zip-bytes"], { type: "application/zip" })),
      text: () => Promise.resolve(""),
    });

    render(<PhonePage />);
    fireEvent.click(screen.getByTestId("phone-download"));

    await waitFor(() => expect(FETCH_MOCK).toHaveBeenCalledTimes(1));
    const [url, init] = FETCH_MOCK.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/api\/v1\/phone\/export$/);
    // The client's single credential source, sent exactly when non-empty.
    expect((init.headers as Record<string, string>)["X-API-Key"]).toBe("test-key");

    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(clicked[0].download).toBe("phone-export-2026-10-15.zip");
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("phone-download-done")).toHaveTextContent(
      "Downloaded phone-export-2026-10-15.zip.",
    );
  });

  it("a refused download is named — no blob, no anchor, no dead button", async () => {
    answerPage();
    FETCH_MOCK.mockResolvedValue({
      ok: false,
      status: 503,
      statusText: "Service Unavailable",
      headers: { get: () => null },
      text: () =>
        Promise.resolve(
          JSON.stringify({
            ok: false,
            error: {
              code: "E_PHONE_UNAVAILABLE",
              message: "the phone store is not mounted",
              how_to_fix: "set PHONE_LINK_DB_DIR and restart",
            },
          }),
        ),
      blob: () => Promise.resolve(new Blob([])),
    });

    render(<PhonePage />);
    fireEvent.click(screen.getByTestId("phone-download"));

    const alert = await screen.findByTestId("phone-download-error");
    expect(alert).toHaveTextContent("E_PHONE_UNAVAILABLE");
    expect(alert).toHaveTextContent("the phone store is not mounted");
    expect(alert).toHaveTextContent("set PHONE_LINK_DB_DIR and restart");
    expect(anchorClick).not.toHaveBeenCalled();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("E_PHONE_UNAVAILABLE on the summary renders ONE page-level honest state — no fabricated rows", async () => {
    answerPage({
      "/phone/summary": REFUSAL(),
      "/phone/messages?limit=100": REFUSAL(),
      "/phone/calls?limit=50": REFUSAL(),
    });

    render(<PhonePage />);

    const block = await screen.findByTestId("phone-unavailable");
    expect(block).toHaveTextContent("E_PHONE_UNAVAILABLE");
    expect(block).toHaveTextContent("set PHONE_LINK_DB_DIR");
    expect(screen.queryByTestId("phone-thread-row")).toBeNull();
    expect(screen.queryByTestId("phone-message-row")).toBeNull();
    expect(screen.queryByTestId("phone-call-row")).toBeNull();
    expect(screen.queryByText(/\d+\s+unread/)).toBeNull();
    // One state, not three stacked copies.
    expect(screen.getAllByText(/E_PHONE_UNAVAILABLE/)).toHaveLength(1);
  });
});
