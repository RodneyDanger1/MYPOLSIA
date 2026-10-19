import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn() },
  };
});

import { api } from "@/lib/api";
import TasksPage from "@/app/tasks/page";
import { recoveryFor } from "@/components/tasks/recovery";
import { costUsdText, tokenText } from "@/components/tasks/runTruth";
import type { TaskRow } from "@/components/tasks/types";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

const FINGERPRINT = "9f2c1e4a5b6d7c8e";
const PARKED_SUMMARY = `awaiting approval: 1 gated action(s) need a token; request one for fingerprint(s) ${FINGERPRINT}`;

function task(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 1,
    title: "Task one",
    agent_type: "orchestrator",
    status: "pending",
    priority: 3,
    created_at: ago(2 * DAY_MS),
    ...overrides,
  };
}

/** Serve one fixture row, render the board and open its detail sheet. */
async function openSheet(row: TaskRow) {
  mockGet.mockResolvedValue([row]);
  const user = userEvent.setup();
  render(<TasksPage />);
  await user.click(await screen.findByRole("button", { name: `Open task: ${row.title}` }));
  return { user, dialog: await screen.findByRole("dialog") };
}

afterEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

// ---------------------------------------------------------------------------------------------
// B4 — run truth: the measured values, rendered honestly (cost null vs 0 vs sub-cent).
// ---------------------------------------------------------------------------------------------

