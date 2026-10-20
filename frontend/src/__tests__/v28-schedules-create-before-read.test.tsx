/**
 * V28 / L1 — a create must not fabricate the schedule TABLE.
 *
 * `GET /schedules` answers `{items, total}` (the whole list). `POST /schedules` answers ONE row.
 * If the POST lands before the list read settles, painting `[created]` as "All schedules" claims
 * there is exactly one schedule — a claim about the table made from a write answer. The sibling
 * page guards this (`app/prospects/page.tsx:89` — `current === null ? current : ...`); this test
 * pins the same rule for schedules.
 *
 * House mocking pattern (u11-schedules): mock `@/lib/api` over `jest.requireActual` so `ApiError`
 * stays real, and fail loudly on a read the test did not declare.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

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

import SchedulesPage from "@/app/schedules/page";
import { api } from "@/lib/api";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

function schedule(over: Record<string, unknown> = {}) {
  return {
    id: 11,
    name: "Nightly revenue report",
    prompt: "Summarise today's revenue.",
    cadence: "daily",
    hour: 22,
    weekday: null,
    enabled: false,
    next_run_at: null,
    last_run_at: null,
    created_at: "2026-10-19T09:00:00Z",
    ...over,
  };
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

describe("SchedulesPage — a create before the list read", () => {
  it("does not present the POST answer as the whole schedule table", async () => {
    // The list read never settles: the page is still "not read yet" for the whole test.
    mockGet.mockReturnValue(new Promise(() => {}));
    mockPost.mockResolvedValue(schedule());

    render(<SchedulesPage />);
    expect(screen.getByTestId("schedules-loading")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Nightly revenue report" },
    });
    fireEvent.change(screen.getByLabelText("Prompt"), {
      target: { value: "Summarise today's revenue." },
    });
    fireEvent.change(screen.getByLabelText("Hour"), { target: { value: "22" } });
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));

    // The row the server answered is real, but it is not the list: no table may be drawn from it.
    expect(screen.queryByTestId("schedules-list")).toBeNull();
    expect(screen.queryByTestId("schedule-row-11")).toBeNull();
    // …and the page must keep saying it has not read the list yet — not "empty", not "all".
    expect(screen.getByTestId("schedules-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("schedules-empty")).toBeNull();
  });
});
