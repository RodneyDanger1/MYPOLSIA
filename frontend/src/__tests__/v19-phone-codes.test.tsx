/**
 * WAVE V19 / seat L1 — the owner-facing CODES panel on `/phone`, and the media `when` nullable
 * parser fix. Written BEFORE either exists (RED leg: raw/01-red.txt — the panel module does not
 * resolve and every pin below fails).
 *
 * Wire truth consumed (V19 plan, frozen — the route is unchanged):
 *   GET /api/v1/phone/codes?limit(1..50)&within_hours  -> {ok,data:{codes:[{when,from,code,snippet}]}}
 *   Every call writes ONE audit row (count + window only — values are never logged server-side).
 *
 * The privacy laws pinned here, because a codes surface that leaks is worse than none:
 *   - IDLE DOES NOT FETCH. The route is never called on mount; only the explicit CTA does.
 *   - MASKED BY DEFAULT (`•••` + the last two characters), Reveal per row, Copy copies the FULL
 *     code and re-masks immediately.
 *   - NOTHING IS PERSISTED OR LOGGED client-side: no localStorage/sessionStorage, no console.
 *   - STATE IS IN-MEMORY ONLY: unmount removes every code from the DOM, and no timer is left.
 *   - a null `when` is "time unknown"; a malformed row is DROPPED and counted, never fabricated.
 *   - E_PHONE_UNAVAILABLE renders the page's one PHONE LINK UNAVAILABLE block; a 422 renders its
 *     own code name inline.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

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
import { maskCode, parseCodes, parseMediaList } from "@/components/phone/phoneApi";

const mockApiGet = api.get as jest.Mock;

const ok = (data: unknown) => ({ ok: true, data });

const SUMMARY = {
  store_status: "ok",
  unread_total: 0,
  threads: [
    { thread_id: 11, with: "Ada Lovelace", unread: 0, last_when: "Today 14:02", last_text: "hi" },
  ],
  missed_calls_today: [],
};

/** The exact frozen codes wire rows — one with the store's legal `when: null`. */
const CODES = [
  {
    when: "Today 14:02",
    from: "Google",
    code: "148293",
    snippet: "G-148293 is your Google verification code.",
  },
  {
    when: null,
    from: "Bank",
    code: "997712",
    snippet: "Your one-time code is 997712 — never share it.",
  },
];

let codesAnswer: unknown = ok({ codes: CODES });
let writeText: jest.Mock;

/** Every codes read the panel made — the idle pin asserts this stays empty. */
const codesCalls = () => mockApiGet.mock.calls.map((call) => String(call[0])).filter((p) => p.startsWith("/phone/codes"));

function answerPage(mediaAnswer?: unknown) {
  const paths: Record<string, unknown> = {
    "/phone/summary": ok(SUMMARY),
    "/phone/messages?limit=100": ok({ messages: [] }),
    "/phone/calls?limit=50": ok({ calls: [] }),
  };
  if (mediaAnswer !== undefined) paths["/phone/media?thread_id=11"] = mediaAnswer;
  mockApiGet.mockImplementation((path: string) => {
    if (path.startsWith("/phone/codes")) {
      return codesAnswer instanceof Error ? Promise.reject(codesAnswer) : Promise.resolve(codesAnswer);
    }
    const value = paths[path];
    if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return Promise.resolve(value);
  });
}

/** The panel is below the gate: wait for the summary-ok branch to mount it. */
async function openPanel() {
  const panel = await screen.findByTestId("phone-codes");
  return panel;
}

async function readCodes(panel: HTMLElement) {
  fireEvent.click(within(panel).getByTestId("phone-codes-read"));
  await within(panel).findAllByTestId("phone-code-row");
}

beforeEach(() => {
  mockApiGet.mockReset();
  codesAnswer = ok({ codes: CODES });
  writeText = jest.fn(() => Promise.resolve());
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  answerPage();
});

/* ── idle: no fetch until the owner asks ──────────────────────────────────────────────────── */

