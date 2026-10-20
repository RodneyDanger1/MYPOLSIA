/**
 * WAVE V27 / lane L2 — the `/ops` run-workspace retention panel.
 *
 * RED-first: at the V26 settle commit `@/components/ops/OpsRetention` does not exist (the import
 * below is the honest RED — module not found) and nothing on `/ops` can dry-run or apply the
 * registry's run-workspace cleanup.
 *
 * What is pinned
 * --------------
 *  1. the panel renders the LAST recorded result (applied pass: counts, names, policy, trigger)
 *     from `GET /ops/run-workspace-cleanup`;
 *  2. `{data: null}` is an honest absence sentence, not a zeroed result grid;
 *  3. a failed read is named and Retry re-issues that read;
 *  4. Dry run posts `{apply: false}` — the SAFE direction — and renders the returned counts;
 *  5. Apply is a TWO-CLICK verb: the first click only arms a confirmation, the second posts
 *     `{apply: true}`; Cancel defuses it;
 *  6. a refused POST renders the server's named refusal (code + message), never a friendly
 *     rewrite;
 *  7. the panel is actually mounted on the `/ops` page.
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
import { OpsRetention } from "@/components/ops/OpsRetention";
import { ApiError, api } from "@/lib/api";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

const ROUTE = "/ops/run-workspace-cleanup";

async function flush(times = 10) {
  await act(async () => {
    for (let i = 0; i < times; i += 1) await Promise.resolve();
  });
}

const LAST_RESULT = {
  runs_dir: "C:/data/workspace/runs",
  count: 2,
  kept: 21,
  removed: ["run-old-00", "run-old-01"],
  skipped: [{ name: "not a run", reason: "invalid_name" }],
  policy: { keep_latest: 20, max_age_hours: 72 },
  applied: true,
};

const LAST = {
  activity_id: 12,
  created_at: "2026-10-19T10:00:00Z",
  trigger: "beat",
  result: LAST_RESULT,
};

const DRY_RESULT = { ...LAST_RESULT, applied: false, count: 1, removed: ["run-old-00"], kept: 22 };

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockGet.mockResolvedValue({ data: null });
});

afterEach(async () => {
  await flush();
});

describe("OpsRetention — the last result", () => {
  it("renders the last recorded applied pass: counts, names, policy and trigger", async () => {
    mockGet.mockResolvedValue({ data: LAST });

    render(<OpsRetention />);

    expect(await screen.findByTestId("ops-retention-result")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith(ROUTE);
    expect(screen.getByTestId("ops-retention-count")).toHaveTextContent("2");
    expect(screen.getByText("run-old-00")).toBeInTheDocument();
    expect(screen.getByText("run-old-01")).toBeInTheDocument();
    expect(screen.getByTestId("ops-retention-policy")).toHaveTextContent(
      "keep newest 20 · max age 72 h",
    );
    expect(screen.getByTestId("ops-retention-applied")).toHaveTextContent("APPLIED");
    expect(screen.getByTestId("ops-retention-trigger")).toHaveTextContent("daily beat");
    expect(screen.getByText("C:/data/workspace/runs")).toBeInTheDocument();
    expect(screen.getByTestId("ops-retention-skipped")).toHaveTextContent("not a run");
  });

  it("says nothing has been recorded yet instead of drawing zeroes", async () => {
    render(<OpsRetention />);

    expect(await screen.findByTestId("ops-retention-empty")).toHaveTextContent(
      /No cleanup has been recorded yet/,
    );
    expect(screen.queryByTestId("ops-retention-result")).toBeNull();
  });

  it("names a failed read and Retry re-issues exactly that read", async () => {
    mockGet
      .mockRejectedValueOnce(new ApiError(503, "the activity ledger is unreachable", "E_DB"))
      .mockResolvedValueOnce({ data: null });

    render(<OpsRetention />);

    expect(await screen.findByText(/E_DB: the activity ledger is unreachable/)).toBeInTheDocument();
    expect(screen.queryByTestId("ops-retention-result")).toBeNull();

    fireEvent.click(screen.getByTestId("ops-retention-retry"));
    await flush();
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(await screen.findByTestId("ops-retention-empty")).toBeInTheDocument();
  });
});

describe("OpsRetention — the verbs", () => {
  it("Dry run posts apply:false and renders the returned counts", async () => {
    mockPost.mockResolvedValue(DRY_RESULT);

    render(<OpsRetention />);
    await flush();

    fireEvent.click(screen.getByTestId("ops-retention-dry-run"));

    expect(mockPost).toHaveBeenCalledWith(ROUTE, { apply: false });
    expect(await screen.findByTestId("ops-retention-result")).toBeInTheDocument();
    expect(screen.getByTestId("ops-retention-count")).toHaveTextContent("1");
    expect(screen.getByTestId("ops-retention-applied")).toHaveTextContent("DRY RUN");
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it("Apply requires a SECOND click — the first only arms the confirmation", async () => {
    mockPost.mockResolvedValue({ ...DRY_RESULT, applied: true });

    render(<OpsRetention />);
    await flush();

    fireEvent.click(screen.getByTestId("ops-retention-apply"));
    expect(mockPost).not.toHaveBeenCalled();
    expect(screen.getByTestId("ops-retention-apply")).toHaveTextContent("Confirm apply");
    expect(screen.getByTestId("ops-retention-confirm-hint")).toHaveTextContent(
      /deletes the expired/,
    );

    fireEvent.click(screen.getByTestId("ops-retention-apply"));
    await flush();
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith(ROUTE, { apply: true });
    expect(screen.getByTestId("ops-retention-applied")).toHaveTextContent("APPLIED");
  });

  it("Cancel defuses an armed confirmation without posting", async () => {
    render(<OpsRetention />);
    await flush();

    fireEvent.click(screen.getByTestId("ops-retention-apply"));
    fireEvent.click(screen.getByTestId("ops-retention-cancel"));

    expect(screen.queryByTestId("ops-retention-confirm-hint")).toBeNull();
    expect(screen.getByTestId("ops-retention-apply")).toHaveTextContent("Apply");
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("renders a refused POST as the server's named refusal", async () => {
    mockPost.mockRejectedValue(
      new ApiError(
        422,
        "the request body carries an unknown key 'surprise'",
        "E_VALIDATION",
        "send {apply?: boolean} only",
      ),
    );

    render(<OpsRetention />);
    await flush();

    fireEvent.click(screen.getByTestId("ops-retention-dry-run"));
    await flush();

    expect(screen.getByTestId("ops-retention-action-error")).toHaveTextContent(
      /E_VALIDATION: the request body carries an unknown key/,
    );
  });
});

describe("/ops page — the panel is mounted", () => {
  it("renders the retention panel alongside the existing cockpit", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === ROUTE) return Promise.resolve({ data: null });
      if (path === "/runs?limit=20") return Promise.resolve({ items: [], total: 0 });
      if (path === "/tasks?status=awaiting_approval&limit=50") return Promise.resolve([]);
      if (path === "/agents/status") return Promise.resolve([]);
      if (path === "/auto-mode") return Promise.resolve({ active: false, session: null });
      if (path === "/config") return Promise.resolve({ dispatch_paused: false });
      if (path === "/ops/sandbox") return Promise.resolve({ sandbox_mode: false });
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<OpsPage />);

    expect(await screen.findByTestId("ops-retention")).toBeInTheDocument();
    expect(await screen.findByTestId("ops-retention-empty")).toBeInTheDocument();
  });
});
