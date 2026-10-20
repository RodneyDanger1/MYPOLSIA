/**
 * V28 / L1 — the two broken-shape refusals of the retention panel, pinned.
 *
 * Before this file, the panel's named refusals for a WIRE that is not the documented shape had no
 * coverage at all (v27-retention pins the healthy read, the empty read, a failed read and a refused
 * POST). Both branches are load-bearing honesty paths: the panel validates the stored record field
 * by field (`isRecord` -> `isResult`) and the POST answer too, so a shape drift is NAMED instead of
 * rendered as counts that do not exist.
 *
 * These are PINS, not fixes: they pass on the base tree (`f0ce8a2`) and exist so the branches
 * cannot regress silently.
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

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockGet.mockResolvedValue({ data: null });
});

afterEach(async () => {
  await flush();
});

describe("OpsRetention — a wire that is not the documented shape is named", () => {
  it("names a stored record whose result is not the seven-key contract, and offers Retry", async () => {
    mockGet.mockResolvedValue({
      data: {
        activity_id: 9,
        created_at: "2026-10-19T10:00:00Z",
        trigger: "beat",
        // `runs_dir` must be a string and `removed` an array of strings: this is neither.
        result: { runs_dir: 42, count: 1, kept: 0, removed: "run-old-00" },
      },
    });

    render(<OpsRetention />);

    expect(
      await screen.findByText(/the stored cleanup result is not the documented shape/),
    ).toBeInTheDocument();
    // Nothing is drawn from a record that failed validation.
    expect(screen.queryByTestId("ops-retention-result")).toBeNull();
    expect(screen.getByTestId("ops-retention-retry")).toBeInTheDocument();
  });

  it("names a POST answer that is not the documented result and adopts no counts", async () => {
    // `count` is a string and the policy block is missing: not a RunWorkspaceCleanupResult.
    mockPost.mockResolvedValue({ runs_dir: "C:/runs", count: "two", kept: 1, applied: false });

    render(<OpsRetention />);
    await flush();

    fireEvent.click(screen.getByTestId("ops-retention-dry-run"));
    await flush();

    expect(screen.getByTestId("ops-retention-action-error")).toHaveTextContent(
      "the server's cleanup response is not the documented shape",
    );
    // The broken answer never becomes the panel's result (the baseline read was `{data: null}`).
    expect(screen.queryByTestId("ops-retention-result")).toBeNull();
    expect(screen.queryByTestId("ops-retention-count")).toBeNull();
  });
});
