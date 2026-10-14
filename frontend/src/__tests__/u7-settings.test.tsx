import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import * as fs from "node:fs";
import * as path from "node:path";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import SettingsPage, { type SettingsConfig } from "@/app/settings/page";
import { AutoModePanel } from "@/components/auto_mode/AutoModePanel";

const mockGet = api.get as jest.Mock;
const mockPut = api.put as jest.Mock;
const mockPost = api.post as jest.Mock;
const mockDelete = api.delete as jest.Mock;

afterEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
  mockPost.mockReset();
  mockDelete.mockReset();
});

/**
 * WAVE V7 / S4 — the settings night-task + emails surfaces and the dashboard AUTO MODE panel.
 *
 * Wire truth these tests pin (read from the backend before writing them):
 *  - GET/PUT /config serves / accepts the U5 columns on CompanyConfigResponse/Update
 *    (schemas/company.py): the five booleans leave as JSON booleans, the night hour / limit as
 *    JSON numbers, and PUT copies ONLY the provided keys (`exclude_unset`) — so the page must
 *    send exactly what changed;
 *  - GET/POST/DELETE /auto-mode (auto_mode.py:56-127): 201 {started, session}; GET {active,
 *    session|null}; DELETE {stopped, session}; `spend_used` null is "nothing was measured" and
 *    renders the dash — never a fabricated $0.
 */

const baseConfig: SettingsConfig = {
  name: "Polsia",
  mission: null,
  vision: null,
  description: null,
  target_market: null,
  value_prop: null,
  website_url: null,
  industry: null,
  product_type: null,
  timezone: null,
  daily_cycle_hour: null,
  night_task_enabled: true,
  night_task_hour: 23,
  night_task_limit: 2,
  notification_email: "owner@example.com",
  notify_on_completed: true,
  notify_on_failed: false,
  notify_on_awaiting_approval: true,
  notify_on_run_requeued: false,
};

function serveReads(config: SettingsConfig = baseConfig) {
  mockGet.mockImplementation((requestPath: string) => {
    if (requestPath === "/config") return Promise.resolve(config);
    if (requestPath === "/config/deepseek-key") {
      return Promise.resolve({ configured: false, source: "none" as const, masked: "" });
    }
    return Promise.reject(new Error(`unexpected GET ${requestPath}`));
  });
}

async function renderSettings(config: SettingsConfig = baseConfig) {
  serveReads(config);
  render(<SettingsPage />);
  await screen.findByRole("tab", { name: "Company Profile" });
}

// -------------------------------------------------------------------------------------------
// V7-T12 — night task controls (settings → Scheduler)
// -------------------------------------------------------------------------------------------

