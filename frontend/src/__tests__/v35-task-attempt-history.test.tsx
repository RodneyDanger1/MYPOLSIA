/**
 * F-41 (wave 7) — the task screen consumes the attempt history the wire has served since A-W4-02.
 *
 * The defect this suite pins (visuals F-41, live `/tasks/313` on the wave-6 rebuild): the task row
 * carried `attempts: 3` and a full `last_attempt_error` record, and the detail screen rendered
 * neither — the retired failure history was write-only from the owner's side. The fix is a
 * consumer, and the acceptance rules are the honest-state rules:
 *
 *   * the record is shown, bounded (a scroll box) and LABELLED "Retired attempt error";
 *   * it is HISTORY — no `role="alert"`, no alert styling, no FAILED badge, and a sentence that
 *     says the task's current status and that the record is not a current failure;
 *   * every number printed is a wire fact (`message_chars`, the counter) — a junk/missing key is
 *     ABSENCE, never a coerced 0, "3", or a fabricated record;
 *   * a live failure (status `failed` + `error_message`) keeps rendering through the existing
 *     Error block, and no attempt history is invented for a row that carries none.
 *
 * The API client is mocked at the boundary only (`@/lib/api`) — the assertions below exercise the
 * real component tree (the sheet the board opens, and the `/tasks/[id]` page).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), patch: jest.fn(), delete: jest.fn() },
  };
});

import { api } from "@/lib/api";
import TaskDetailPage from "@/app/tasks/[id]/page";
import TasksPage from "@/app/tasks/page";
import {
  attemptCount,
  attemptsText,
  hasAttemptHistory,
  retiredAttemptError,
} from "@/components/tasks/attemptHistory";
import type { TaskRow } from "@/components/tasks/types";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

const HOUR_MS = 60 * 60 * 1000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

/** The live `/tasks/313` record, verbatim in shape (the wire the baseline captured). */
const RETIRED = {
  message:
    "PolsiaError: E_AGENT_BAD_OUTPUT: social_media failed: POLSIA-E110 1 validation error for SocialMediaOutput\nposts\n  Value error, twitter content must be <= 280 chars",
  message_chars: 347,
  truncated: false,
  attempts: 3,
  retired_at: "2026-10-29T01:31:00.115Z",
  retired_by: "wave4-taskmaster-live-repair",
  retired_on_status: "completed",
};

function task(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 313,
    title: "Provisional brand copy for a decision-stage home-fragrance brand",
    agent_type: "social_media",
    status: "completed",
    priority: 3,
    created_at: ago(8 * HOUR_MS),
    completed_at: ago(3 * HOUR_MS),
    ...overrides,
  };
}

/** Serve one row on the board and open its detail sheet (the shared `TaskDetailContent`). The
 *  sheet also mounts the attachments + artifacts panels, so those two reads answer their real
 *  empty shapes — a failed attachment read would put ITS OWN alert in the dialog and blur what
 *  this suite is asserting about the task's error rendering. */
async function openSheet(row: TaskRow) {
  mockGet.mockImplementation((path: string) => {
    if (path.endsWith("/attachments")) return Promise.resolve([]);
    if (path.startsWith("/runs?task_id=")) return Promise.resolve({ items: [], total: 0 });
    if (path.startsWith("/tasks/")) return Promise.resolve(row);
    return Promise.resolve([row]);
  });
  const user = userEvent.setup();
  render(<TasksPage />);
  await user.click(await screen.findByRole("button", { name: `Open task: ${row.title}` }));
  return { user, dialog: await screen.findByRole("dialog") };
}

afterEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

// ── the screen consumer ─────────────────────────────────────────────────────────────────────

