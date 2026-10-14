/**
 * Wave V8b / seat l1-strip-log — the activity strip + expandable activity log.
 *
 * The strip is the THIRD view of the ONE activity stream (C13): the transport hook is mocked here
 * (`jest.mock` on the hook path) so every status word, event list and error is driven
 * deterministically — the real hook opens a socket and belongs to its own tests. The fixtures carry
 * the wire's own nulls (`created_at: null` is a real value) so the honest placeholders are
 * exercised rather than invented.
 *
 * Pinned here: the four status words, the newest-event band line, the LOG affordance
 * (aria-expanded / focus into / focus return / Esc / scrim), the stage chips (ALL · distinct
 * actions · OTHER — never a silently hidden event), the counter, COPY == the rendered visible lines
 * verbatim, DOWNLOAD == the same text as `polsia-activity.log`, and the empty/error honesty.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/hooks/useActivityFeed", () => ({ useActivityFeed: jest.fn() }));

import { ActivityStrip, COPY_CONFIRM_MS } from "@/components/shell/ActivityStrip";
import { useActivityFeed, type FeedStatus } from "@/hooks/useActivityFeed";
import type { ActivityEvent } from "@/lib/api";

const mockHook = useActivityFeed as jest.Mock;

type FeedState = {
  events: ActivityEvent[];
  connected: boolean;
  status: FeedStatus;
  error: string | null;
  attempts: number;
};

function setFeed(over: Partial<FeedState> = {}): void {
  mockHook.mockReturnValue({
    events: [],
    connected: false,
    status: "connecting",
    error: null,
    attempts: 0,
    ...over,
  });
}

function ev(id: number, over: Partial<ActivityEvent> = {}): ActivityEvent {
  return {
    id,
    agent_type: "scout",
    action: "deploy",
    summary: "Shipped the release",
    level: "info",
    created_at: "2026-10-14T14:36:00.000Z",
    ...over,
  };
}

/** The feed's own time formatting (mirrored from dashboard/ActivityFeed.tsx) — the wording under
 *  test is the app-wide one, including the null "date unknown" placeholder. */
const clock = (iso: string | null): string =>
  iso === null
    ? "date unknown"
    : new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });

const rowTexts = (): string[] =>
  screen.getAllByTestId("log-row").map((row) => row.textContent ?? "");

/** jsdom's Blob has no `.text()`; FileReader is the supported way to read one back. */
function blobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

/** Open the overlay the way a browser does: the LOG button takes focus on click, then fires. */
function openLog(): HTMLElement {
  const log = screen.getByTestId("strip-log");
  log.focus();
  fireEvent.click(log);
  return log;
}

let writeText: jest.Mock;
let createObjectURL: jest.Mock;
let revokeObjectURL: jest.Mock;
let anchorClick: jest.SpyInstance;

beforeEach(() => {
  mockHook.mockReset();
  setFeed();

  writeText = jest.fn((text: string) => {
    void text;
    return Promise.resolve();
  });
  createObjectURL = jest.fn((blob: Blob) => {
    void blob;
    return "blob:polsia-activity-test";
  });
  revokeObjectURL = jest.fn((url: string) => {
    void url;
  });

  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  Object.defineProperty(URL, "createObjectURL", { value: createObjectURL, configurable: true });
  Object.defineProperty(URL, "revokeObjectURL", { value: revokeObjectURL, configurable: true });

  // jsdom has no navigation: capture the download anchor's click instead of dispatching it.
  anchorClick = jest.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  anchorClick.mockRestore();
});

