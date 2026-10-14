import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), put: jest.fn(), post: jest.fn(), delete: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import SettingsPage, { type SettingsConfig } from "@/app/settings/page";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;
const mockApiPut = api.put as jest.MockedFunction<typeof api.put>;

/**
 * The fixture is declared as `SettingsConfig` on purpose: the page's contract is `timezone: string |
 * null` / `daily_cycle_hour: number | null` (the columns carry no NOT NULL and the backend serves a
 * stored NULL). Under the old strict `timezone: string` / `daily_cycle_hour: number` this file does
 * not compile — that is the pin.
 */
const wireConfig: SettingsConfig = {
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
};

const datedConfig: SettingsConfig = { ...wireConfig, timezone: "America/Chicago", daily_cycle_hour: 6 };

/** The page mounts DeepSeekKeySection, which reads its own status endpoint. */
function mockConfigGet(config: SettingsConfig) {
  mockApiGet.mockImplementation((path: string) => {
    if (path === "/config") return Promise.resolve(config);
    if (path === "/config/deepseek-key") {
      return Promise.resolve({ configured: false, source: "none" as const, masked: "" });
    }
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

function inputFor(label: string): HTMLInputElement {
  const labelNode = screen.getByText(label);
  const input = labelNode.parentElement?.querySelector("input");
  if (!input) throw new Error(`the "${label}" field must render an input`);
  return input as HTMLInputElement;
}

const HOUR_LABEL = "Morning Cycle Hour (company-local)";

describe("SettingsPage", () => {
  afterEach(() => {
    mockApiGet.mockReset();
    mockApiPut.mockReset();
  });

  it("surfaces a refused save with the server's own code — and never a success banner", async () => {
    mockConfigGet(datedConfig);
    mockApiPut.mockRejectedValue(new ApiError(503, "company configuration has not been seeded yet", "E_CONFIG", "run the seed step"));

    render(<SettingsPage />);
    // U3 re-aim: the save is dirty-gated now (settings.md ACC 3), so the form must be edited first.
    fireEvent.change(await screen.findByLabelText("Industry"), { target: { value: "Changed" } });
    const save = screen.getByRole("button", { name: "Save Settings" });
    fireEvent.click(save);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_CONFIG");
    expect(alert).toHaveTextContent("run the seed step");
    expect(screen.queryByText("Settings saved successfully")).toBeNull();
    // The button must come back out of its "Saving…" state (a refusal is not a hang).
    expect(await screen.findByRole("button", { name: "Save Settings" })).toBeEnabled();
  });

  it("shows the success banner only when the write actually landed", async () => {
    mockConfigGet(datedConfig);
    mockApiPut.mockResolvedValue({ ...datedConfig, industry: "Changed" });

    render(<SettingsPage />);
    // U3 re-aim: dirty-gated save — edit a field first.
    fireEvent.change(await screen.findByLabelText("Industry"), { target: { value: "Changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    expect(await screen.findByText("Settings saved successfully")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a cleared hour box is TRANSMITTED as null — the previous value is never kept (Q-18 re-aim)", async () => {
    // OLD (pre-U3): "a cleared hour box keeps the previous value — it never becomes midnight" —
    // that snap-back WAS the Q-18 defect (the operator could not clear the box). NEW: the empty
    // draft stays visible and the PUT carries `daily_cycle_hour: null` (the documented "no value").
    mockConfigGet(datedConfig);
    mockApiPut.mockResolvedValue({ ...datedConfig, daily_cycle_hour: null });

    render(<SettingsPage />);
    await screen.findByRole("button", { name: "Save Settings" });
    fireEvent.click(screen.getByRole("tab", { name: "Scheduler" }));
    expect(inputFor(HOUR_LABEL).value).toBe("6");

    fireEvent.change(inputFor(HOUR_LABEL), { target: { value: "" } });
    expect(inputFor(HOUR_LABEL).value).toBe("");

    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));
    await waitFor(() => expect(mockApiPut).toHaveBeenCalledTimes(1));
    const [, body] = mockApiPut.mock.calls[0];
    expect(body).toEqual({ daily_cycle_hour: null });
  });

  it("renders a NULL timezone/hour as empty boxes; an untouched NULL is never rewritten", async () => {
    // OLD: "transmits the null unchanged" (the whole fetched object was PUT). NEW: only CHANGED
    // fields are transmitted, and with no edit the dirty-gated save is disabled — so no request
    // fires at all and a stored NULL can never be laundered into "".
    mockConfigGet(wireConfig);
    mockApiPut.mockResolvedValue(wireConfig);

    render(<SettingsPage />);
    await screen.findByRole("button", { name: "Save Settings" });
    fireEvent.click(screen.getByRole("tab", { name: "Scheduler" }));

    expect(inputFor(HOUR_LABEL).value).toBe("");
    expect(inputFor("Timezone").value).toBe("");

    expect(screen.getByRole("button", { name: "Save Settings" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));
    expect(mockApiPut).not.toHaveBeenCalled();
  });

  it("names the reason a failed LOAD — the generic line alone carried none", async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path === "/config") {
        return Promise.reject(new ApiError(404, "company configuration has not been seeded yet", "E_NOT_FOUND", "seed the company row"));
      }
      return Promise.resolve({ configured: false, source: "none" as const, masked: "" });
    });

    render(<SettingsPage />);

    expect(await screen.findByText("Failed to load config")).toBeInTheDocument();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("E_NOT_FOUND");
    expect(alert).toHaveTextContent("seed the company row");
  });

  // Q-17 (settings.md §4): the page's suite pins the new loading skeleton, the dirty-gated save,
  // and (above) Q-18's cleared-box transmission — U3 re-aim of this file.
  it("renders the loading skeleton (never bare 'Loading…') and gates the save on dirty", async () => {
    mockConfigGet(datedConfig);
    const { container } = render(<SettingsPage />);

    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(screen.queryByText("Loading…")).toBeNull();

    const save = await screen.findByRole("button", { name: "Save Settings" });
    expect(save).toBeDisabled(); // clean form: nothing to write

    fireEvent.change(screen.getByLabelText("Industry"), { target: { value: "Dirty" } });
    expect(save).toBeEnabled();
  });
});
