/**
 * V28 / L1 — a STORED retention record whose time the server could not state must not be
 * rendered as if it had just happened.
 *
 * Wire truth (`backend/app/api/v1/ops.py`):
 *   GET  /ops/run-workspace-cleanup -> `{data: RunWorkspaceCleanupRecord | null}` where
 *        `created_at: UtcDatetime | None` (ops.py:328 — the ORM column is nullable), and
 *        `{data: null}` means "nothing recorded yet".
 *   POST /ops/run-workspace-cleanup -> the RunWorkspaceCleanupResult of THIS call. That result
 *        genuinely is "just now" — it is the one case where a missing timestamp is the clock.
 *
 * So `at === null` has two meanings and the panel must not collapse them: a GET record with a
 * null stamp is "the server recorded no timestamp", while a POST result is "just now". Before
 * V28 the panel printed "just now" for both, inventing an instant for a cleanup that may be days
 * old and calling it "result of this run".
 *
 * House mocking pattern (v27-retention): keep the real `@/lib/api` classes, replace the `api` object.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), patch: jest.fn(), delete: jest.fn() },
  };
});

import { act, fireEvent, render, screen } from "@testing-library/react";

import { OpsRetention } from "@/components/ops/OpsRetention";
import { api } from "@/lib/api";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

const ROUTE = "/ops/run-workspace-cleanup";

async function flush(times = 10) {
  await act(async () => {
    for (let i = 0; i < times; i += 1) await Promise.resolve();
  });
}

const RESULT = {
  runs_dir: "C:/data/workspace/runs",
  count: 3,
  kept: 20,
  removed: ["run-old-00", "run-old-01", "run-old-02"],
  skipped: [],
  policy: { keep_latest: 20, max_age_hours: 72 },
  applied: true,
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockGet.mockResolvedValue({ data: null });
});

afterEach(async () => {
  await flush();
});

describe("OpsRetention — a null recorded timestamp is not 'just now'", () => {
  it("states that the stored record carries no timestamp, and names the recorded row", async () => {
    mockGet.mockResolvedValue({
      data: { activity_id: 41, created_at: null, trigger: "beat", result: RESULT },
    });

    render(<OpsRetention />);

    const trigger = await screen.findByTestId("ops-retention-trigger");
    expect(trigger).toHaveTextContent("the daily beat");
    // The defect: the panel used to print "just now" for this legal wire value.
    expect(trigger).not.toHaveTextContent(/just now/);
    expect(trigger).toHaveTextContent(/no timestamp recorded/);
    // …and the basis line must not claim the result came from a run pressed in this session.
    expect(screen.getByTestId("ops-retention-basis")).not.toHaveTextContent(/result of this run/);
    expect(screen.getByTestId("ops-retention-basis")).toHaveTextContent("last recorded row #41");
  });

  it("still says 'just now' for the result of a POST the owner just ran", async () => {
    mockPost.mockResolvedValue({ ...RESULT, applied: false, count: 1 });
    render(<OpsRetention />);
    await flush();

    fireEvent.click(screen.getByTestId("ops-retention-dry-run"));
    await flush();

    expect(mockPost).toHaveBeenCalledWith(ROUTE, { apply: false });
    expect(screen.getByTestId("ops-retention-trigger")).toHaveTextContent(/just now/);
    expect(screen.getByTestId("ops-retention-basis")).toHaveTextContent("result of this run");
  });
});