describe("codes panel — fetch-on-click only", () => {
  it("mounts IDLE and calls no codes route until the explicit CTA", async () => {
    render(<PhonePage />);
    const panel = await openPanel();

    expect(within(panel).getByTestId("phone-codes-read")).toHaveTextContent("Read verification codes");
    // Give the page's own reads time to settle, then prove nothing touched /phone/codes.
    await waitFor(() => expect(within(panel).getByTestId("phone-codes-read")).toBeInTheDocument());
    expect(codesCalls()).toHaveLength(0);
    expect(within(panel).queryByTestId("phone-code-row")).toBeNull();
  });

  it("fetches exactly once with the chosen limit and window", async () => {
    render(<PhonePage />);
    const panel = await openPanel();

    fireEvent.click(within(panel).getByTestId("phone-codes-window-1"));
    await readCodes(panel);

    expect(codesCalls()).toEqual(["/phone/codes?limit=10&within_hours=1"]);
    expect(within(panel).getAllByTestId("phone-code-row")).toHaveLength(2);
  });

  it("the default window is 24 h; 'All' sends no window parameter", async () => {
    render(<PhonePage />);
    const panel = await openPanel();

    await readCodes(panel);
    expect(codesCalls()).toEqual(["/phone/codes?limit=10&within_hours=24"]);

    // Choosing a different window CLEARS the rows — a row can never sit under a window it was
    // not read in.
    fireEvent.click(within(panel).getByTestId("phone-codes-window-all"));
    expect(within(panel).queryByTestId("phone-code-row")).toBeNull();
    expect(within(panel).getByTestId("phone-codes-read")).toBeInTheDocument();

    fireEvent.click(within(panel).getByTestId("phone-codes-read"));
    await within(panel).findAllByTestId("phone-code-row");
    expect(codesCalls()).toEqual([
      "/phone/codes?limit=10&within_hours=24",
      "/phone/codes?limit=10",
    ]);
  });
});

/* ── masking / reveal / copy ──────────────────────────────────────────────────────────────── */