describe("u7 settings — night task (V7-T12)", () => {
  it("renders the current night-task values from the mocked GET", async () => {
    await renderSettings();
    fireEvent.click(screen.getByRole("tab", { name: "Scheduler" }));

    expect(screen.getByRole("switch", { name: "Night task enabled" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    const hourGroup = screen.getByRole("radiogroup", { name: "Night task hour" });
    expect(within(hourGroup).getAllByRole("radio")).toHaveLength(24);
    expect(within(hourGroup).getByRole("radio", { name: "23:00" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(within(hourGroup).getByRole("radio", { name: "00:00" })).toHaveAttribute(
      "aria-checked",
      "false",
    );

    const limitGroup = screen.getByRole("radiogroup", { name: "Night task limit" });
    expect(within(limitGroup).getAllByRole("radio")).toHaveLength(5);
    expect(within(limitGroup).getByRole("radio", { name: "2 tasks" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("toggling + save PUTs exactly the changed fields — booleans as booleans, numbers as numbers", async () => {
    await renderSettings();
    fireEvent.click(screen.getByRole("tab", { name: "Scheduler" }));

    fireEvent.click(screen.getByRole("switch", { name: "Night task enabled" })); // on -> off
    fireEvent.click(
      within(screen.getByRole("radiogroup", { name: "Night task hour" })).getByRole("radio", {
        name: "22:00",
      }),
    ); // 23 -> 22
    fireEvent.click(
      within(screen.getByRole("radiogroup", { name: "Night task limit" })).getByRole("radio", {
        name: "5 tasks",
      }),
    ); // 2 -> 5

    mockPut.mockResolvedValue({
      ...baseConfig,
      night_task_enabled: false,
      night_task_hour: 22,
      night_task_limit: 5,
    });
    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    const [requestPath, body] = mockPut.mock.calls[0];
    expect(requestPath).toBe("/config");
    expect(body).toEqual({ night_task_enabled: false, night_task_hour: 22, night_task_limit: 5 });
    expect(typeof body.night_task_enabled).toBe("boolean");
    expect(typeof body.night_task_hour).toBe("number");
    expect(typeof body.night_task_limit).toBe("number");
  });

  it("a refused save surfaces the ApiError envelope — code and how_to_fix", async () => {
    await renderSettings();
    fireEvent.click(screen.getByRole("tab", { name: "Scheduler" }));
    fireEvent.click(screen.getByRole("switch", { name: "Night task enabled" }));

    mockPut.mockRejectedValue(
      new ApiError(
        422,
        "night_task_limit: Input should be less than or equal to 5",
        "E_VALIDATION",
        "send night_task_limit as a number between 1 and 5",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_VALIDATION");
    expect(alert).toHaveTextContent("send night_task_limit as a number between 1 and 5");
  });
});

// -------------------------------------------------------------------------------------------
// V7-T13 — the Emails tab
// -------------------------------------------------------------------------------------------

describe("u7 settings — emails tab (V7-T13)", () => {
  it("is the fourth tab under a truthful label", async () => {
    await renderSettings();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Company Profile",
      "Scheduler",
      "Emails",
      "DeepSeek Key",
    ]);
  });

  it("renders the address field + the four switches, and includes their changes in the PUT", async () => {
    await renderSettings();
    fireEvent.click(screen.getByRole("tab", { name: "Emails" }));

    const email = screen.getByLabelText("Notification Email") as HTMLInputElement;
    expect(email.value).toBe("owner@example.com");

    expect(screen.getByRole("switch", { name: "Notify on completed tasks" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("switch", { name: "Notify on failed tasks" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(screen.getByRole("switch", { name: "Notify on awaiting approval" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("switch", { name: "Notify on requeued runs" })).toHaveAttribute(
      "aria-checked",
      "false",
    );

    // Requeued honesty: the helper must not promise an email that cannot fire yet.
    expect(screen.getByText(/sends nothing yet/)).toBeInTheDocument();

    fireEvent.change(email, { target: { value: "new-owner@example.com" } });
    fireEvent.click(screen.getByRole("switch", { name: "Notify on failed tasks" }));
    fireEvent.click(screen.getByRole("switch", { name: "Notify on requeued runs" }));

    mockPut.mockResolvedValue({
      ...baseConfig,
      notification_email: "new-owner@example.com",
      notify_on_failed: true,
      notify_on_run_requeued: true,
    });
    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    const [, body] = mockPut.mock.calls[0];
    expect(body).toEqual({
      notification_email: "new-owner@example.com",
      notify_on_failed: true,
      notify_on_run_requeued: true,
    });
    expect(typeof body.notify_on_failed).toBe("boolean");
  });
});

// -------------------------------------------------------------------------------------------
// V7-T14 — the AUTO MODE panel (dashboard)
// -------------------------------------------------------------------------------------------

const SESSION = {
  id: 9,
  started_at: "2026-10-14T12:00:00Z",
  ends_at: "2026-10-14T13:30:00Z",
  stopped_at: null,
  stop_reason: null,
  tasks_run: 7,
  spend_used: null,
  time_left_seconds: 5400,
};

describe("u7 auto mode panel (V7-T14)", () => {
  it("OFF renders the duration picker + START, and START posts duration_hours", async () => {
    mockGet.mockResolvedValue({ active: false, session: null });
    mockPost.mockResolvedValue({ started: true, session: { ...SESSION } });

    render(<AutoModePanel />);

    const group = await screen.findByRole("radiogroup", { name: "Auto mode duration" });
    expect(within(group).getAllByRole("radio")).toHaveLength(5);
    for (const label of ["1h", "6h", "24h", "3d", "5d"]) {
      expect(within(group).getByRole("radio", { name: label })).toBeInTheDocument();
    }

    const start = screen.getByRole("button", { name: "Start Auto Mode" });
    expect(start).toBeDisabled(); // no invented default window — the operator picks one

    fireEvent.click(within(group).getByRole("radio", { name: "24h" }));
    expect(start).toBeEnabled();
    fireEvent.click(start);

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost).toHaveBeenCalledWith("/auto-mode", { duration_hours: 24 });
    // The 201 answer flips the panel to the ON body — counters come from that same answer.
    expect(await screen.findByText("7 tasks done")).toBeInTheDocument();
  });

  it("ON renders time left · tasks done · spend — a null spend is the dash, NEVER 0", async () => {
    mockGet.mockResolvedValue({ active: true, session: SESSION });

    render(<AutoModePanel />);

    expect(await screen.findByText("1h 30m left")).toBeInTheDocument();
    expect(screen.getByText("7 tasks done")).toBeInTheDocument();
    expect(screen.getByText("— used")).toBeInTheDocument();
    expect(screen.queryByText("0 used")).toBeNull();
    expect(screen.queryByText("$0 used")).toBeNull();
    expect(screen.queryByText(/\$0\b/)).toBeNull();
  });

  it("STOP issues DELETE and shows the session's stop_reason human-mapped", async () => {
    mockGet.mockResolvedValue({ active: true, session: SESSION });
    mockDelete.mockResolvedValue({
      stopped: true,
      session: {
        ...SESSION,
        stopped_at: "2026-10-14T12:40:00Z",
        stop_reason: "user_stop",
        time_left_seconds: null,
      },
    });

    render(<AutoModePanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Stop Auto Mode" }));

    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/auto-mode"));
    expect(await screen.findByText(/Stopped by you/)).toBeInTheDocument();
    // Back to OFF: a new window can be started again.
    expect(screen.getByRole("radiogroup", { name: "Auto mode duration" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start Auto Mode" })).toBeInTheDocument();
  });

  it("a refused START renders the server envelope — never a silent no-op", async () => {
    mockGet.mockResolvedValue({ active: false, session: null });
    mockPost.mockRejectedValue(
      new ApiError(
        422,
        "an Auto Mode session is already active",
        "E_VALIDATION",
        "stop the active session with DELETE /api/v1/auto-mode before starting another",
      ),
    );

    render(<AutoModePanel />);
    const group = await screen.findByRole("radiogroup", { name: "Auto mode duration" });
    fireEvent.click(within(group).getByRole("radio", { name: "1h" }));
    fireEvent.click(screen.getByRole("button", { name: "Start Auto Mode" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_VALIDATION");
    expect(alert).toHaveTextContent("DELETE /api/v1/auto-mode");
  });
});

// -------------------------------------------------------------------------------------------
// F-03 — the stale "nothing exposes auto mode / night task" claim is gone
// -------------------------------------------------------------------------------------------

describe("F-03 — PolsiaStatusCard's false claim (source read)", () => {
  it("no longer says no endpoint or config field exposes the auto-mode / night-task state", () => {
    const source = fs.readFileSync(
      path.join(process.cwd(), "src", "components", "dashboard", "PolsiaStatusCard.tsx"),
      "utf8",
    );
    expect(source).not.toContain("no endpoint or config field exposes");
    expect(source).not.toContain("a pill that toggles nothing is a fake affordance");
    // The replacement names the real surfaces instead.
    expect(source).toContain("GET /auto-mode");
  });
});
