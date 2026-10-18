/**
 * WAVE V17 / seat L4 — /usage paging cannot walk into an empty page.
 *
 * `canNext` was `offset + items.length < total`. When the ledger shrank between reads (or the server
 * answered an offset past its rows) a page could come back with ZERO rows while `total` still
 * exceeded the offset: NEXT stayed enabled and every further press re-read the same empty page —
 * a control promising rows the page already knows it did not get.
 *
 * Fix: NEXT requires at least one row on the current page (`items.length > 0`) as well as room
 * under the declared total. A zero-row page still says its real numbers ("No rows on this page —
 * N in the ledger") and PREV still works.
 *
 * RED leg: pre-fix, after moving to the empty page the NEXT button is enabled.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api } from "@/lib/api";
import UsagePage from "@/app/usage/page";

const mockGet = api.get as jest.Mock;

/** One ledger row; the fields the page renders, nothing invented. */
function run(id: number) {
  return {
    id,
    task_id: 7,
    task_title: `Run ${id}`,
    agent_type: "social_media",
    run_type: "task",
    status: "success",
    attempt: 1,
    started_at: "2026-10-13T18:21:39Z",
    ended_at: "2026-10-13T18:22:10Z",
    duration_secs: 31,
    tokens_input: null,
    tokens_output: null,
    tokens_used: 1200,
    cost_usd: 0.0042,
    cost_source: "measured",
    pricing_version: null,
  };
}

beforeEach(() => {
  mockGet.mockReset();
  // Page 1 has ONE row of a declared 60 (so NEXT is legitimately enabled); every later offset comes
  // back EMPTY while the declared total still says 60 — the ledger-shrank case. OpsPanel's own reads
  // are outside this pin; they fail loudly and render their own named states.
  mockGet.mockImplementation((path: string) => {
    if (!path.startsWith("/runs?")) return Promise.reject(new Error(`unexpected read: ${path}`));
    const offset = Number(new URLSearchParams(path.split("?")[1] ?? "").get("offset") ?? "0");
    return Promise.resolve(offset === 0 ? { items: [run(1)], total: 60 } : { items: [], total: 60 });
  });
});

describe("UsagePage — paging edges", () => {
  it("disables NEXT on a zero-row page and still shows the real declared numbers", async () => {
    const user = userEvent.setup();
    render(<UsagePage />);

    const next = await screen.findByTestId("usage-next");
    await waitFor(() =>
      expect(screen.getByTestId("usage-count")).toHaveTextContent("Showing the first 1 of 60"),
    );
    expect(next).toBeEnabled();

    await user.click(next);
    await screen.findByTestId("usage-count");
    await waitFor(() => expect(screen.getByTestId("usage-count")).toHaveTextContent("No rows on this page"));
    expect(screen.getByTestId("usage-next")).toBeDisabled();
    expect(screen.getByTestId("usage-prev")).toBeEnabled();
  });
});