describe("codes panel — masking, reveal, copy", () => {
  it("renders every code MASKED by default (last two characters only)", async () => {
    render(<PhonePage />);
    const panel = await openPanel();
    await readCodes(panel);

    const rows = within(panel).getAllByTestId("phone-code-row");
    const first = within(rows[0]).getByTestId("phone-code-value");
    expect(first).toHaveTextContent("•••93");
    expect(first.textContent).not.toContain("148293");
    // The full code is nowhere in the DOM before a reveal — not in text, not in an attribute.
    expect(panel.innerHTML).not.toContain("148293");
    expect(panel.innerHTML).not.toContain("997712");
  });

  it("Reveal shows the code, toggles aria-pressed, and Hide masks it again", async () => {
    render(<PhonePage />);
    const panel = await openPanel();
    await readCodes(panel);

    const row = within(panel).getAllByTestId("phone-code-row")[0];
    const reveal = within(row).getByTestId("phone-code-reveal");
    expect(reveal).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(reveal);
    expect(within(row).getByTestId("phone-code-value")).toHaveTextContent("148293");
    expect(reveal).toHaveAttribute("aria-pressed", "true");
    expect(reveal).toHaveTextContent("Hide");

    fireEvent.click(reveal);
    expect(within(row).getByTestId("phone-code-value")).toHaveTextContent("•••93");
    expect(reveal).toHaveAttribute("aria-pressed", "false");
  });

  it("Escape re-masks a revealed row (keyboard-only parity)", async () => {
    render(<PhonePage />);
    const panel = await openPanel();
    await readCodes(panel);

    const row = within(panel).getAllByTestId("phone-code-row")[0];
    fireEvent.click(within(row).getByTestId("phone-code-reveal"));
    expect(within(row).getByTestId("phone-code-value")).toHaveTextContent("148293");

    fireEvent.keyDown(within(row).getByTestId("phone-code-reveal"), { key: "Escape" });
    expect(within(row).getByTestId("phone-code-value")).toHaveTextContent("•••93");
    expect(within(row).getByTestId("phone-code-reveal")).toHaveAttribute("aria-pressed", "false");
    // Escape did not also collapse the panel — the first Escape belongs to the reveal.
    expect(within(panel).getAllByTestId("phone-code-row")).toHaveLength(2);
  });

  it("blurring out of a revealed row masks it again", async () => {
    render(<PhonePage />);
    const panel = await openPanel();
    await readCodes(panel);

    const row = within(panel).getAllByTestId("phone-code-row")[0];
    fireEvent.click(within(row).getByTestId("phone-code-reveal"));
    expect(within(row).getByTestId("phone-code-value")).toHaveTextContent("148293");

    // Focus moves out of the row entirely (to the panel's collapse control).
    fireEvent.blur(within(row).getByTestId("phone-code-reveal"), { relatedTarget: within(panel).getByTestId("phone-codes-toggle") });
    expect(within(row).getByTestId("phone-code-value")).toHaveTextContent("•••93");
  });

  it("Copy writes the FULL code while masked and re-masks immediately", async () => {
    render(<PhonePage />);
    const panel = await openPanel();
    await readCodes(panel);

    const row = within(panel).getAllByTestId("phone-code-row")[0];
    fireEvent.click(within(row).getByTestId("phone-code-reveal"));
    fireEvent.click(within(row).getByTestId("phone-code-copy"));

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText).toHaveBeenCalledWith("148293");
    // The panel is masked again, and it SAYS so (a status region, not a silent copy).
    expect(within(row).getByTestId("phone-code-value")).toHaveTextContent("•••93");
    expect(within(row).getByTestId("phone-code-reveal")).toHaveAttribute("aria-pressed", "false");
    const copied = within(row).getByTestId("phone-code-copied");
    expect(copied).toHaveAttribute("role", "status");
    expect(copied).toHaveTextContent("masked again");
  });

  it("Copy works from the MASKED state too — the mask is display only, never the value", async () => {
    render(<PhonePage />);
    const panel = await openPanel();
    await readCodes(panel);

    const row = within(panel).getAllByTestId("phone-code-row")[0];
    const value = within(row).getByTestId("phone-code-value");
    expect(value).toHaveTextContent("•••93");

    fireEvent.click(within(row).getByTestId("phone-code-copy"));

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText).toHaveBeenCalledWith("148293");
    expect(value).toHaveTextContent("•••93");
  });

  it("collapsing CLEARS every held code (keyboard Escape, then Expand is idle)", async () => {
    render(<PhonePage />);
    const panel = await openPanel();
    await readCodes(panel);

    // No row is revealed: the first Escape collapses the panel and drops the rows.
    fireEvent.keyDown(within(panel).getAllByTestId("phone-code-value")[0], { key: "Escape" });

    expect(within(panel).getByTestId("phone-codes-collapsed")).toBeInTheDocument();
    expect(within(panel).queryByTestId("phone-code-row")).toBeNull();
    expect(within(panel).queryByTestId("phone-codes-read")).toBeNull();
    expect(panel.textContent ?? "").not.toContain("148293");

    fireEvent.click(within(panel).getByTestId("phone-codes-toggle"));
    expect(within(panel).getByTestId("phone-codes-read")).toBeInTheDocument();
    expect(within(panel).queryByTestId("phone-code-row")).toBeNull();
  });

  it("a refused clipboard is named inline, never silent, and leaves the code masked", async () => {
    writeText.mockRejectedValueOnce(new Error("clipboard is not available in this context"));
    render(<PhonePage />);
    const panel = await openPanel();
    await readCodes(panel);

    const row = within(panel).getAllByTestId("phone-code-row")[0];
    fireEvent.click(within(row).getByTestId("phone-code-reveal"));
    fireEvent.click(within(row).getByTestId("phone-code-copy"));

    const failure = await within(row).findByTestId("phone-code-copy-error");
    expect(failure).toHaveAttribute("role", "alert");
    expect(failure).toHaveTextContent("clipboard is not available");
    expect(within(row).getByTestId("phone-code-value")).toHaveTextContent("•••93");
  });
});

/* ── privacy: no storage, no console, nothing left behind ────────────────────────────────── */

