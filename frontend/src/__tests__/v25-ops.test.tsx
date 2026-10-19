/**
 * WAVE V25 / lane L5 — the `/ops` "God Mode" cockpit.
 *
 * RED-first: this suite was written before `src/app/ops/page.tsx` existed (the honest RED is the
 * missing module) and pins the whole owner-override surface against the SHIPPING API only:
 *
 *   GET  /runs?limit=20                       live ledger page (id, task, agent, status, cost, duration)
 *   GET  /runs/{id}                           detail-only `tools_used` (null | [] | names)
 *   GET  /tasks?status=awaiting_approval      the parked rows
 *   POST /approvals                           approve one fingerprint (mint + attach + re-queue)
 *   PATCH /tasks/{id}/status                  deny a parked task (status=rejected)
 *   GET  /agents/status                       the nine agents + trigger targets
 *   POST /agents/{type}/trigger               manual dispatch (creates a NEW task row)
 *   GET  /auto-mode, GET /config, GET /ops/sandbox   the kill-switch / auto-mode readings
 *
 * What is pinned, in order:
 *   1. loading is a skeleton, never an empty table;
 *   2. the runs table renders every required column, with `tools_used` chips from the detail read
 *      and BOTH honest absence states (`null` = never recorded, `[]` = recorded and none);
 *   3. empty states are explicit on every panel;
 *   4. a failed runs read is named AND has a working Retry;
 *   5. Approve posts the real mint+attach body and names the outcome;
 *   6. Deny is a two-step write to `PATCH /tasks/{id}/status` (rejected), never a dead button;
 *   7. an agent trigger disables in flight and renders the server's refusal BY NAME;
 *   8. auto-mode / dispatch-pause / sandbox are read, not guessed;
 *   9. polling is visibility-aware: hidden ticks issue nothing, becoming visible re-reads at once.
 *
 * House mocking pattern: keep the real `@/lib/api` classes, replace only the `api` object.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), patch: jest.fn(), delete: jest.fn() },
  };
});

import { act, fireEvent, render, screen } from "@testing-library/react";

import OpsPage from "@/app/ops/page";
import { OPS_POLL_MS } from "@/hooks/useVisibilityPoll";
import { ApiError, api } from "@/lib/api";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;
const mockPatch = api.patch as jest.Mock;

const RUNS_PATH = "/runs?limit=20";
const APPROVALS_PATH = "/tasks?status=awaiting_approval&limit=50";

/** Flush the microtask/timer chain that mount reads + detail enrichment produce. */
async function flush(times = 10) {
  await act(async () => {
    for (let i = 0; i < times; i += 1) await Promise.resolve();
  });
}

function runRow(id: number, overrides: Record<string, unknown> = {}) {
  return {
    id,
    task_id: 40 + id,
    task_title: `Ledger task ${id}`,
    agent_type: "competitor_research",
    run_type: "task",
    status: "completed",
    attempt: 1,
    started_at: "2026-10-19T11:00:00Z",
    ended_at: "2026-10-19T11:02:05Z",
    duration_secs: 125,
    tokens_input: 10,
    tokens_output: 5,
    tokens_used: 15,
    cost_usd: 0.0123,
    cost_source: "token_estimate",
    pricing_version: "2026-10-01",
    ...overrides,
  };
}

const PENDING_FINGERPRINT = "fp12345678";

const PENDING_TASK = {
  id: 3,
  title: "Review the campaign budget",
  description: null,
  agent_type: "ads_management",
  priority: 2,
  status: "awaiting_approval",
  source: "cycle",
  scheduled_date: null,
  result_summary: `awaiting approval: 1 gated action(s) need a token; request one for fingerprint(s) ${PENDING_FINGERPRINT}`,
  error_message: null,
  completed_at: null,
  cost_usd: null,
  tokens_in: null,
  tokens_out: null,
  created_at: "2026-10-19T10:00:00Z",
};

const AGENTS = [
  {
    agent_type: "competitor_research",
    last_run_at: "2026-10-19T10:00:00Z",
    last_run_status: "completed",
    tasks_today: 2,
    tasks_total: 9,
  },
  {
    agent_type: "content",
    last_run_at: null,
    last_run_status: null,
    tasks_today: 0,
    tasks_total: 0,
  },
];

