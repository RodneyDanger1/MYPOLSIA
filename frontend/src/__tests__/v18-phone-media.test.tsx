/**
 * WAVE V18 / seat L4 — the phone MEDIA viewer (frozen list + stream surface) and the a11y bar it
 * must clear. Written BEFORE the viewer exists (RED leg raw/01-red.txt): pre-fix the media module
 * does not resolve and every pin below fails.
 *
 * Wire truth consumed (V18 plan, frozen by L2):
 *   GET /api/v1/phone/media?thread_id=N -> {ok,data:{parts:[{part_id,content_type,size_bytes,when,
 *                                          thread_id}], truncated}}
 *   GET /api/v1/phone/media/{part_id}   -> 200 binary; 404 E_NOT_FOUND; 503 E_PHONE_UNAVAILABLE
 * Both key-auth. The viewer is the ONLY consumer: no fabricated thumbnails, no autoplay, no
 * invented sizes/times, and every object URL it creates is revoked.
 *
 * Honesty law held here:
 *   - the grid renders the server's order and the server's own `when` verbatim;
 *   - `truncated:true` states the newest-50 cap by name;
 *   - an empty thread prints its own empty line only after a successful, genuinely empty read;
 *   - a 404 part is ONE inline chip — the rest of the gallery still opens;
 *   - a 503 part/read is the page's own PHONE LINK UNAVAILABLE block, never an empty list;
 *   - bytes are fetched only when a part is opened (no 50-request stampede), and a part above the
 *     stated inline-preview limit is offered as a download, not silently fetched.
 *
 * Keyboard-only navigation is pinned as behaviour: the opener button hands focus to the dialog's
 * close button, Tab is trapped inside the dialog, ArrowRight/ArrowLeft walk the parts, Escape
 * closes and focus RETURNS to the opener.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn() }, API_KEY: "test-key" };
});

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
import PhonePage from "@/app/phone/page";

const mockApiGet = api.get as jest.Mock;
const mockApiPost = api.post as jest.Mock;

const FETCH_MOCK = jest.fn();
let anchorClick: jest.SpyInstance;
let clicked: Array<{ download: string; href: string }> = [];
let createdUrls: string[] = [];

const API = "http://localhost/api/v1/phone/media";

/* ── fixtures: the exact frozen wire shapes, envelope included ─────────────────────────────── */

const ok = (data: unknown) => ({ ok: true, data });

const SUMMARY = {
  store_status: "ok",
  unread_total: 0,
  threads: [
    { thread_id: 11, with: "Ada Lovelace", unread: 0, last_when: "Today 14:02", last_text: "hi" },
  ],
  missed_calls_today: [],
};

const MESSAGES = [
  { when: "Today 14:02", dir: "in", who: "Ada Lovelace", text: "hi", thread_id: 11 },
];

const PARTS = [
  { part_id: 201, content_type: "image/jpeg", size_bytes: 1536, when: "Today 14:01", thread_id: 11 },
  { part_id: 202, content_type: "audio/mp4", size_bytes: 2_097_152, when: "Today 14:00", thread_id: 11 },
  { part_id: 203, content_type: "application/pdf", size_bytes: 4096, when: "Yesterday 09:00", thread_id: 11 },
];

const media = (parts: unknown[] = PARTS, truncated = false) => ok({ parts, truncated });

const REFUSAL_BODY = (code: string, message: string) => ({
  ok: false,
  error: { code, message, how_to_fix: "fix the phone store" },
});

function bytesResponse(
  status: number,
  opts: { blob?: Blob; body?: unknown; disposition?: string } = {},
) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 404 ? "Not Found" : status === 503 ? "Service Unavailable" : "OK",
    headers: {
      get: (name: string) =>
        name.toLowerCase() === "content-disposition" ? (opts.disposition ?? null) : null,
    },
    blob: () => Promise.resolve(opts.blob ?? new Blob(["bytes"], { type: "application/octet-stream" })),
    text: () => Promise.resolve(opts.body === undefined ? "" : JSON.stringify(opts.body)),
  };
}

/** Route the media stream fetch per part id; an unrouted id fails the test by name. */
function routeBytes(routes: Record<number, ReturnType<typeof bytesResponse>>) {
  FETCH_MOCK.mockImplementation((url: string) => {
    const match = /\/api\/v1\/phone\/media\/(\d+)$/.exec(url);
    const id = match ? Number(match[1]) : NaN;
    if (!routes[id]) return Promise.reject(new Error(`unexpected media fetch: ${url}`));
    return Promise.resolve(routes[id]);
  });
}