describe("F-41 — the retired attempt error is rendered as history", () => {
  it("renders the live task-313 shape: attempts, the label, the message and the retirement facts", async () => {
    const { dialog } = await openSheet(task({ attempts: 3, last_attempt_error: RETIRED }));

    const history = within(dialog).getByTestId("task-attempt-history");
    expect(history.textContent).toMatch(/Attempt history/i);
    // The counter is stated, not inferred.
    expect(history.textContent).toMatch(/3 attempts/);
    // The label the acceptance names, verbatim.
    expect(within(history).getByTestId("task-retired-attempt-label")).toHaveTextContent(
      /^Retired attempt error$/,
    );
    // The failure text itself reaches the screen (both of its lines).
    const message = within(history).getByTestId("task-retired-attempt-message");
    expect(message.textContent).toContain("E_AGENT_BAD_OUTPUT");
    expect(message.textContent).toContain("twitter content must be <= 280 chars");
    // The retirement provenance.
    expect(history.textContent).toMatch(/retired when the row moved to completed/);
    expect(history.textContent).toMatch(/wave4-taskmaster-live-repair/);
    expect(history.textContent).toMatch(/the counter when this failure was retired/);
    expect(history.textContent).toMatch(/347 characters stored/);
  });

  it("never presents the history as a failure state: no alert, no alert styling, no FAILED chip", async () => {
    const { dialog } = await openSheet(task({ attempts: 3, last_attempt_error: RETIRED }));

    const history = within(dialog).getByTestId("task-attempt-history");
    expect(within(history).queryByRole("alert")).toBeNull();
    expect(history.querySelector('[role="alert"]')).toBeNull();
    expect(history.querySelector(".text-alert")).toBeNull();
    expect(history.querySelector(".border-alert")).toBeNull();
    expect(history.textContent).not.toMatch(/\bFAILED\b/);
    // And the sentence that keeps the two facts apart: current status, and "not a current failure".
    expect(within(history).getByTestId("task-retired-attempt-note").textContent).toMatch(
      /History, not a current failure/,
    );
    expect(within(history).getByTestId("task-retired-attempt-note").textContent).toMatch(
      /current status is completed/,
    );
    expect(within(history).getByTestId("task-retired-attempt-note").textContent).toMatch(
      /current error is none/,
    );
    // The completed row renders no CURRENT-error section: the sheet's "Error" panel (the one
    // headed by a `ui-label` reading exactly "Error") is absent. The attachments panel can carry
    // ITS OWN alert in jsdom (its blob client uses the global `fetch`, which this environment does
    // not provide) — a different surface, scoped out exactly as u3-task-truth scoped its retry
    // check ("anchored, not loosened").
    const currentErrorLabels = Array.from(dialog.querySelectorAll("p.ui-label")).filter(
      (node) => (node.textContent ?? "").trim() === "Error",
    );
    expect(currentErrorLabels).toHaveLength(0);
  });

  it("states that a row with no recorded record has none — it never invents one", async () => {
    const { dialog } = await openSheet(task({ status: "completed", attempts: 3 }));

    const history = within(dialog).getByTestId("task-attempt-history");
    expect(history.textContent).toMatch(/3 attempts/);
    expect(within(history).getByTestId("task-no-retired-attempt-error").textContent).toMatch(
      /No retired attempt error is recorded on this row/,
    );
    expect(within(history).queryByTestId("task-retired-attempt-message")).toBeNull();
  });

  it("says a retired record with no named status was retired by a later write, not a guess", async () => {
    const { dialog } = await openSheet(
      task({ attempts: 2, last_attempt_error: { ...RETIRED, retired_on_status: null } }),
    );

    expect(
      within(dialog).getByTestId("task-retired-attempt-note").textContent,
    ).toMatch(/retired when a later write finished the row/);
  });

  it("bounds the text and STATES the wire's own cut from the server's numbers", async () => {
    const longMessage = "x".repeat(2000);
    const { dialog } = await openSheet(
      task({
        attempts: 4,
        last_attempt_error: {
          ...RETIRED,
          message: longMessage,
          message_chars: 8400,
          truncated: true,
        },
      }),
    );

    const history = within(dialog).getByTestId("task-attempt-history");
    // The stored length is the server's, not the rendered length.
    expect(history.textContent).toMatch(/8,400 characters stored/);
    expect(history.textContent).toMatch(/the task read cut the text at 2,000 characters/);
    // Bounded on screen by a scroll box — not a second truncation of the bytes.
    const message = within(history).getByTestId("task-retired-attempt-message");
    expect(message.className).toMatch(/max-h-48/);
    expect(message.className).toMatch(/overflow-auto/);
    expect(message.textContent).toBe(longMessage);
  });

  it("keeps a live failure as the CURRENT error: Error block present, no invented history", async () => {
    const { dialog } = await openSheet(
      task({ status: "failed", error_message: "POLSIA-E110: run failed", completed_at: null }),
    );

    // The existing Error section still renders the current failure…
    expect(dialog.textContent).toMatch(/POLSIA-E110: run failed/);
    // …and no attempt-history section is invented for a row the wire gave no history.
    expect(within(dialog).queryByTestId("task-attempt-history")).toBeNull();
  });

  it("renders BOTH when a hostile payload carries a current error and a retired record", async () => {
    const { dialog } = await openSheet(
      task({ status: "failed", error_message: "current failure", last_attempt_error: RETIRED }),
    );

    const history = within(dialog).getByTestId("task-attempt-history");
    expect(within(history).getByTestId("task-retired-attempt-note").textContent).toMatch(
      /a current error is recorded separately below/,
    );
    expect(dialog.textContent).toMatch(/current failure/);
  });
});