describe("codes panel — client-side privacy", () => {
  it("touches no storage API and logs nothing across a full read/reveal/copy flow", async () => {
    const setItem = jest.spyOn(Storage.prototype, "setItem");
    const getItem = jest.spyOn(Storage.prototype, "getItem");
    const removeItem = jest.spyOn(Storage.prototype, "removeItem");
    const log = jest.spyOn(console, "log").mockImplementation(() => undefined);
    const info = jest.spyOn(console, "info").mockImplementation(() => undefined);
    const debug = jest.spyOn(console, "debug").mockImplementation(() => undefined);
    const error = jest.spyOn(console, "error").mockImplementation(() => undefined);

    try {
      render(<PhonePage />);
      const panel = await openPanel();
      await readCodes(panel);

      const row = within(panel).getAllByTestId("phone-code-row")[0];
      fireEvent.click(within(row).getByTestId("phone-code-reveal"));
      fireEvent.click(within(row).getByTestId("phone-code-copy"));
      await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));

      expect(setItem).not.toHaveBeenCalled();
      expect(getItem).not.toHaveBeenCalled();
      expect(removeItem).not.toHaveBeenCalled();
      expect(log).not.toHaveBeenCalled();
      expect(info).not.toHaveBeenCalled();
      expect(debug).not.toHaveBeenCalled();
      // `console.error` is NOT forbidden outright (React may use it), but it must never carry a
      // code value — the whole point of the masking.
      const logged = error.mock.calls.map((call) => call.map((part) => String(part)).join(" ")).join("\n");
      expect(logged).not.toContain("148293");
      expect(logged).not.toContain("997712");
    } finally {
      setItem.mockRestore();
      getItem.mockRestore();
      removeItem.mockRestore();
      log.mockRestore();
      info.mockRestore();
      debug.mockRestore();
      error.mockRestore();
    }
  });

  it("unmount removes every code from the DOM and leaves NO timer behind", async () => {
    jest.useFakeTimers();
    try {
      const view = render(<PhonePage />);
      const panel = await openPanel();
      fireEvent.click(within(panel).getByTestId("phone-codes-read"));
      await within(panel).findAllByTestId("phone-code-row");

      const row = within(panel).getAllByTestId("phone-code-row")[0];
      fireEvent.click(within(row).getByTestId("phone-code-reveal"));
      expect(within(row).getByTestId("phone-code-value")).toHaveTextContent("148293");

      view.unmount();

      // The codes lived in component state only: unmounting the page removes them from the DOM.
      expect(document.body.textContent ?? "").not.toContain("148293");
      expect(document.body.textContent ?? "").not.toContain("997712");
      expect(screen.queryByTestId("phone-codes")).toBeNull();
      expect(jest.getTimerCount()).toBe(0);
    } finally {
      jest.useRealTimers();
    }
  });
});

/* ── hostile wire: malformed rows, null times, refusal names ─────────────────────────────── */

describe("codes panel — hostile wire", () => {
  it("drops malformed rows and COUNTS them — never fabricates a row", async () => {
    codesAnswer = ok({
      codes: [
        CODES[0],
        { when: "Today 14:00", from: "Spam", code: 555111, snippet: "numeric code" },
        { when: 42, from: "Broken", code: "777888", snippet: "bad when" },
        CODES[1],
      ],
    });
    render(<PhonePage />);
    const panel = await openPanel();
    await readCodes(panel);

    expect(within(panel).getAllByTestId("phone-code-row")).toHaveLength(2);
    const note = within(panel).getByTestId("phone-codes-dropped");
    expect(note).toHaveTextContent("2 malformed rows were dropped");
    expect(panel.textContent ?? "").not.toContain("555111");
    expect(panel.textContent ?? "").not.toContain("777888");
  });

  it("a null codes `when` renders 'time unknown' — never Invalid Date", async () => {
    render(<PhonePage />);
    const panel = await openPanel();
    await readCodes(panel);

    const rows = within(panel).getAllByTestId("phone-code-row");
    expect(rows[1]).toHaveTextContent("time unknown");
    expect(panel.textContent ?? "").not.toContain("Invalid Date");
  });

  it("states the empty window by name ('no codes within N h')", async () => {
    codesAnswer = ok({ codes: [] });
    render(<PhonePage />);
    const panel = await openPanel();
    fireEvent.click(within(panel).getByTestId("phone-codes-window-6"));
    fireEvent.click(within(panel).getByTestId("phone-codes-read"));

    const empty = await within(panel).findByTestId("phone-codes-empty");
    expect(empty).toHaveTextContent("No verification codes within 6 h");
  });

  it("E_PHONE_UNAVAILABLE renders the page's ONE unavailable block — never an empty list", async () => {
    codesAnswer = new ApiError(503, "the phone store is not mounted", "E_PHONE_UNAVAILABLE", "set PHONE_LINK_DB_DIR");
    render(<PhonePage />);
    const panel = await openPanel();
    fireEvent.click(within(panel).getByTestId("phone-codes-read"));

    const block = await within(panel).findByTestId("phone-codes-unavailable");
    expect(block).toHaveTextContent("PHONE LINK UNAVAILABLE");
    expect(block).toHaveTextContent("E_PHONE_UNAVAILABLE");
    expect(within(panel).queryByTestId("phone-codes-empty")).toBeNull();
    expect(within(panel).queryByTestId("phone-code-row")).toBeNull();
  });

  it("a 422 renders its refusal code inline (the panel stays usable)", async () => {
    codesAnswer = new ApiError(422, "within_hours must be greater than 0", "E_VALIDATION", "send a positive window");
    render(<PhonePage />);
    const panel = await openPanel();
    fireEvent.click(within(panel).getByTestId("phone-codes-read"));

    const failure = await within(panel).findByTestId("phone-codes-error");
    expect(failure).toHaveAttribute("role", "alert");
    expect(failure).toHaveTextContent("E_VALIDATION");
    expect(within(panel).getByTestId("phone-codes-read")).toBeInTheDocument();
  });

  it("names the audit trail: every read writes one counted row, values never logged", async () => {
    render(<PhonePage />);
    const panel = await openPanel();
    await readCodes(panel);

    const note = within(panel).getByTestId("phone-codes-audit-note");
    expect(note).toHaveTextContent("audit");
    expect(note).toHaveTextContent("count and window only");
    expect(note).toHaveTextContent("never logged");
    expect(within(panel).getByTestId("phone-codes-limit-note")).toHaveTextContent("10");
  });
});

