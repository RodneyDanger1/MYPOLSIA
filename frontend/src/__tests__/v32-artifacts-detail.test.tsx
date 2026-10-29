/**
 * WAVE 4 / F-27 completion — artifacts where the OWNER looks: `/runs/{id}` and `/tasks/{id}`.
 *
 * Wave 3 put the artifact card on `/ops`; this suite pins the wave-4 wiring:
 *   * `/runs/{id}` renders that run's files through the shared reader (`GET /artifacts/{run_id}`),
 *     with the text + sha256/bytes/modified provenance from the byte route's headers;
 *   * a measured `0 files` run says "wrote nothing" — never a spinner, never an error;
 *   * a run with no workspace is the server's named `E_NOT_FOUND`, shown POLITELY (this page owns
 *     its own alert for the ledger read), not as an empty list;
 *   * `/tasks/{id}` lists the task's runs from ONE ledger read (`GET /runs?task_id=`) and expands a
 *     run into the same panel — no fan-out across runs on mount;
 *   * a task with no runs says so from the ledger's measured zero; a failed ledger read is named
 *     with a Retry that re-issues it.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), patch: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import RunDetailPage from "@/app/runs/[id]/page";
import TaskDetailPage from "@/app/tasks/[id]/page";
import type { RunDetailRow } from "@/components/runs/useRunDetail";
import type { TaskRow } from "@/components/tasks/types";

const mockGet = api.get as jest.Mock;

beforeEach(() => {
  mockGet.mockReset();
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: jest.fn(() => "blob:artifact"),
  });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: jest.fn() });
});

afterEach(() => {
  jest.restoreAllMocks();
});

function runDetail(overrides: Partial<RunDetailRow> = {}): RunDetailRow {
  return {
    id: 314,
    task_id: 313,
    task_title: "Label spec for the new candle",
    agent_type: "code_generation",
    run_type: "task",
    status: "completed",
    attempt: 1,
    started_at: "2026-10-28T21:12:00Z",
    ended_at: "2026-10-28T21:12:30Z",
    duration_secs: 30,
    tokens_input: 100,
    tokens_output: 20,
    tokens_used: 120,
    cost_usd: 0.0123,
    cost_source: "token_estimate",
    pricing_version: "2026-10-01",
    num_turns: 2,
    is_error: null,
    tools_used: null,
    ...overrides,
  };
}

function taskRow(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 313,
    title: "Label spec for the new candle",
    agent_type: "code_generation",
    status: "completed",
    priority: 3,
    created_at: new Date(Date.now() - 3600_000).toISOString(),
    ...overrides,
  };
}

const LISTING_314 = {
  run_id: "314",
  items: [
    { path: "notes/label-spec.md", bytes: 4318, modified_at: "2026-10-28T21:12:08Z" },
    { path: "notes/label-checklist.md", bytes: 7055, modified_at: "2026-10-28T21:12:09Z" },
  ],
  total: 2,
  total_bytes: 11373,
  truncated: false,
  skipped_links: 0,
};

const EMPTY_LISTING = {
  run_id: "314",
  items: [],
  total: 0,
  total_bytes: 0,
  truncated: false,
  skipped_links: 0,
};

function contentResponse(text: string, sha256: string, bytes: number): Response {
  return {
    ok: true,
    status: 200,
    text: async () => text,
    headers: new Headers({
      "X-Polsia-Artifact-Sha256": sha256,
      "X-Polsia-Artifact-Bytes": String(bytes),
      "X-Polsia-Artifact-Modified": "2026-10-28T21:12:08Z",
    }),
  } as unknown as Response;
}

// ── /runs/{id} ───────────────────────────────────────────────────────────────────────────────
describe("F-27 — the run page opens the run's artifacts", () => {
  it("lists the run's files and opens one with the server's sha256 + bytes", async () => {
    const user = userEvent.setup();
    mockGet.mockImplementation((path: string) => {
      if (path === "/runs/314") return Promise.resolve(runDetail());
      if (path === "/artifacts/314") return Promise.resolve(LISTING_314);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    global.fetch = jest
      .fn()
      .mockResolvedValue(contentResponse("# Label spec\n\nBody.", "22fcfe35c5e2", 4318)) as unknown as typeof fetch;

    render(<RunDetailPage params={{ id: "314" }} />);

    const card = await screen.findByTestId("run-artifacts");
    expect(await within(card).findByTestId("run-artifacts-count")).toHaveTextContent(
      "2 file(s) · 11.1 KB",
    );
    const file = await within(card).findByTestId("run-artifact-file-0");
    expect(file).toHaveTextContent("notes/label-spec.md");

    await user.click(file);

    expect(await screen.findByTestId("run-artifact-content")).toHaveTextContent("# Label spec");
    const provenance = screen.getByTestId("run-artifact-provenance");
    expect(provenance).toHaveTextContent("22fcfe35c5e2");
    expect(provenance).toHaveTextContent("4.2 KB");

    // The byte read is the frozen content route with the owner key, exactly like the /ops card.
    const call = (global.fetch as jest.Mock).mock.calls[0];
    expect(call[0]).toContain("/api/v1/artifacts/314/content?path=notes%2Flabel-spec.md");
  });

  it("a run that wrote nothing says so — the measured zero, not an error", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/runs/314") return Promise.resolve(runDetail());
      if (path === "/artifacts/314") return Promise.resolve(EMPTY_LISTING);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<RunDetailPage params={{ id: "314" }} />);

    expect(await screen.findByTestId("run-artifacts-empty")).toHaveTextContent(/wrote nothing/);
    expect(screen.queryByTestId("run-artifacts-error")).toBeNull();
  });

  it("a run with no workspace is the named E_NOT_FOUND, shown politely", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/runs/314") return Promise.resolve(runDetail());
      if (path === "/artifacts/314")
        return Promise.reject(
          new ApiError(404, "run 314 has no workspace — nothing was written for it", "E_NOT_FOUND"),
        );
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<RunDetailPage params={{ id: "314" }} />);

    const error = await screen.findByTestId("run-artifacts-error");
    expect(error).toHaveTextContent(/no workspace for this run/);
    expect(error).toHaveTextContent("E_NOT_FOUND");
    // The ledger read succeeded, so this page owns NO alert — the artifacts failure is polite.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a failed artifact walk does not take the ledger row down with it", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/runs/314") return Promise.resolve(runDetail());
      if (path === "/artifacts/314")
        return Promise.reject(new ApiError(503, "the volume is unreachable", "E_STORAGE"));
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<RunDetailPage params={{ id: "314" }} />);

    expect(await screen.findByTestId("run-detail-card")).toBeInTheDocument();
    expect(await screen.findByTestId("run-artifacts-error")).toHaveTextContent("E_STORAGE");
  });
});

// ── /tasks/{id} ──────────────────────────────────────────────────────────────────────────────
function wireTaskPage(reads: {
  task?: unknown;
  attachments?: unknown;
  runs?: unknown;
  artifacts?: unknown;
}) {
  mockGet.mockImplementation((path: string) => {
    if (path === "/tasks/313") {
      return "task" in reads ? Promise.resolve(reads.task) : Promise.resolve(taskRow());
    }
    if (path === "/tasks/313/attachments") return Promise.resolve(reads.attachments ?? []);
    if (path.startsWith("/runs?task_id=313")) {
      return "runs" in reads
        ? Promise.resolve(reads.runs)
        : Promise.resolve({ items: [], total: 0 });
    }
    if (path === "/artifacts/314") {
      return "artifacts" in reads
        ? Promise.resolve(reads.artifacts)
        : Promise.resolve(LISTING_314);
    }
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

const RUN_ROW = {
  id: 314,
  task_id: 313,
  task_title: "Label spec for the new candle",
  agent_type: "code_generation",
  run_type: "task",
  status: "completed",
  attempt: 1,
  started_at: "2026-10-28T21:12:00Z",
  ended_at: "2026-10-28T21:12:30Z",
  duration_secs: 30,
  tokens_input: 100,
  tokens_output: 20,
  tokens_used: 120,
  cost_usd: 0.0123,
  cost_source: "token_estimate",
  pricing_version: "2026-10-01",
};

describe("F-27 — the task page lists its runs and opens their files", () => {
  it("one ledger read lists the task's runs; expanding one reads that run's files", async () => {
    const user = userEvent.setup();
    wireTaskPage({ runs: { items: [RUN_ROW], total: 1 } });

    render(<TaskDetailPage params={{ id: "313" }} />);

    const card = await screen.findByTestId("task-artifacts");
    const runButton = await within(card).findByTestId("task-artifact-run-314");
    expect(runButton).toHaveTextContent("run #314");
    // The link is the full run page (where the SAME panel is mounted).
    expect(within(card).getByTestId("task-artifact-run-link-314")).toHaveAttribute(
      "href",
      "/runs/314",
    );
    // Nothing was read from the artifact routes yet: the expansion is the read (no fan-out).
    expect(mockGet.mock.calls.filter(([path]) => path === "/artifacts/314")).toHaveLength(0);

    await user.click(runButton);

    expect(await within(card).findByTestId("run-artifact-file-0")).toHaveTextContent(
      "notes/label-spec.md",
    );
    expect(mockGet.mock.calls.filter(([path]) => path === "/artifacts/314")).toHaveLength(1);
  });

  it("a task with no runs is a measured zero from the ledger", async () => {
    wireTaskPage({ runs: { items: [], total: 0 } });

    render(<TaskDetailPage params={{ id: "313" }} />);

    expect(await screen.findByTestId("task-artifacts-empty")).toHaveTextContent(
      /No run has run this task yet/,
    );
    expect(mockGet.mock.calls.filter(([path]) => path === "/artifacts/314")).toHaveLength(0);
  });

  it("a failed runs read is named with a Retry that re-issues it", async () => {
    const user = userEvent.setup();
    let reads = 0;
    mockGet.mockImplementation((path: string) => {
      if (path === "/tasks/313") return Promise.resolve(taskRow());
      if (path === "/tasks/313/attachments") return Promise.resolve([]);
      if (path.startsWith("/runs?task_id=313")) {
        reads += 1;
        return reads === 1
          ? Promise.reject(new ApiError(503, "the runs ledger is unreachable", "E_DB"))
          : Promise.resolve({ items: [], total: 0 });
      }
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<TaskDetailPage params={{ id: "313" }} />);

    const error = await screen.findByTestId("task-artifacts-error");
    expect(error).toHaveTextContent("E_DB");
    expect(screen.queryByRole("alert")).toBeNull();

    await user.click(screen.getByTestId("task-artifacts-retry"));

    await waitFor(() => expect(reads).toBe(2));
    expect(await screen.findByTestId("task-artifacts-empty")).toBeInTheDocument();
  });
});
