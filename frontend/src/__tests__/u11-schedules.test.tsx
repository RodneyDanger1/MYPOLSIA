/**
 * W3/unit-2 — the /schedules page (wiring audit F1: schedules ran via cadence.py with no UI at all).
 *
 * House pattern (u9-planner et al.): mock `@/lib/api` via a factory over `jest.requireActual`
 * (ApiError stays REAL) and pin every surface on its BEHAVIOUR — real endpoint path + payload,
 * empty/loading/error/refusal states, null == em dash.
 *
 * Wire truth written from the REAL backend files (read 2026-10-15):
 *   GET    /api/v1/schedules              -> 200 {items: [ScheduleResponse], total: n}  (schedules.py:39)
 *   POST   /api/v1/schedules              -> 201 ScheduleResponse                       (schedules.py:50)
 *   PATCH  /api/v1/schedules/{id}         -> 200 ScheduleResponse    (exclude_unset; pause = {"enabled": false}) (schedules.py:70)
 *   DELETE /api/v1/schedules/{id}         -> 200 ScheduleResponse (the REMOVED row)      (schedules.py:84)
 * Request schemas: `app/schemas/schedule.py` ScheduleCreate/ScheduleUpdate — cadence one of
 * once|daily|weekdays|weekly, hour 0-23 company-local, weekday 0-6 (Monday=0) for weekly only,
 * `enabled` defaults FALSE on create. Refusals: 422 by the schema (names the field) + the
 * service's E_VALIDATION belt; unknown id = E_NOT_FOUND (404). The page renders ApiError.display.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

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

import { absoluteStamp } from "@/components/dashboard/format";
import { api, ApiError } from "@/lib/api";
import SchedulesPage from "@/app/schedules/page";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;
const mockPatch = api.patch as jest.Mock;
const mockDelete = api.delete as jest.Mock;

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPatch.mockReset();
  mockDelete.mockReset();
});

/** Exact-path reads; an unlisted path fails LOUDLY so a new read cannot pass silently. */
function answerWith(answers: Record<string, unknown> = {}) {
  const paths: Record<string, unknown> = {
    "/schedules": { items: [], total: 0 },
    ...answers,
  };
  mockGet.mockImplementation((path: string) => {
    const value = paths[path];
    if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

/** One schedule in the frozen ScheduleResponse shape. */
function schedule(over: Record<string, unknown> = {}) {
  return {
    id: 3,
    name: "Morning report",
    prompt: "Write the morning report and email it to me.",
    cadence: "daily",
    hour: 9,
    weekday: null,
    enabled: true,
    next_run_at: "2026-10-16T14:00:00Z",
    last_run_at: null,
    created_at: "2026-10-01T09:00:00Z",
    ...over,
  };
}

describe("SchedulesPage — the list (GET /schedules)", () => {
  it("reads /schedules and states the honest empty list", async () => {
    answerWith();
    render(<SchedulesPage />);

    const empty = await screen.findByTestId("schedules-empty");
    expect(empty).toHaveTextContent(/No schedules yet/);
    expect(mockGet).toHaveBeenCalledWith("/schedules");
    expect(screen.queryByTestId("schedules-read-error")).toBeNull();
  });

  it("renders every field the server sent, and nulls as the em dash (never an invented date)", async () => {
    answerWith({
      "/schedules": {
        items: [
          schedule(),
          schedule({
            id: 5,
            name: "Weekly review",
            cadence: "weekly",
            hour: 17,
            weekday: 4,
            enabled: false,
            next_run_at: null,
            last_run_at: null,
          }),
        ],
        total: 2,
      },
    });
    render(<SchedulesPage />);

    const first = await screen.findByTestId("schedule-row-3");
    expect(within(first).getByTestId("schedule-name")).toHaveTextContent("Morning report");
    expect(within(first).getByTestId("schedule-prompt")).toHaveTextContent(/Write the morning report/);
    expect(within(first).getByTestId("schedule-cadence")).toHaveTextContent("daily");
    expect(within(first).getByTestId("schedule-hour")).toHaveTextContent("09:00");
    // weekday null on a daily row: the em dash, not "Monday".
    expect(within(first).getByTestId("schedule-weekday")).toHaveTextContent("—");
    expect(within(first).getByTestId("schedule-next-run")).toHaveTextContent(
      absoluteStamp("2026-10-16T14:00:00Z"),
    );

    const second = screen.getByTestId("schedule-row-5");
    expect(within(second).getByTestId("schedule-cadence")).toHaveTextContent("weekly");
    expect(within(second).getByTestId("schedule-weekday")).toHaveTextContent("Friday");
    // A paused row's cleared next-run is the em dash — never a fabricated instant.
    expect(within(second).getByTestId("schedule-next-run")).toHaveTextContent("—");
    expect(within(second).getByTestId("schedule-last-run")).toHaveTextContent("—");
    // The switch carries the server's real enabled state.
    expect(within(second).getByRole("switch")).toHaveAttribute("aria-checked", "false");
    expect(within(first).getByRole("switch")).toHaveAttribute("aria-checked", "true");
  });

  it("shows a loading state before the read settles (no dash rows, no empty claim)", () => {
    mockGet.mockImplementation(() => new Promise(() => {}));
    render(<SchedulesPage />);

    expect(screen.getByTestId("schedules-loading")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText(/No schedules yet/)).toBeNull();
    expect(screen.queryByTestId("schedules-read-error")).toBeNull();
  });

  it("names a failed read with ApiError.display and draws no rows and no empty claim", async () => {
    answerWith({ "/schedules": new ApiError(500, "boom", "E_HTTP_500", "check the backend") });
    render(<SchedulesPage />);

    const alert = await screen.findByTestId("schedules-read-error");
    expect(alert).toHaveTextContent("Could not load the schedules: E_HTTP_500: boom — check the backend");
    expect(screen.queryByTestId("schedule-row-3")).toBeNull();
    expect(screen.queryByText(/No schedules yet/)).toBeNull();
  });

  it("names a broken (non-list) body instead of laundering it into an empty list", async () => {
    answerWith({ "/schedules": { detail: "not a list" } });
    render(<SchedulesPage />);

    const alert = await screen.findByTestId("schedules-read-error");
    expect(alert).toHaveTextContent("the endpoint answered without a schedule list");
    expect(screen.queryByText(/No schedules yet/)).toBeNull();
  });
});

describe("SchedulesPage — create (POST /schedules)", () => {
  it("POSTs the exact body (enabled defaults false) and shows the server's row with no invented re-read", async () => {
    answerWith({ "/schedules": { items: [schedule()], total: 1 } });
    mockPost.mockResolvedValue(
      schedule({
        id: 11,
        name: "Nightly revenue report",
        prompt: "Summarise today's revenue.",
        enabled: false,
        next_run_at: null,
      }),
    );
    render(<SchedulesPage />);
    await screen.findByTestId("schedule-row-3");

    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Nightly revenue report" },
    });
    fireEvent.change(screen.getByLabelText("Prompt"), {
      target: { value: "Summarise today's revenue." },
    });
    fireEvent.change(screen.getByLabelText("Hour"), { target: { value: "22" } });
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));

    // Daily is the default cadence: no `weekday` key rides the wire (the server owns that rule).
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/schedules", {
        name: "Nightly revenue report",
        prompt: "Summarise today's revenue.",
        cadence: "daily",
        hour: 22,
        enabled: false,
      }),
    );

    expect(await screen.findByTestId("schedule-row-11")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("carries the weekday when the cadence is weekly", async () => {
    answerWith({ "/schedules": { items: [], total: 0 } });
    mockPost.mockResolvedValue(schedule({ id: 12, cadence: "weekly", weekday: 0, hour: 8 }));
    render(<SchedulesPage />);
    await screen.findByTestId("schedules-empty");

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Monday kickoff" } });
    fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "Plan the week." } });

    const cadenceGroup = screen.getByRole("radiogroup", { name: "Cadence" });
    fireEvent.click(within(cadenceGroup).getByRole("radio", { name: "Weekly" }));
    fireEvent.change(screen.getByLabelText("Hour"), { target: { value: "8" } });
    fireEvent.change(screen.getByLabelText("Weekday"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/schedules", {
        name: "Monday kickoff",
        prompt: "Plan the week.",
        cadence: "weekly",
        hour: 8,
        weekday: 0,
        enabled: false,
      }),
    );
  });

  it("renders a create refusal as ApiError.display — no invented schedule", async () => {
    answerWith({ "/schedules": { items: [], total: 0 } });
    mockPost.mockRejectedValue(
      new ApiError(
        422,
        "task_schedules.hour 25 is invalid: an integer 0-23",
        "E_VALIDATION",
        "fix the value and retry",
      ),
    );
    render(<SchedulesPage />);
    await screen.findByTestId("schedules-empty");

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Bad hour" } });
    fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "Never valid." } });
    fireEvent.change(screen.getByLabelText("Hour"), { target: { value: "25" } });
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));

    const error = await screen.findByTestId("schedules-action-error");
    expect(error).toHaveTextContent(
      "E_VALIDATION: task_schedules.hour 25 is invalid: an integer 0-23 — fix the value and retry",
    );
    expect(screen.queryByTestId("schedule-row-11")).toBeNull();
  });
});