describe("run truth (B4)", () => {
  it("renders the measured cost, tokens and completion stamp verbatim", async () => {
    const row = task({
      id: 7,
      title: "Measured run",
      status: "completed",
      completed_at: ago(3 * HOUR_MS),
      cost_usd: 0.0042,
      tokens_in: 1234,
      tokens_out: 567,
    });

    const { dialog } = await openSheet(row);
    const truth = within(dialog).getByTestId("run-truth");

    // The exact wire values, formatted — sub-cent cost keeps its 4 decimals (never $0.00).
    expect(within(truth).getByText("$0.0042")).toBeInTheDocument();
    expect(within(truth).getByText("1,234")).toBeInTheDocument();
    expect(within(truth).getByText("567")).toBeInTheDocument();
    expect(within(truth).getByText("3h ago")).toBeInTheDocument();
    expect(within(truth).queryByText(/not measured/i)).toBeNull();
    // Labelled as measured, with no pricing claim attached.
    expect(truth.textContent).toMatch(/no estimate and no pricing is applied here/);
  });

  it("renders a measured zero as $0.00 (a real zero IS a value — never the dash)", async () => {
    const row = task({
      id: 8,
      title: "Zero-cost run",
      status: "completed",
      completed_at: ago(DAY_MS),
      cost_usd: 0,
      tokens_in: 0,
      tokens_out: 0,
    });

    const { dialog } = await openSheet(row);
    const truth = within(dialog).getByTestId("run-truth");

    expect(within(truth).getByText("$0.00")).toBeInTheDocument();
    // Both token rows carry the measured zero — two exact "0" text nodes, not the dash.
    expect(within(truth).getAllByText("0")).toHaveLength(2);
    // The unknown-reason string must NOT appear when a value was recorded.
    expect(within(truth).queryByText(/not measured/i)).toBeNull();
  });

  it("renders a null cost as the not-measured dash WITH its reason — never $0.00 for unknown", async () => {
    const row = task({
      id: 9,
      title: "Unmeasured run",
      status: "completed",
      completed_at: ago(DAY_MS),
      cost_usd: null,
    });

    const { dialog } = await openSheet(row);
    const truth = within(dialog).getByTestId("run-truth");

    expect(
      within(truth).getByText("— not measured — the server recorded no run totals for this task"),
    ).toBeInTheDocument();
    // The whole sheet prints no currency at all for an unmeasured task.
    expect(dialog.textContent ?? "").not.toContain("$");
    // Null tokens render no token rows at all (values "when present").
    expect(within(truth).queryByText(/^Tokens in$/)).toBeNull();
    expect(within(truth).queryByText(/^Tokens out$/)).toBeNull();
  });

  it("treats keys absent from the wire exactly like null (old payload, no crash)", async () => {
    // The row never carries cost_usd / tokens_in / tokens_out — an old-shaped payload.
    const row = task({ id: 10, title: "Old payload", status: "failed" });

    const { dialog } = await openSheet(row);
    const truth = within(dialog).getByTestId("run-truth");

    expect(within(truth).getByText(/not measured/)).toBeInTheDocument();
    expect(truth.textContent ?? "").not.toContain("$");
  });

  it("refuses to format a non-numeric cost as money (defensive, honest)", async () => {
    const row = task({ id: 11, title: "Bad cost", cost_usd: "0.0042" as unknown as number });

    const { dialog } = await openSheet(row);
    const truth = within(dialog).getByTestId("run-truth");

    expect(within(truth).getByText(/not measured/)).toBeInTheDocument();
    expect(truth.textContent ?? "").not.toContain("$");
    expect(truth.textContent ?? "").not.toMatch(/NaN/);
  });

  it("says a completed task's missing completion stamp is not recorded — never a fake date", async () => {
    const row = task({ id: 12, title: "Completed, no stamp", status: "completed", completed_at: null });

    const { dialog } = await openSheet(row);
    const truth = within(dialog).getByTestId("run-truth");

    expect(within(truth).getByText("— not recorded")).toBeInTheDocument();
    expect(within(dialog).queryByText(/Invalid Date/)).toBeNull();
  });

  it("keeps relativeTime/the absolute stamp on a recorded completion", async () => {
    const completedAt = ago(2 * HOUR_MS);
    const row = task({ id: 13, title: "Stamped", status: "completed", completed_at: completedAt });

    const { dialog } = await openSheet(row);
    const truth = within(dialog).getByTestId("run-truth");

    expect(within(truth).getByText("2h ago")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------------------------
// B4 — the formatter units (pure).
// ---------------------------------------------------------------------------------------------

describe("costUsdText / tokenText units", () => {
  it("formats finite costs with at least 2 and at most 4 exact decimals", () => {
    expect(costUsdText(0)).toBe("$0.00");
    expect(costUsdText(0.0042)).toBe("$0.0042");
    expect(costUsdText(0.01)).toBe("$0.01");
    expect(costUsdText(1.5)).toBe("$1.50");
    expect(costUsdText(2)).toBe("$2.00");
    expect(costUsdText(12.345)).toBe("$12.345");
  });

  it("answers null for every unmeasured / non-numeric input (never a fabricated value)", () => {
    expect(costUsdText(null)).toBeNull();
    expect(costUsdText(undefined)).toBeNull();
    expect(costUsdText(Number.NaN)).toBeNull();
    expect(costUsdText(Number.POSITIVE_INFINITY)).toBeNull();
    expect(costUsdText("1.00" as unknown as number)).toBeNull();
  });

  it("formats token counts with explicit en-US grouping, null when unmeasured", () => {
    expect(tokenText(1234)).toBe("1,234");
    expect(tokenText(0)).toBe("0");
    expect(tokenText(null)).toBeNull();
    expect(tokenText(undefined)).toBeNull();
    expect(tokenText(Number.NaN)).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// B3 — exactly ONE truthful recovery action per state, in a table the tests pin.
// ---------------------------------------------------------------------------------------------

describe("recovery grammar (B3)", () => {
  it("maps each state to exactly one action or a stated reason (the pinned table)", () => {
    expect(recoveryFor(task({ status: "pending" }))).toMatchObject({ kind: "run", label: "Run now" });
    expect(recoveryFor(task({ status: "failed" }))).toMatchObject({ kind: "run", label: "Run again" });
    expect(recoveryFor(task({ status: "completed" }))).toMatchObject({ kind: "none" });
    expect(recoveryFor(task({ status: "in_progress" }))).toMatchObject({ kind: "none" });
    expect(recoveryFor(task({ status: "rejected" }))).toMatchObject({ kind: "none" });
    expect(recoveryFor(task({ status: "cancelled" }))).toMatchObject({ kind: "none" });
    expect(recoveryFor(task({ status: null }))).toMatchObject({ kind: "none" });
    expect(recoveryFor(task({ status: "weird_future_status" }))).toMatchObject({ kind: "none" });

    // The parked state splits on the readable fingerprint — approve only when one exists.
    expect(recoveryFor(task({ status: "awaiting_approval", result_summary: PARKED_SUMMARY }))).toMatchObject({
      kind: "approve",
    });
    expect(
      recoveryFor(task({ status: "awaiting_approval", result_summary: "no fingerprint here" })),
    ).toMatchObject({ kind: "none" });
  });

  it("states, before the click, that the run creates a NEW task and spends", () => {
    const pending = recoveryFor(task({ status: "pending" }));
    const failed = recoveryFor(task({ status: "failed" }));
    for (const recovery of [pending, failed]) {
      if (recovery.kind !== "run") throw new Error("expected a run recovery");
      expect(recovery.helper).toMatch(/NEW task/);
      expect(recovery.helper).toMatch(/spends/);
      expect(recovery.helper).not.toMatch(/retry/i);
    }
  });
});

// ---------------------------------------------------------------------------------------------
// B3 — the sheet renders at most ONE action control for EVERY state (the exactly-one rule).
// ---------------------------------------------------------------------------------------------

describe("recovery in the sheet (B3)", () => {
  const STATES: Array<[string, Partial<TaskRow>, number, RegExp | null, RegExp | null]> = [
    //  label, fixture overrides, expected glossy count, action button pattern, reason pattern
    ["pending", { status: "pending" }, 1, /run now/i, null],
    ["failed", { status: "failed" }, 1, /run again/i, null],
    [
      "awaiting_approval with a fingerprint",
      { status: "awaiting_approval", result_summary: PARKED_SUMMARY },
      1,
      /approve/i,
      null,
    ],
    ["completed", { status: "completed", completed_at: ago(DAY_MS) }, 0, null, /no recovery action/i],
    [
      "awaiting_approval without a readable fingerprint",
      { status: "awaiting_approval", result_summary: null },
      0,
      null,
      /no fingerprint this build can read/i,
    ],
    ["in_progress", { status: "in_progress" }, 0, null, /already underway/i],
    ["rejected", { status: "rejected" }, 0, null, /refused before execution/i],
    ["cancelled", { status: "cancelled" }, 0, null, /no restart control/i],
    ["unknown status", { status: null }, 0, null, /no truthful action can be named/i],
    ["unrecognised status word", { status: "weird_future_status" }, 0, null, /no control is drawn/i],
  ];

  it.each(STATES)(
    "%s: exactly one action (or a stated reason), no competing CTAs",
    async (label, overrides, expectedCount, actionPattern, reasonPattern) => {
      const row = task({ id: 100, title: `State: ${label}`, ...overrides });

      const { dialog } = await openSheet(row);

      // The invariant this item exists for: never two action controls on one card.
      expect(dialog.querySelectorAll(".btn-glossy").length).toBe(expectedCount);

      if (actionPattern) {
        expect(within(dialog).getByRole("button", { name: actionPattern })).toBeInTheDocument();
        expect(dialog.querySelector('[data-recovery="run"], [data-recovery="approve"]')).not.toBeNull();
      } else {
        // No run/retry control of ANY kind — and the reason is on screen instead.
        //
        // wave V25 (L1): the task sheet now hosts an IMAGE-ATTACHMENT panel, which owns its own
        // "Retry" when the blob read fails.  That control belongs to the attachment surface, not
        // to task recovery, so the invariant is scoped to the `[data-recovery="none"]` block: the
        // recovery area must never offer a competing run/retry CTA.  (Anchored, not loosened —
        // the attachment panel's own Retry is covered by v25-vision.test.tsx.)
        const recoveryNone = dialog.querySelector('[data-recovery="none"]');
        expect(recoveryNone).not.toBeNull();
        expect(
          within(recoveryNone as HTMLElement).queryByRole("button", {
            name: /run now|run again|retry/i,
          }),
        ).toBeNull();
        expect(dialog.textContent ?? "").toMatch(reasonPattern as RegExp);
      }
    },
  );

  it("failed: Run again posts the real trigger endpoint with the task title", async () => {
    mockPost.mockResolvedValueOnce({
      status: "queued",
      task_id: 88,
      message: "Task #88 queued for code_generation.",
    });
    const row = task({
      id: 21,
      title: "Broken run",
      agent_type: "code_generation",
      status: "failed",
    });

    const { user, dialog } = await openSheet(row);
    await user.click(within(dialog).getByRole("button", { name: /run again/i }));

    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith(
      "/agents/code_generation/trigger",
      expect.objectContaining({ task_title: "Broken run" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("Task #88 queued for code_generation.");
  });

  it("completed: the live product's RETRY-on-a-done-task defect is not copied", async () => {
    const row = task({
      id: 22,
      title: "Finished work",
      status: "completed",
      completed_at: ago(DAY_MS),
    });

    const { dialog } = await openSheet(row);

    expect(dialog.querySelectorAll(".btn-glossy")).toHaveLength(0);
    // wave V25 (L1): scope the recovery invariant to the `[data-recovery="none"]` block — the
    // attachment panel's own failed-read Retry is a different surface (pinned by v25-vision).
    const recoveryNone = dialog.querySelector('[data-recovery="none"]');
    expect(recoveryNone).not.toBeNull();
    expect(within(dialog).queryByRole("button", { name: /run/i })).toBeNull();
    expect(
      within(recoveryNone as HTMLElement).queryByRole("button", { name: /retry/i }),
    ).toBeNull();
    expect((recoveryNone as HTMLElement).textContent ?? "").not.toMatch(/\bretry\b/i);
    expect(recoveryNone?.textContent ?? "").toMatch(/no recovery action/i);
  });

  it("awaiting_approval: approve is the ONLY primary and no new-run button competes with it", async () => {
    const row = task({
      id: 23,
      title: "Green light",
      status: "awaiting_approval",
      result_summary: PARKED_SUMMARY,
    });

    const { dialog } = await openSheet(row);

    expect(dialog.querySelectorAll(".btn-glossy")).toHaveLength(1);
    expect(within(dialog).getByRole("button", { name: /approve/i })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /run now|run again/i })).toBeNull();
  });
});