describe("activity strip — the band", () => {
  it.each([
    ["connecting", 0, "CONNECTING…"],
    ["live", 0, "LIVE"],
    ["reconnecting", 2, "RECONNECTING (ATTEMPT 2)"],
    ["offline", 0, "OFFLINE"],
  ] as [FeedStatus, number, string][])(
    "renders the %s status word in the band",
    (status, attempts, word) => {
      setFeed({ status, attempts });
      render(<ActivityStrip />);
      expect(screen.getByRole("region", { name: "Activity strip" })).toHaveTextContent(word);
    },
  );

  it("renders the newest event as one '>' line — action, em dash, summary", () => {
    setFeed({
      status: "live",
      events: [
        ev(2, { action: "deploy", summary: "Shipped the release" }),
        ev(1, { action: "publish", summary: "Drafted the launch post" }),
      ],
    });
    render(<ActivityStrip />);

    const line = screen.getByTestId("strip-latest");
    expect(line).toHaveTextContent("> deploy — Shipped the release");
    expect(line).not.toHaveTextContent("Drafted the launch post");
  });

  it("empty + live shows the waiting copy", () => {
    setFeed({ status: "live" });
    render(<ActivityStrip />);
    expect(screen.getByTestId("strip-empty")).toHaveTextContent("Waiting for activity…");
  });

  it("empty + offline shows the status word only (no waiting copy)", () => {
    setFeed({ status: "offline" });
    render(<ActivityStrip />);
    expect(screen.getByRole("region", { name: "Activity strip" })).toHaveTextContent("OFFLINE");
    expect(screen.queryByTestId("strip-empty")).not.toBeInTheDocument();
  });

  it("a hook error is rendered in the band — never a blank band pretending health", () => {
    setFeed({ status: "reconnecting", attempts: 3, error: "connection closed (code 1006)" });
    render(<ActivityStrip />);
    expect(screen.getByTestId("strip-error")).toHaveTextContent("connection closed (code 1006)");
  });
});