function answerPage(mediaAnswer: unknown = media()) {
  const paths: Record<string, unknown> = {
    "/phone/summary": ok(SUMMARY),
    "/phone/messages?limit=100&thread_id=11": ok({ messages: MESSAGES }),
    "/phone/calls?limit=50": ok({ calls: [] }),
    "/phone/media?thread_id=11": mediaAnswer,
  };
  mockApiGet.mockImplementation((path: string) => {
    const value = paths[path];
    if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

async function openThread() {
  mockSearch.thread = "11";
  render(<PhonePage />);
  const section = await screen.findByTestId("phone-media");
  // The section mounts before its read settles: wait for the ONE settled state (list, honest
  // empty, unavailable block, or a named failure) so assertions never race the skeleton.
  await waitFor(() => {
    const settled =
      within(section).queryByTestId("phone-media-list") ??
      within(section).queryByTestId("phone-media-empty") ??
      within(section).queryByTestId("phone-media-unavailable") ??
      within(section).queryByRole("alert");
    expect(settled).not.toBeNull();
  });
  return section;
}

beforeEach(() => {
  mockApiGet.mockReset();
  mockApiPost.mockReset();
  mockSearch.thread = "11";
  FETCH_MOCK.mockReset();
  global.fetch = FETCH_MOCK as unknown as typeof fetch;
  clicked = [];
  createdUrls = [];
  anchorClick = jest
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ download: this.download, href: this.href });
    });
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    writable: true,
    value: jest.fn((blob: Blob) => {
      const url = `blob:part-${createdUrls.length + 1}`;
      createdUrls.push(url);
      return url;
    }),
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

/* ── the list ──────────────────────────────────────────────────────────────────────────────── */

describe("phone media list (frozen GET /phone/media?thread_id)", () => {
  it("lists the thread's parts in the server's order with the store's own sizes and times", async () => {
    answerPage();
    const section = await openThread();

    const rows = within(section).getAllByTestId("phone-media-item");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("#201");
    expect(rows[0]).toHaveTextContent("image/jpeg");
    expect(rows[0]).toHaveTextContent("1.5 KB");
    expect(rows[0]).toHaveTextContent("Today 14:01");
    expect(rows[1]).toHaveTextContent("2.0 MB");
    expect(rows[2]).toHaveTextContent("4.0 KB");
    expect(rows[2]).toHaveTextContent("Yesterday 09:00");

    // The one bounded window is stated, not implied.
    expect(within(section).getByTestId("phone-media-note")).toHaveTextContent("50");
    expect(screen.queryByTestId("phone-media-truncated")).toBeNull();
  });

  it("truncated:true names the newest-50 cap instead of silently dropping the rest", async () => {
    answerPage(media(PARTS, true));
    const section = await openThread();

    expect(within(section).getByTestId("phone-media-truncated")).toHaveTextContent(
      "Showing the newest 50 media parts",
    );
  });

  it("no media is an honest per-thread empty state, and fetches no bytes", async () => {
    answerPage(media([]));
    const section = await openThread();

    expect(within(section).getByTestId("phone-media-empty")).toHaveTextContent(
      "No media in this thread.",
    );
    expect(within(section).queryByTestId("phone-media-list")).toBeNull();
    expect(FETCH_MOCK).not.toHaveBeenCalled();
  });

  it("E_PHONE_UNAVAILABLE on the list renders the page's ONE unavailable block — never an empty list", async () => {
    answerPage(
      new ApiError(503, "the phone store is not mounted", "E_PHONE_UNAVAILABLE", "set PHONE_LINK_DB_DIR"),
    );
    const section = await openThread();

    const block = within(section).getByTestId("phone-media-unavailable");
    expect(block).toHaveTextContent("PHONE LINK UNAVAILABLE");
    expect(block).toHaveTextContent("E_PHONE_UNAVAILABLE");
    expect(within(section).queryByTestId("phone-media-empty")).toBeNull();
    expect(within(section).queryByTestId("phone-media-list")).toBeNull();
  });

  it("a malformed list is refused BY NAME — a string part_id never renders as a row", async () => {
    answerPage(ok({ parts: [{ ...PARTS[0], part_id: "201" }], truncated: false }));
    const section = await openThread();

    expect(await within(section).findByRole("alert")).toHaveTextContent("numeric part_id");
    expect(within(section).queryByTestId("phone-media-list")).toBeNull();
  });

  it("without a thread the section says media is per thread and calls no media route", async () => {
    mockSearch.thread = null;
    answerPage();
    render(<PhonePage />);

    const section = await screen.findByTestId("phone-media");
    expect(within(section).getByTestId("phone-media-needs-thread")).toHaveTextContent("per thread");
    expect(mockApiGet.mock.calls.every((call) => !String(call[0]).includes("/phone/media"))).toBe(true);
  });
});

/* ── the viewer: bytes, object URLs, honest per-part states ───────────────────────────────── */

describe("phone media viewer (frozen GET /phone/media/{part_id})", () => {
  it("opens an image from a key-authed blob URL and REVOKES that URL on close", async () => {
    answerPage();
    routeBytes({ 201: bytesResponse(200, { blob: new Blob(["jpeg-bytes"], { type: "image/jpeg" }) }) });
    const section = await openThread();

    // Nothing is fetched until the operator opens a part — no 50-request stampede.
    expect(FETCH_MOCK).not.toHaveBeenCalled();

    fireEvent.click(within(section).getByRole("button", { name: "Open image part #201" }));

    const img = await within(section).findByTestId("phone-media-image");
    expect(img).toHaveAttribute("src", "blob:part-1");
    expect(img.getAttribute("alt")).toContain("#201");
    expect(FETCH_MOCK).toHaveBeenCalledTimes(1);
    const [url, init] = FETCH_MOCK.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/201`);
    expect((init.headers as Record<string, string>)["X-API-Key"]).toBe("test-key");

    fireEvent.keyDown(within(section).getByTestId("phone-media-viewer"), { key: "Escape" });

    expect(screen.queryByTestId("phone-media-viewer")).toBeNull();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:part-1");
    // Focus RETURNS to the control that opened the viewer.
    expect(within(section).getByRole("button", { name: "Open image part #201" })).toHaveFocus();
  });

  it("renders audio as a labelled <audio controls> with NO autoplay", async () => {
    answerPage();
    routeBytes({ 202: bytesResponse(200, { blob: new Blob(["aac"], { type: "audio/mp4" }) }) });
    const section = await openThread();

    fireEvent.click(within(section).getByRole("button", { name: "Play audio part #202" }));

    const audio = await within(section).findByTestId("phone-media-audio");
    expect(audio).toHaveAttribute("controls");
    expect(audio).not.toHaveAttribute("autoplay");
    expect(audio).toHaveAttribute("aria-label", "Audio part #202");
    expect(audio).toHaveAttribute("src", "blob:part-1");
  });

  it("keyboard-only: focus enters the dialog, Tab is trapped, arrows walk parts, Escape returns focus", async () => {
    answerPage();
    routeBytes({
      201: bytesResponse(200, { blob: new Blob(["jpeg"], { type: "image/jpeg" }) }),
      202: bytesResponse(200, { blob: new Blob(["aac"], { type: "audio/mp4" }) }),
    });
    const section = await openThread();

    const opener = within(section).getByRole("button", { name: "Open image part #201" });
    fireEvent.click(opener);

    await within(section).findByTestId("phone-media-image");
    const viewer = within(section).getByTestId("phone-media-viewer");
    expect(viewer).toHaveAttribute("role", "dialog");
    expect(viewer).toHaveAttribute("aria-modal", "true");
    expect(within(viewer).getByTestId("phone-media-close")).toHaveFocus();
    expect(within(viewer).getByTestId("phone-media-counter")).toHaveTextContent("1 of 3");

    // Shift+Tab from the first focusable wraps to the last (the trap holds).
    fireEvent.keyDown(within(viewer).getByTestId("phone-media-close"), { key: "Tab", shiftKey: true });
    expect(within(viewer).getByTestId("phone-media-next")).toHaveFocus();

    // ArrowRight walks to the next part without a pointer.
    fireEvent.keyDown(within(viewer).getByTestId("phone-media-close"), { key: "ArrowRight" });
    await waitFor(() => expect(within(viewer).getByTestId("phone-media-counter")).toHaveTextContent("2 of 3"));
    expect(await within(viewer).findByTestId("phone-media-audio")).toBeInTheDocument();

    // The previous object URL was revoked when the part changed — one live URL at a time.
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:part-1");

    fireEvent.keyDown(viewer, { key: "Escape" });
    expect(screen.queryByTestId("phone-media-viewer")).toBeNull();
    expect(opener).toHaveFocus();
  });

  it("a 404 part is ONE inline chip — the gallery keeps working and the next part opens", async () => {
    answerPage();
    routeBytes({
      201: bytesResponse(404, { body: REFUSAL_BODY("E_NOT_FOUND", "no media part 201") }),
      202: bytesResponse(200, { blob: new Blob(["aac"], { type: "audio/mp4" }) }),
    });
    const section = await openThread();

    fireEvent.click(within(section).getByRole("button", { name: "Open image part #201" }));

    const missing = await within(section).findByTestId("phone-media-part-missing");
    expect(missing).toHaveTextContent("part not found");
    expect(missing).toHaveTextContent("E_NOT_FOUND");
    expect(screen.queryByTestId("phone-media-image")).toBeNull();
    // The page did not crash: the other parts are still listed and still openable.
    expect(within(section).getAllByTestId("phone-media-item")).toHaveLength(3);

    fireEvent.keyDown(within(section).getByTestId("phone-media-viewer"), { key: "ArrowRight" });
    expect(await within(section).findByTestId("phone-media-audio")).toBeInTheDocument();
  });

  it("a 503 on one part renders the page's PHONE LINK UNAVAILABLE block for that part", async () => {
    answerPage();
    routeBytes({
      201: bytesResponse(503, {
        body: REFUSAL_BODY("E_PHONE_UNAVAILABLE", "the phone store is not mounted"),
      }),
    });
    const section = await openThread();

    fireEvent.click(within(section).getByRole("button", { name: "Open image part #201" }));

    const block = await within(section).findByTestId("phone-media-part-unavailable");
    expect(block).toHaveTextContent("PHONE LINK UNAVAILABLE");
    expect(block).toHaveTextContent("E_PHONE_UNAVAILABLE");
  });

  it("an oversized part is stated, offered as a download, and NOT auto-fetched", async () => {
    const huge = {
      part_id: 204,
      content_type: "image/png",
      size_bytes: 40 * 1024 * 1024,
      when: "Today 13:00",
      thread_id: 11,
    };
    answerPage(media([...PARTS, huge]));
    routeBytes({});
    const section = await openThread();

    fireEvent.click(within(section).getByRole("button", { name: "Open image part #204" }));

    const note = await within(section).findByTestId("phone-media-oversized");
    expect(note).toHaveTextContent("40.0 MB");
    expect(note).toHaveTextContent("32 MB");
    expect(within(section).getByRole("button", { name: "Download part #204" })).toBeInTheDocument();
    expect(FETCH_MOCK).not.toHaveBeenCalled();
  });

  it("a non-previewable part downloads under the SERVER's filename, then revokes the URL", async () => {
    answerPage();
    routeBytes({
      203: bytesResponse(200, {
        blob: new Blob(["pdf"], { type: "application/pdf" }),
        disposition: 'attachment; filename="phone-part-203.pdf"',
      }),
    });
    const section = await openThread();

    fireEvent.click(within(section).getByRole("button", { name: "Download part #203" }));

    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(clicked[0].download).toBe("phone-part-203.pdf");
    expect(clicked[0].href).toBe("blob:part-1");
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:part-1");
    expect(await within(section).findByTestId("phone-media-saved")).toHaveTextContent(
      "Downloaded phone-part-203.pdf.",
    );
  });

  it("a download with no Content-Disposition falls back to the stated phone-part-<id>.<ext>", async () => {
    const txt = {
      part_id: 205,
      content_type: "text/plain",
      size_bytes: 12,
      when: "Today 12:00",
      thread_id: 11,
    };
    answerPage(media([txt]));
    routeBytes({ 205: bytesResponse(200, { blob: new Blob(["hi"], { type: "text/plain" }) }) });
    const section = await openThread();

    fireEvent.click(within(section).getByRole("button", { name: "Download part #205" }));

    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(clicked[0].download).toBe("phone-part-205.txt");
  });
});

/* ── the sweep's phone-console a11y pins (SAME page, cheap to hold here) ──────────────────── */

describe("phone console a11y", () => {
  it("a send refusal is ASSOCIATED with the fields it refused (aria-invalid + aria-describedby)", async () => {
    answerPage();
    routeBytes({});
    mockApiPost.mockRejectedValue(
      new ApiError(422, "body is required", "E_VALIDATION", "send a non-empty body"),
    );
    render(<PhonePage />);

    const to = await screen.findByTestId("phone-send-to");
    fireEvent.change(to, { target: { value: "+155****4477" } });
    fireEvent.change(screen.getByTestId("phone-send-body"), { target: { value: "hello" } });
    fireEvent.click(screen.getByTestId("phone-send-submit"));

    const alert = await screen.findByTestId("phone-send-error");
    expect(alert).toHaveAttribute("id");
    const describedBy = screen.getByTestId("phone-send-body").getAttribute("aria-describedby");
    expect(describedBy).toBe(alert.getAttribute("id"));
    expect(screen.getByTestId("phone-send-body")).toHaveAttribute("aria-invalid", "true");
  });

  it("the search result count is a live status region, so filtering is announced", async () => {
    answerPage();
    routeBytes({});
    render(<PhonePage />);
    await screen.findByTestId("phone-messages");

    fireEvent.change(screen.getByTestId("phone-search"), { target: { value: "hi" } });

    const note = screen.getByTestId("phone-search-note");
    expect(note).toHaveAttribute("role", "status");
    expect(note).toHaveTextContent("1 of 1");

    fireEvent.change(screen.getByTestId("phone-search"), { target: { value: "zzz" } });
    expect(screen.getByTestId("phone-search-empty")).toHaveAttribute("role", "status");
  });
});