describe("SchedulesPage — pause/resume (PATCH /schedules/{id})", () => {
  it("pauses with the exact body {enabled:false} and takes the cleared next-run from the answer", async () => {
    answerWith({ "/schedules": { items: [schedule()], total: 1 } });
    mockPatch.mockResolvedValue(schedule({ enabled: false, next_run_at: null }));
    render(<SchedulesPage />);
    const row = await screen.findByTestId("schedule-row-3");

    fireEvent.click(within(row).getByRole("switch"));

    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/schedules/3", { enabled: false }));
    await waitFor(() =>
      expect(within(screen.getByTestId("schedule-row-3")).getByRole("switch")).toHaveAttribute(
        "aria-checked",
        "false",
      ),
    );
    expect(within(screen.getByTestId("schedule-row-3")).getByTestId("schedule-next-run")).toHaveTextContent(
      "—",
    );
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("resumes with {enabled:true} and shows the recomputed next-run from the answer", async () => {
    answerWith({
      "/schedules": { items: [schedule({ enabled: false, next_run_at: null })], total: 1 },
    });
    mockPatch.mockResolvedValue(schedule({ enabled: true, next_run_at: "2026-10-16T14:00:00Z" }));
    render(<SchedulesPage />);
    const row = await screen.findByTestId("schedule-row-3");

    fireEvent.click(within(row).getByRole("switch"));

    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/schedules/3", { enabled: true }));
    await waitFor(() =>
      expect(within(screen.getByTestId("schedule-row-3")).getByTestId("schedule-next-run")).toHaveTextContent(
        absoluteStamp("2026-10-16T14:00:00Z"),
      ),
    );
  });

  it("renders a pause refusal by name — the switch snaps back to the server's state", async () => {
    answerWith({ "/schedules": { items: [schedule()], total: 1 } });
    mockPatch.mockRejectedValue(
      new ApiError(404, "task schedule 3 does not exist", "E_NOT_FOUND", "refresh the list and retry"),
    );
    render(<SchedulesPage />);
    const row = await screen.findByTestId("schedule-row-3");

    fireEvent.click(within(row).getByRole("switch"));

    const error = await screen.findByTestId("schedules-action-error");
    expect(error).toHaveTextContent("E_NOT_FOUND: task schedule 3 does not exist — refresh the list and retry");
    // No invented pause: the row still shows the server's last answer.
    expect(within(screen.getByTestId("schedule-row-3")).getByRole("switch")).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });
});

describe("SchedulesPage — edit (PATCH with only the changed fields)", () => {
  it("PATCHes only the field the user changed (exclude_unset semantics)", async () => {
    answerWith({ "/schedules": { items: [schedule()], total: 1 } });
    mockPatch.mockResolvedValue(schedule({ name: "Morning digest" }));
    render(<SchedulesPage />);
    const row = await screen.findByTestId("schedule-row-3");

    fireEvent.click(within(row).getByRole("button", { name: /^Edit$/ }));
    const form = await screen.findByTestId("schedule-edit-3");
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Morning digest" } });
    fireEvent.click(within(form).getByRole("button", { name: /^Save$/ }));

    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/schedules/3", { name: "Morning digest" }));
    const [, payload] = mockPatch.mock.calls[0];
    expect(payload).not.toHaveProperty("hour");
    expect(payload).not.toHaveProperty("enabled");
    await waitFor(() =>
      expect(within(screen.getByTestId("schedule-row-3")).getByTestId("schedule-name")).toHaveTextContent(
        "Morning digest",
      ),
    );
    expect(mockGet).toHaveBeenCalledTimes(1);
  });
});

describe("SchedulesPage — delete (DELETE /schedules/{id})", () => {
  it("deletes through a confirm step and removes the removed row", async () => {
    answerWith({
      "/schedules": {
        items: [schedule(), schedule({ id: 5, name: "Weekly review", cadence: "weekly", weekday: 4 })],
        total: 2,
      },
    });
    mockDelete.mockResolvedValue(schedule());
    render(<SchedulesPage />);
    const row = await screen.findByTestId("schedule-row-3");

    fireEvent.click(within(row).getByRole("button", { name: /^Delete$/ }));
    // First click only arms the confirm — nothing is deleted yet.
    expect(mockDelete).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("schedule-delete-confirm-3"));

    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/schedules/3"));
    await waitFor(() => expect(screen.queryByTestId("schedule-row-3")).toBeNull());
    expect(screen.getByTestId("schedule-row-5")).toBeInTheDocument();
  });

  it("renders a delete refusal by name — the row stays", async () => {
    answerWith({ "/schedules": { items: [schedule()], total: 1 } });
    mockDelete.mockRejectedValue(
      new ApiError(404, "task schedule 3 does not exist", "E_NOT_FOUND", "refresh the list and retry"),
    );
    render(<SchedulesPage />);
    const row = await screen.findByTestId("schedule-row-3");

    fireEvent.click(within(row).getByRole("button", { name: /^Delete$/ }));
    fireEvent.click(screen.getByTestId("schedule-delete-confirm-3"));

    const error = await screen.findByTestId("schedules-action-error");
    expect(error).toHaveTextContent("E_NOT_FOUND: task schedule 3 does not exist — refresh the list and retry");
    expect(screen.getByTestId("schedule-row-3")).toBeInTheDocument();
  });
});

describe("api.patch — the REAL client method (mirror of put)", () => {
  it("sends method PATCH with the JSON body to /api/v1<path>", async () => {
    const real = jest.requireActual("@/lib/api") as typeof import("@/lib/api");
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    });
    (global as unknown as { fetch: unknown }).fetch = fetchMock;

    const result = await real.api.patch<{ ok: boolean }>("/schedules/5", { enabled: false });

    expect(result).toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost"}/api/v1/schedules/5`);
    expect(init.method).toBe("PATCH");
    expect(init.body).toBe(JSON.stringify({ enabled: false }));
  });
});
