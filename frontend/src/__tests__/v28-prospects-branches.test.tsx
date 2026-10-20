/**
 * V28 / L1 — two ProspectForms / ProspectRowView branches that had no coverage.
 *
 *  (a) CLEARING is a write: an emptied optional box must PATCH an explicit `null` (the column's
 *      nullable contract) and never an empty string — `changesFrom` is the one place a draft
 *      becomes a request body, and the difference is the difference between "unknown" and "".
 *  (b) An UNKNOWN stored status word must be offered as its own option in the row's select:
 *      `PROSPECT_STATUSES` is UI vocabulary, the column is an open string, so opening the control
 *      must not be able to silently rewrite a value this build does not know (Q-25's discipline).
 *
 * These are PINS, not fixes: they pass on the base tree (`f0ce8a2`).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: {
      get: jest.fn(),
      post: jest.fn(),
      put: jest.fn(),
      patch: jest.fn(),
      delete: jest.fn(),
    },
  };
});

import ProspectsPage from "@/app/prospects/page";
import { api } from "@/lib/api";

const mockGet = api.get as jest.Mock;
const mockPatch = api.patch as jest.Mock;

const LIST_PATH = "/prospects?limit=100";

function prospect(over: Record<string, unknown> = {}) {
  return {
    id: 7,
    email: "ada@example.com",
    first_name: "Ada",
    last_name: "Lovelace",
    company: "Analytical Engines",
    title: "Founder",
    source: "manual",
    status: "new",
    created_at: "2026-10-01T09:00:00Z",
    ...over,
  };
}

beforeEach(() => {
  mockGet.mockReset();
  mockPatch.mockReset();
  mockGet.mockResolvedValue({ items: [prospect()], total: 1 });
});

describe("ProspectsPage — branches with no prior coverage", () => {
  it("PATCHes an explicit null when the operator empties an optional field", async () => {
    const user = userEvent.setup();
    mockPatch.mockResolvedValue(prospect({ title: null }));
    render(<ProspectsPage />);

    const row = await screen.findByTestId("prospect-row-7");
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    const form = within(row).getByTestId("prospect-edit-form");
    await user.clear(within(form).getByLabelText("Title"));
    await user.click(within(form).getByRole("button", { name: "Save" }));

    // A cleared box CLEARS the column; it is not an empty string, and no untouched field rides along.
    expect(mockPatch).toHaveBeenCalledWith("/prospects/7", { title: null });
    await waitFor(() =>
      expect(
        within(screen.getByTestId("prospect-row-7")).getByTestId("prospect-title"),
      ).toHaveTextContent("—"),
    );
  });

  it("offers a stored-but-unknown status as its own option instead of rewriting it", async () => {
    mockGet.mockResolvedValue({ items: [prospect({ status: "qualified_x" })], total: 1 });
    render(<ProspectsPage />);

    const row = await screen.findByTestId("prospect-row-7");
    const select = within(row).getByLabelText("Status for ada@example.com") as HTMLSelectElement;

    expect(select.value).toBe("qualified_x");
    expect(within(select).getByRole("option", { name: "qualified_x" })).toBeInTheDocument();
    // The chip repeats the server's word verbatim — no disguised status.
    expect(within(row).getByTestId("prospect-status")).toHaveTextContent("qualified_x");
  });
});