/** The default honest wire — every GET the page legitimately makes answers its real shape. */
function wire() {
  mockGet.mockImplementation((path: string) => {
    if (path === RUNS_PATH) {
      return Promise.resolve({
        items: [
          runRow(7),
          runRow(8, { status: "failed", cost_usd: null, duration_secs: null }),
          runRow(9, { status: "in_progress", ended_at: null }),
        ],
        total: 3,
      });
    }
    if (path === "/runs/7") {
      return Promise.resolve({ ...runRow(7), tools_used: ["web_search", "code_workspace"] });
    }
    if (path === "/runs/8") {
      return Promise.resolve({ ...runRow(8), tools_used: null });
    }
    if (path === "/runs/9") {
      return Promise.resolve({ ...runRow(9), tools_used: [] });
    }
    if (path === APPROVALS_PATH) return Promise.resolve([PENDING_TASK]);
    if (path === "/agents/status") return Promise.resolve(AGENTS);
    if (path === "/auto-mode") {
      return Promise.resolve({
        active: true,
        session: {
          id: 1,
          started_at: "2026-10-19T10:00:00Z",
          ends_at: "2026-10-19T11:30:00Z",
          stopped_at: null,
          stop_reason: null,
          tasks_run: 4,
          spend_used: 1.25,
          time_left_seconds: 5400,
        },
      });
    }
    if (path === "/config") return Promise.resolve({ dispatch_paused: true });
    if (path === "/ops/sandbox") return Promise.resolve({ sandbox_mode: true });
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPatch.mockReset();
  wire();
});

afterEach(async () => {
  // Let the page's in-flight detail reads settle inside act before the next test — otherwise the
  // unmount races the mock promises and React logs act() warnings that belong to the harness, not
  // to a product defect.
  await flush();
  jest.useRealTimers();
});

describe("/ops — the live runs cockpit", () => {
  it("shows a busy skeleton before the first ledger read lands", () => {
    render(<OpsPage />);
    expect(screen.getByTestId("ops-runs-loading")).toBeInTheDocument();
    expect(screen.queryByText("No runs recorded yet")).toBeNull();
  });

  it("renders id, task, agent, status, tools_used chips, cost and duration", async () => {
    render(<OpsPage />);

    expect(await screen.findByText("Ledger task 7")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open run 7" })).toHaveTextContent("#7");
    expect(screen.getAllByText("competitor_research").length).toBeGreaterThan(0);
    expect(screen.getAllByText("completed").length).toBeGreaterThan(0);

    // The detail-only field: real chips once the run's detail read lands.
    expect(await screen.findByText("web_search")).toBeInTheDocument();
    expect(screen.getByText("code_workspace")).toBeInTheDocument();

    // Three tools_used states are distinct: names, never recorded (null), recorded none ([]).
    expect(screen.getByTestId("ops-tools-8")).toHaveTextContent("never recorded");
    expect(screen.getByTestId("ops-tools-9")).toHaveTextContent("none");

    // 125 s, and the ledger's 4dp money shape; a null cost is the dash, never $0.00.
    expect(screen.getByTestId("ops-duration-7")).toHaveTextContent("2m 5s");
    expect(screen.getByTestId("ops-cost-7")).toHaveTextContent("$0.0123");
    expect(screen.getByTestId("ops-cost-8")).toHaveTextContent("—");
    expect(screen.getByTestId("ops-runs-count")).toHaveTextContent("Showing all 3");
  });

  it("names every empty state instead of drawing an empty table", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === RUNS_PATH) return Promise.resolve({ items: [], total: 0 });
      if (path === APPROVALS_PATH) return Promise.resolve([]);
      if (path === "/agents/status") return Promise.resolve([]);
      if (path === "/auto-mode") return Promise.resolve({ active: false, session: null });
      if (path === "/config") return Promise.resolve({ dispatch_paused: false });
      if (path === "/ops/sandbox") return Promise.resolve({ sandbox_mode: false });
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<OpsPage />);

    expect(await screen.findByText(/No runs recorded yet/)).toBeInTheDocument();
    expect(screen.getByText(/No tasks are waiting on approval/)).toBeInTheDocument();
    expect(screen.getByText(/No agent statuses came back/)).toBeInTheDocument();
    expect(screen.getByTestId("ops-switch-auto-mode")).toHaveTextContent("OFF");
  });

  it("names a failed ledger read and Retry re-issues exactly that read", async () => {
    let runsReads = 0;
    mockGet.mockImplementation((path: string) => {
      if (path === RUNS_PATH) {
        runsReads += 1;
        return runsReads === 1
          ? Promise.reject(new ApiError(503, "the runs table is unreachable", "E_DB"))
          : Promise.resolve({ items: [], total: 0 });
      }
      if (path === APPROVALS_PATH) return Promise.resolve([]);
      if (path === "/agents/status") return Promise.resolve([]);
      if (path === "/auto-mode") return Promise.resolve({ active: false, session: null });
      if (path === "/config") return Promise.resolve({ dispatch_paused: false });
      if (path === "/ops/sandbox") return Promise.resolve({ sandbox_mode: false });
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<OpsPage />);

    expect(await screen.findByText(/Could not load the runs ledger: E_DB:/)).toBeInTheDocument();
    expect(screen.queryByTestId("ops-runs-table")).toBeNull();

    fireEvent.click(screen.getByTestId("ops-runs-retry"));
    await flush();
    expect(runsReads).toBe(2);
    expect(await screen.findByText(/No runs recorded yet/)).toBeInTheDocument();
  });
});