describe("activity log overlay", () => {
  it("opens from the LOG button: dialog appears, focus moves inside, aria-expanded flips", () => {
    setFeed({ status: "live", events: [ev(1)] });
    render(<ActivityStrip />);

    const log = screen.getByTestId("strip-log");
    expect(log).toHaveAttribute("aria-expanded", "false");

    log.focus();
    fireEvent.click(log);

    const dialog = screen.getByRole("dialog", { name: /activity log/i });
    expect(dialog).toBeInTheDocument();
    expect(log).toHaveAttribute("aria-expanded", "true");
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
  });

  it("Esc closes the dialog and focus returns to the LOG button", () => {
    setFeed({ status: "live", events: [ev(1)] });
    render(<ActivityStrip />);
    const log = openLog();

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(log).toHaveAttribute("aria-expanded", "false");
    expect(log).toHaveFocus();
  });

  it("a scrim click closes the dialog; a click inside the panel does not", () => {
    setFeed({ status: "live", events: [ev(1)] });
    render(<ActivityStrip />);
    openLog();

    fireEvent.click(screen.getByRole("dialog"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("log-scrim"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("chips: ALL + sorted distinct actions + OTHER; unknown actions live in OTHER only", () => {
    const deploy = ev(1, { action: "deploy", summary: "Shipped the release" });
    const publish = ev(2, { action: "publish", summary: "Drafted the launch post", level: "success" });
    setFeed({ status: "live", events: [deploy, publish] });
    const { rerender } = render(<ActivityStrip />);
    openLog();

    const chipButtons = within(screen.getByTestId("log-chips")).getAllByRole("button");
    expect(chipButtons.map((b) => b.textContent)).toEqual(["ALL", "deploy", "publish", "OTHER"]);
    expect(screen.getByTestId("chip-ALL")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("log-counter")).toHaveTextContent("2 OF 2");

    // A new event arrives while the log is open, carrying an action that had no chip at open time.
    const mystery = ev(3, { action: "mystery", summary: "Something unlabelled", level: "warning" });
    setFeed({ status: "live", events: [mystery, deploy, publish] });
    rerender(<ActivityStrip />);

    // ALL shows it — an event is never silently hidden.
    expect(rowTexts()).toHaveLength(3);
    expect(screen.getByTestId("log-counter")).toHaveTextContent("3 OF 3");

    fireEvent.click(screen.getByTestId("chip-OTHER"));
    expect(rowTexts()).toHaveLength(1);
    expect(rowTexts()[0]).toContain("mystery");
    expect(screen.getByTestId("log-counter")).toHaveTextContent("1 OF 3");

    fireEvent.click(screen.getByTestId("chip-deploy"));
    expect(rowTexts()).toHaveLength(1);
    expect(rowTexts()[0]).toContain("deploy");
    expect(rowTexts()[0]).not.toContain("mystery");
    expect(screen.getByTestId("chip-deploy")).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByTestId("chip-ALL"));
    expect(rowTexts()).toHaveLength(3);
    expect(screen.getByTestId("log-counter")).toHaveTextContent("3 OF 3");
  });

  it("COPY writes exactly the visible filtered lines", async () => {
    const deploy = ev(1, { action: "deploy", summary: "Shipped the release", level: "info" });
    const publish = ev(2, {
      action: "publish",
      summary: "Drafted the launch post",
      level: "success",
      created_at: "2026-10-14T14:30:00.000Z",
    });
    setFeed({ status: "live", events: [deploy, publish] });
    // Fake timers for this case so the transient COPIED window is controllable end to end.
    jest.useFakeTimers();
    try {
      render(<ActivityStrip />);
      openLog();

      fireEvent.click(screen.getByTestId("log-copy"));
      await act(async () => {}); // flush the mocked clipboard promise

      expect(writeText).toHaveBeenCalledTimes(1);
      const copied = writeText.mock.calls[0][0] as string;

      expect(copied).toBe(
        `${clock(deploy.created_at)} · deploy — Shipped the release · INFO\n` +
          `${clock(publish.created_at)} · publish — Drafted the launch post · SUCCESS`,
      );
      // The ui-gap acceptance: the string equals the rendered visible lines, verbatim.
      expect(copied).toBe(rowTexts().join("\n"));
      // Transient confirmation appears, then clears itself.
      expect(screen.getByTestId("log-copied")).toHaveTextContent("COPIED");
      await act(async () => {
        jest.advanceTimersByTime(COPY_CONFIRM_MS + 1);
      });
      expect(screen.queryByTestId("log-copied")).not.toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("COPY acts on the filtered set (chips first, then copy)", async () => {
    setFeed({
      status: "live",
      events: [
        ev(1, { action: "deploy", summary: "Shipped the release" }),
        ev(2, { action: "publish", summary: "Drafted the launch post", level: "success" }),
      ],
    });
    render(<ActivityStrip />);
    openLog();

    fireEvent.click(screen.getByTestId("chip-publish"));
    fireEvent.click(screen.getByTestId("log-copy"));

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText.mock.calls[0][0] as string).toBe(rowTexts().join("\n"));
    expect(rowTexts()).toHaveLength(1);
  });

  it("DOWNLOAD writes the same lines as a text blob named polsia-activity.log", async () => {
    setFeed({
      status: "live",
      events: [
        ev(1, { action: "deploy", summary: "Shipped the release" }),
        ev(2, { action: "publish", summary: "Drafted the launch post", level: "success" }),
      ],
    });
    render(<ActivityStrip />);
    openLog();

    fireEvent.click(screen.getByTestId("log-download"));

    expect(anchorClick).toHaveBeenCalledTimes(1);
    const anchor = anchorClick.mock.instances[0] as unknown as HTMLAnchorElement;
    expect(anchor.download).toBe("polsia-activity.log");
    expect(anchor.href).toBe("blob:polsia-activity-test");

    const blob = createObjectURL.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("text/plain");
    await expect(blobText(blob)).resolves.toBe(rowTexts().join("\n"));
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:polsia-activity-test");

    // The download acts on the filtered visible set too.
    fireEvent.click(screen.getByTestId("chip-deploy"));
    fireEvent.click(screen.getByTestId("log-download"));
    const filtered = createObjectURL.mock.calls[1][0] as Blob;
    await expect(blobText(filtered)).resolves.toBe(rowTexts().join("\n"));
    expect(rowTexts()).toHaveLength(1);
  });

  it("a null created_at renders the feed's date-unknown wording — no fabricated timestamp", () => {
    setFeed({
      status: "live",
      events: [ev(9, { action: "deploy", summary: "No timestamp on the wire", created_at: null })],
    });
    render(<ActivityStrip />);
    openLog();

    expect(rowTexts()[0]).toContain("date unknown");
    expect(rowTexts()[0]).not.toContain("1970");
  });

  it("levels: a known word keeps the feed's label colour, an unknown word falls back to gray", () => {
    setFeed({
      status: "live",
      events: [
        ev(4, { action: "deploy", summary: "Odd level", level: "mystery" }),
        ev(5, { action: "deploy", summary: "Known level", level: "info", created_at: "2026-10-14T14:40:00.000Z" }),
      ],
    });
    render(<ActivityStrip />);
    openLog();

    const rows = screen.getAllByTestId("log-row");
    expect(within(rows[0]).getByText("MYSTERY")).toHaveClass("text-gray-500");
    expect(within(rows[1]).getByText("INFO")).toHaveClass("text-blue-700");
  });
});