/* ── the V19 item 2: media `when` is NULLABLE on the wire ─────────────────────────────────── */

describe("media `when` nullability (V19 item 2)", () => {
  it("parseMediaList ACCEPTS when:null and keeps it null (no fabricated time)", () => {
    const parsed = parseMediaList(
      ok({
        parts: [{ part_id: 302, content_type: "image/png", size_bytes: 10, when: null, thread_id: 11 }],
        truncated: false,
      }).data,
    );
    expect(parsed.parts).toHaveLength(1);
    expect(parsed.parts[0].when).toBeNull();
  });

  it("still refuses a non-string, non-null when (a number never renders as a time)", () => {
    expect(() =>
      parseMediaList({
        parts: [{ part_id: 302, content_type: "image/png", size_bytes: 10, when: 17, thread_id: 11 }],
        truncated: false,
      }),
    ).toThrow(/string-or-null when/);
  });

  it("renders the app-wide honest placeholder for a null media `when` — never Invalid Date", async () => {
    answerPage(ok({
      parts: [{ part_id: 302, content_type: "image/png", size_bytes: 10, when: null, thread_id: 11 }],
      truncated: false,
    }));
    const { PhoneMediaSection } = await import("@/components/phone/PhoneMedia");
    render(<PhoneMediaSection threadId={11} />);
    const list = await screen.findByTestId("phone-media-list");
    expect(list).toHaveTextContent("time unknown");
    expect(document.body.textContent ?? "").not.toContain("Invalid Date");
  });
});

/* ── the parser itself, pinned without the UI ─────────────────────────────────────────────── */

describe("codes parser + mask (unit)", () => {
  it("parseCodes counts dropped rows and keeps the server's order", () => {
    const data = {
      codes: [
        CODES[0],
        { when: "x", from: "y", code: "", snippet: "empty code" },
        CODES[1],
      ],
    };
    const parsed = parseCodes(data);
    expect(parsed.codes).toHaveLength(2);
    expect(parsed.dropped).toBe(1);
    expect(parsed.codes[0].from).toBe("Google");
  });

  it("parseCodes refuses a body without a codes array, BY NAME", () => {
    expect(() => parseCodes({ rows: [] })).toThrow(/codes array/);
  });

  it("maskCode shows only the last two characters and never the length of short codes", () => {
    expect(maskCode("148293")).toBe("•••93");
    expect(maskCode("12")).toBe("•••");
    expect(maskCode("")).toBe("");
  });
});