describe("/ops — pending approvals", () => {
  it("approve posts the fingerprint AND the parked task id, then names the outcome", async () => {
    mockPost.mockResolvedValue({
      token: "tok_not_rendered",
      action_fingerprint: PENDING_FINGERPRINT,
      expires_at: 1_760_000_000,
      ttl_s: 3600,
    });
    render(<OpsPage />);

    expect(await screen.findByText("Review the campaign budget")).toBeInTheDocument();
    expect(screen.getByText(PENDING_FINGERPRINT)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId(`approve-3-${PENDING_FINGERPRINT}`));

    expect(mockPost).toHaveBeenCalledWith("/approvals", {
      action_fingerprint: PENDING_FINGERPRINT,
      task_id: 3,
    });
    expect(await screen.findByTestId("ops-approvals-notice")).toHaveTextContent(/re-queued/i);
    // The minted token is never rendered.
    expect(screen.queryByText("tok_not_rendered")).toBeNull();
  });

  it("deny is a two-step, real status write (rejected) — not a dead button", async () => {
    mockPatch.mockResolvedValue({ ...PENDING_TASK, status: "rejected" });
    render(<OpsPage />);

    const deny = await screen.findByTestId("deny-3");
    fireEvent.click(deny);
    expect(mockPatch).not.toHaveBeenCalled();
    expect(screen.getByTestId("deny-3")).toHaveTextContent("Confirm deny");

    fireEvent.click(screen.getByTestId("deny-3"));
    await flush();

    expect(mockPatch).toHaveBeenCalledWith("/tasks/3/status", {
      status: "rejected",
      error_message: expect.stringContaining("Denied"),
    });
    expect(await screen.findByTestId("ops-approvals-notice")).toHaveTextContent(/denied/i);
  });
});

describe("/ops — manual agent triggers", () => {
  it("disables the control in flight and names the server's refusal by code", async () => {
    mockPost.mockRejectedValueOnce(
      new ApiError(
        503,
        "task #9 was created but could not be handed to a worker",
        "E_REDIS",
        "start Redis, then retry the dispatch",
      ),
    );
    render(<OpsPage />);

    const trigger = await screen.findByTestId("trigger-competitor_research");
    fireEvent.click(trigger);
    await flush();

    const error = await screen.findByTestId("ops-trigger-error");
    expect(error).toHaveTextContent("E_REDIS: task #9 was created but could not be handed to a worker");
    expect(mockPost).toHaveBeenCalledWith("/agents/competitor_research/trigger", {
      task_title: "Manual competitor_research run (Ops)",
    });
  });

  it("stays disabled until the dispatch answers, then quotes the server's message", async () => {
    let resolvePost!: (value: unknown) => void;
    mockPost.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePost = resolve;
        }),
    );
    render(<OpsPage />);

    const trigger = await screen.findByTestId("trigger-competitor_research");
    fireEvent.click(trigger);
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveTextContent("Queueing…");

    await act(async () => {
      resolvePost({
        status: "queued",
        task_id: 11,
        message: "Task #11 queued for competitor_research",
      });
    });
    await flush();

    expect(await screen.findByTestId("ops-trigger-notice")).toHaveTextContent(
      "Task #11 queued for competitor_research",
    );
    expect(trigger).not.toBeDisabled();
  });
});

describe("/ops — auto mode and the kill switches", () => {
  it("renders the three real readings from /auto-mode, /config and /ops/sandbox", async () => {
    render(<OpsPage />);

    const auto = await screen.findByTestId("ops-switch-auto-mode");
    expect(auto).toHaveTextContent(/ON/);
    expect(auto).toHaveTextContent(/1h 30m left/);
    expect(auto).toHaveTextContent(/4 tasks done/);
    expect(auto).toHaveTextContent(/\$1.25 used/);

    expect(screen.getByTestId("ops-switch-dispatch-pause")).toHaveTextContent("PAUSED");
    expect(screen.getByTestId("ops-switch-sandbox")).toHaveTextContent(
      "ON — external actions are drafted, never sent",
    );
  });
});

describe("/ops — polling is visibility-aware", () => {
  /** jsdom's visibilityState is a prototype getter; override it per test and restore after. */
  function setVisibility(state: "visible" | "hidden") {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => state,
    });
  }

  it("issues nothing on hidden ticks and re-reads immediately when the tab becomes visible", async () => {
    jest.useFakeTimers();
    mockGet.mockImplementation((path: string) => {
      if (path === RUNS_PATH) return Promise.resolve({ items: [], total: 0 });
      if (path === APPROVALS_PATH) return Promise.resolve([]);
      if (path === "/agents/status") return Promise.resolve([]);
      if (path === "/auto-mode") return Promise.resolve({ active: false, session: null });
      if (path === "/config") return Promise.resolve({ dispatch_paused: false });
      if (path === "/ops/sandbox") return Promise.resolve({ sandbox_mode: false });
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    try {
      render(<OpsPage />);
      await flush();

      const runsReads = () =>
        mockGet.mock.calls.filter(([path]) => path === RUNS_PATH).length;
      expect(runsReads()).toBe(1);

      setVisibility("hidden");
      act(() => {
        jest.advanceTimersByTime(OPS_POLL_MS * 3);
      });
      await flush();
      expect(runsReads()).toBe(1);

      setVisibility("visible");
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await flush();
      expect(runsReads()).toBe(2);
    } finally {
      setVisibility("visible");
    }
  });
});
