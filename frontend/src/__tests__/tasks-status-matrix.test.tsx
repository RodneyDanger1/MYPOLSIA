import fs from "node:fs";
import path from "node:path";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

/**
 * B2-C5-R1 (carry) — the status control must offer the server's TRANSITION MATRIX, not the whole
 * wire enum.
 *
 * Measured defect at HEAD: `TaskStatusControl` rendered all seven `TaskStatus` values from every
 * current status and its docstring claimed "there is NO transition matrix in the service". F-17
 * (wave B1) made that false: `task_service.TASK_TRANSITIONS` is the one authority and
 * `PATCH /tasks/{id}/status` refuses an illegal cell with `E_TASK_NOT_RUNNABLE` (409).
 *
 * Pins: (1) the browser mirror equals the parsed backend matrix, status by status, so drift fails
 * here; (2) a `pending` row offers exactly its five legal targets and never offers its own status;
 * (3) a terminal row offers no control and says why; (4) a 409's `context.accepted` REPLACES the
 * offered options — the server, not the mirror, has the last word.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), patch: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import {
  LEGAL_STATUS_TARGETS,
  TASK_STATUS_VALUES,
  TaskStatusControl,
  legalTaskStatusTargets,
} from "@/components/tasks/TaskStatusControl";
import type { TaskRow } from "@/components/tasks/types";

const mockPatch = (api as unknown as { patch: jest.Mock }).patch;

const ROOT = process.cwd();

/** Parse `TASK_TRANSITIONS` out of the service: `{ pending: ["cancelled", ...], ... }`.
 *
 *  Line-oriented on purpose: the tuple entries carry trailing `#` comments that name the writer,
 *  and a status can be declared empty inline (`TaskStatus.COMPLETED: (),`). A regex over the
 *  whole block either swallows the comment or misses the empty tuple — a parser this small is
 *  easier to keep honest than a clever one.
 */
function backendMatrix(): Record<string, string[]> {
  const source = fs.readFileSync(
    path.join(ROOT, "../backend/app/services/task_service.py"),
    "utf8",
  );
  const start = source.indexOf("TASK_TRANSITIONS: dict");
  const end = source.indexOf("TASK_TRANSITION_WRITERS");
  expect(start).toBeGreaterThan(-1);
  expect(end).toBeGreaterThan(start);
  const block = source.slice(start, end);

  const matrix: Record<string, string[]> = {};
  let open: string | null = null;
  for (const line of block.split(/\r?\n/)) {
    const empty = line.match(/^\s{4}TaskStatus\.([A-Z_]+):\s*\(\s*\),\s*$/);
    if (empty) {
      matrix[empty[1].toLowerCase()] = [];
      open = null;
      continue;
    }
    const header = line.match(/^\s{4}TaskStatus\.([A-Z_]+):\s*\(\s*$/);
    if (header) {
      open = header[1].toLowerCase();
      matrix[open] = [];
      continue;
    }
    if (open && /^\s{4}\),\s*$/.test(line)) {
      open = null;
      continue;
    }
    if (open) {
      const target = line.match(/TaskStatus\.([A-Z_]+),/);
      if (target) matrix[open].push(target[1].toLowerCase());
    }
  }
  return matrix;
}

function row(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 5,
    title: "Status target",
    agent_type: "orchestrator",
    status: "pending",
    priority: 3,
    created_at: "2026-10-25T10:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  mockPatch.mockReset();
});

describe("B2-C5-R1 — the mirror is pinned to the backend matrix", () => {
  it("parses all seven statuses and matches the browser mirror exactly", () => {
    const matrix = backendMatrix();
    expect(Object.keys(matrix).sort()).toEqual([...TASK_STATUS_VALUES].sort());
    for (const status of TASK_STATUS_VALUES) {
      expect({ status, targets: [...legalTaskStatusTargets(status)] }).toEqual({
        status,
        targets: [...matrix[status]].sort(),
      });
    }
    // And the exported table has no extra keys the backend does not declare.
    expect(Object.keys(LEGAL_STATUS_TARGETS).sort()).toEqual([...TASK_STATUS_VALUES].sort());
  });

  it("answers the service's own empty tuple for a terminal or unrecorded status", () => {
    expect(legalTaskStatusTargets("completed")).toEqual([]);
    expect(legalTaskStatusTargets("cancelled")).toEqual([]);
    expect(legalTaskStatusTargets("rejected")).toEqual([]);
    expect(legalTaskStatusTargets(null)).toEqual([]);
    expect(legalTaskStatusTargets("no_such_status")).toEqual([]);
  });
});

describe("B2-C5-R1 — the control offers only legal writes", () => {
  it("a pending row offers its five targets, never its own status", () => {
    render(<TaskStatusControl task={row({ status: "pending" })} />);

    const select = screen.getByLabelText("New status") as HTMLSelectElement;
    expect([...select.options].map((option) => option.value)).toEqual([
      "",
      "cancelled",
      "completed",
      "failed",
      "in_progress",
      "rejected",
    ]);
    expect([...select.options].some((option) => option.value === "pending")).toBe(false);
    expect(screen.getByTestId("status-current")).toHaveTextContent("pending");
    // The old, false sentence is gone from the surface.
    expect(document.body.textContent ?? "").not.toMatch(/NO transition matrix|exactly these seven/i);
  });

  it("a completed row offers nothing and says the state is terminal", () => {
    render(<TaskStatusControl task={row({ status: "completed" })} />);

    expect(screen.queryByLabelText("New status")).toBeNull();
    expect(screen.getByTestId("status-no-target")).toHaveTextContent(/terminal state/);
    expect(screen.getByTestId("status-no-target")).toHaveTextContent(/refuses every status write/);
  });

  it("an unrecorded status offers nothing and names why", () => {
    render(<TaskStatusControl task={row({ status: null })} />);

    expect(screen.queryByLabelText("New status")).toBeNull();
    expect(screen.getByTestId("status-no-target")).toHaveTextContent(/no recorded status/);
    expect(screen.getByTestId("status-current")).toHaveTextContent("not recorded");
  });

  it("a 409 whose context names the accepted set replaces the offered options", async () => {
    mockPatch.mockRejectedValueOnce(
      new ApiError(
        409,
        "illegal task transition pending -> completed",
        "E_TASK_NOT_RUNNABLE",
        "pick a legal target",
        {
          detail: {
            code: "E_TASK_NOT_RUNNABLE",
            message: "illegal task transition pending -> completed",
            context: { from: "pending", to: "completed", accepted: ["cancelled"] },
          },
        },
      ),
    );

    render(<TaskStatusControl task={row({ status: "pending" })} />);
    fireEvent.change(screen.getByLabelText("New status"), { target: { value: "completed" } });
    fireEvent.click(screen.getByRole("button", { name: "Set status" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_TASK_NOT_RUNNABLE");

    await waitFor(() => {
      const select = screen.getByLabelText("New status") as HTMLSelectElement;
      expect([...select.options].map((option) => option.value)).toEqual(["", "cancelled"]);
    });
    expect(mockPatch).toHaveBeenCalledWith("/tasks/5/status", { status: "completed" });
  });
});