describe("F-41 — absence and junk are absence, never fabricated facts", () => {
  it("renders no history section for an ordinary single-delivery row (attempts 1, no record)", async () => {
    const { dialog } = await openSheet(task({ attempts: 1 }));

    expect(within(dialog).queryByTestId("task-attempt-history")).toBeNull();
  });

  it("renders no history section when the server carries neither key (old payload)", async () => {
    const { dialog } = await openSheet(task({ status: "pending" }));

    expect(within(dialog).queryByTestId("task-attempt-history")).toBeNull();
  });

  it.each([
    ["a list", ["not", "a", "record"]],
    ["a string", "POLSIA-E110"],
    ["a number", 7],
    ["an object with no message", { attempts: 3, retired_at: "2026-10-29T01:31:00.115Z" }],
    ["an object with an empty message", { message: "", message_chars: 0, truncated: false }],
    ["an object with a non-string message", { message: 42, message_chars: 2, truncated: false }],
  ])("treats %s as no record (no crash, no invented failure)", async (_label, raw) => {
    const { dialog } = await openSheet(
      task({ attempts: 3, last_attempt_error: raw as TaskRow["last_attempt_error"] }),
    );

    const history = within(dialog).getByTestId("task-attempt-history");
    expect(within(history).getByTestId("task-no-retired-attempt-error")).toBeInTheDocument();
    expect(within(history).queryByTestId("task-retired-attempt-message")).toBeNull();
    expect(dialog.textContent ?? "").not.toMatch(/NaN/);
  });

  it("never coerces a non-integer counter into a number on screen", async () => {
    const { dialog } = await openSheet(
      task({
        attempts: "3" as unknown as number,
        // A hand-edited bag can carry any JSON: the junk counter/message_chars are typed `unknown`
        // by the consumer, so the cast is on the FIXTURE (a hostile payload), not on the reader.
        last_attempt_error: {
          ...RETIRED,
          attempts: "3",
          message_chars: "347",
        } as unknown as TaskRow["last_attempt_error"],
      }),
    );

    const history = within(dialog).getByTestId("task-attempt-history");
    // The junk counter is absence: no "3 attempts" line, and no "Counter then" row.
    expect(history.textContent).not.toMatch(/3 attempts/);
    expect(history.textContent).not.toMatch(/Counter then/);
    // `message_chars` junk falls back to the rendered length — the message is a fact, the bad count
    // is not repeated.
    expect(history.textContent).toMatch(new RegExp(`${RETIRED.message.length} characters stored`));
  });
});

// ── the /tasks/[id] route (the addressable detail) ───────────────────────────────────────────

describe("F-41 — the same consumer on the addressable /tasks/[id] page", () => {
  it("renders the attempt history from a real GET /tasks/313 shape", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/tasks/313") return Promise.resolve(task({ attempts: 3, last_attempt_error: RETIRED }));
      if (path.startsWith("/runs?task_id=313")) return Promise.resolve({ items: [], total: 0 });
      if (path.endsWith("/attachments")) return Promise.resolve([]);
      return Promise.reject(new Error(`unexpected read: ${path}`));
    });

    render(<TaskDetailPage params={{ id: "313" }} />);

    const history = await screen.findByTestId("task-attempt-history");
    expect(history.textContent).toMatch(/Retired attempt error/);
    expect(history.textContent).toMatch(/3 attempts/);
    await waitFor(() =>
      expect(screen.getByTestId("task-retired-attempt-message").textContent).toContain(
        "E_AGENT_BAD_OUTPUT",
      ),
    );
  });
});

// ── the pure narrowing units ─────────────────────────────────────────────────────────────────

describe("F-41 — attemptHistory units", () => {
  it("accepts only non-negative integers as a count (0 is a measured zero)", () => {
    expect(attemptCount(0)).toBe(0);
    expect(attemptCount(3)).toBe(3);
    for (const value of [-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, "3", true, null, undefined]) {
      expect(attemptCount(value)).toBeNull();
    }
  });

  it("formats the counter without inventing a value", () => {
    expect(attemptsText(0)).toBe("0 attempts — no delivery recorded");
    expect(attemptsText(1)).toBe("1 attempt");
    expect(attemptsText(3)).toBe("3 attempts");
    expect(attemptsText("3")).toBeNull();
    expect(attemptsText(null)).toBeNull();
  });

  it("derives `truncated` from the stored length when the flag is absent", () => {
    const record = retiredAttemptError({
      last_attempt_error: { message: "abc", message_chars: 4000 },
    });
    expect(record).not.toBeNull();
    expect(record?.truncated).toBe(true);
    expect(record?.messageChars).toBe(4000);
    // Junk `message_chars` (smaller than the rendered text) is not repeated as the stored length.
    expect(
      retiredAttemptError({ last_attempt_error: { message: "abcdef", message_chars: 2 } })
        ?.messageChars,
    ).toBe(6);
  });

  it("counts a single delivery as no history and a record as history", () => {
    expect(hasAttemptHistory({ attempts: 1 })).toBe(false);
    expect(hasAttemptHistory({ attempts: 0 })).toBe(false);
    expect(hasAttemptHistory({ attempts: 2 })).toBe(true);
    expect(hasAttemptHistory({ last_attempt_error: RETIRED })).toBe(true);
    expect(hasAttemptHistory({})).toBe(false);
    expect(hasAttemptHistory(null)).toBe(false);
  });
});
