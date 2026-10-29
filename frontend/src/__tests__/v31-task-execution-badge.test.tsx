/**
 * WAVE 3 / F-07 — the screen half of the partial-run badge.
 *
 * Measured defect (Visuals F-07/F-25): tasks 311/312/314 carried `status: "completed"` while their
 * own envelopes said `[partial]`; every surface (board card, detail sheet, dashboard preview) drew
 * COMPLETED and nothing else. The wave-2 backend half now serves the run's own verdict BESIDE the
 * lifecycle status as two ADDITIVE fields (`execution_status`, `needs_attention`,
 * `backend/app/schemas/task.py:127-142`).
 *
 * These tests pin the UI half:
 *   : a `completed` row with `execution_status: "partial"` renders BOTH the lifecycle chip and a
 *     named PARTIAL chip (and a one-sentence explanation on the detail sheet);
 *   : `refused` and an unknown verdict word render the server's word verbatim, never a friendlier
 *     invented one;
 *   : `needs_attention: true` with no status still renders (the server flagged the row; the reason
 *     is not invented);
 *   : a clean completion renders NO verdict chip at all, so the badge's presence always means
 *     something;
 *   : the lifecycle vocabulary is untouched — `partial`/`refused` are NOT task statuses.
 */
import { render, screen, waitFor } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { ...actual.api, get: jest.fn() } };
});

import { api } from "@/lib/api";
import { TasksPreview } from "@/components/dashboard/TasksPreview";
import { ExecutionVerdictChip } from "@/components/tasks/ExecutionVerdictChip";
import { TaskCard } from "@/components/tasks/TaskCard";
import { TaskDetailContent } from "@/components/tasks/TaskDetailContent";
import { TASK_STATUS_VALUES } from "@/components/tasks/TaskStatusControl";
import { executionVerdict } from "@/components/tasks/status";
import type { TaskRow } from "@/components/tasks/types";

const mockGet = api.get as jest.Mock;

function row(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 311,
    title: "Compare candle vs soap sourcing",
    agent_type: "competitor_research",
    status: "completed",
    priority: 3,
    created_at: "2026-10-28T20:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  mockGet.mockReset();
  // TaskAttachments reads `/tasks/{id}/attachments` over plain fetch; answer the empty list.
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => [],
  }) as unknown as typeof fetch;
});

describe("F-07 — the verdict reader", () => {
  it("names partial and refused, and keeps an unknown word verbatim", () => {
    expect(executionVerdict({ execution_status: "partial" })).toMatchObject({
      label: "PARTIAL",
    });
    expect(executionVerdict({ execution_status: "refused" })).toMatchObject({
      label: "REFUSED",
    });
    const unknown = executionVerdict({ execution_status: "degraded" });
    expect(unknown?.label).toBe("DEGRADED");
    expect(unknown?.explanation).toMatch(/does not know that word/);
  });

  it("renders nothing for a clean completion, and flags attention with no status", () => {
    expect(executionVerdict({ execution_status: null, needs_attention: false })).toBeNull();
    expect(executionVerdict({})).toBeNull();
    expect(executionVerdict({ execution_status: "   ", needs_attention: null })).toBeNull();
    expect(executionVerdict({ needs_attention: true })).toMatchObject({
      label: "NEEDS ATTENTION",
    });
  });

  it("the lifecycle vocabulary is untouched — the verdict is not a new task status", () => {
    expect([...TASK_STATUS_VALUES]).toHaveLength(7);
    expect(TASK_STATUS_VALUES).not.toContain("partial");
    expect(TASK_STATUS_VALUES).not.toContain("refused");
  });
});

describe("F-07 — the badge on the tasks board card", () => {
  it("a completed+partial row draws BOTH the lifecycle chip and the PARTIAL chip", () => {
    render(
      <TaskCard
        task={row({ execution_status: "partial", needs_attention: true })}
        isNext={false}
        onOpen={() => {}}
      />,
    );

    expect(screen.getByText("completed")).toBeInTheDocument();
    const verdict = screen.getByTestId("task-verdict-311");
    expect(verdict).toHaveTextContent("PARTIAL");
    expect(verdict).toHaveAttribute("title", expect.stringContaining("not a clean"));
  });

  it("a clean completion draws no verdict chip", () => {
    render(
      <TaskCard
        task={row({ needs_attention: false, execution_status: null })}
        isNext={false}
        onOpen={() => {}}
      />,
    );

    expect(screen.getByText("completed")).toBeInTheDocument();
    expect(screen.queryByTestId("task-verdict-311")).toBeNull();
  });

  it("attention without a status is still shown", () => {
    render(<TaskCard task={row({ needs_attention: true })} isNext={false} onOpen={() => {}} />);
    expect(screen.getByTestId("task-verdict-311")).toHaveTextContent("NEEDS ATTENTION");
  });
});

describe("F-07 — the badge on the detail sheet", () => {
  it("shows the lifecycle status AND the run's own verdict with its explanation", () => {
    render(<TaskDetailContent task={row({ execution_status: "partial", needs_attention: true })} />);

    // The status control repeats the word in its own current-status line; both are the server's.
    expect(screen.getAllByText("completed").length).toBeGreaterThan(0);
    expect(screen.getByTestId("task-detail-verdict")).toHaveTextContent("PARTIAL");
    const explanation = screen.getByTestId("task-verdict-explanation");
    expect(explanation).toHaveTextContent(/The run's own envelope said PARTIAL/);
    expect(explanation).toHaveTextContent(/not a clean completion/);
  });

  it("a clean completion renders the status only — no verdict, no explanation", () => {
    render(<TaskDetailContent task={row({ execution_status: null, needs_attention: false })} />);

    expect(screen.getAllByText("completed").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("task-detail-verdict")).toBeNull();
    expect(screen.queryByTestId("task-verdict-explanation")).toBeNull();
  });
});

describe("F-07 — the dashboard preview is a task surface too", () => {
  it("marks a partial row beside its status chip", async () => {
    mockGet.mockResolvedValue([
      row({ id: 312, execution_status: "partial", needs_attention: true }),
    ]);
    render(<TasksPreview />);

    await waitFor(() => expect(screen.getByText("Compare candle vs soap sourcing")).toBeInTheDocument());
    expect(screen.getByTestId("preview-verdict-312")).toHaveTextContent("PARTIAL");
    expect(screen.getByText("completed")).toBeInTheDocument();
  });
});

describe("F-07 — the chip component itself", () => {
  it("renders an incomplete row's verdict and nothing for a clean one", () => {
    const { rerender } = render(<ExecutionVerdictChip task={row({ execution_status: "partial" })} />);
    expect(screen.getByText("PARTIAL")).toBeInTheDocument();

    rerender(<ExecutionVerdictChip task={row({ execution_status: null, needs_attention: false })} />);
    expect(screen.queryByText("PARTIAL")).toBeNull();
  });
});
