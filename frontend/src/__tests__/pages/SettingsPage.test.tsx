import { fireEvent, render, screen } from "@testing-library/react";

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
    const save = await screen.findByRole("button", { name: "Save Settings" });
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
    mockApiPut.mockResolvedValue(datedConfig);

    render(<SettingsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Save Settings" }));

    expect(await screen.findByText("Settings saved successfully")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a cleared hour box keeps the previous value — it never becomes midnight (Number('') === 0)", async () => {
    mockConfigGet(datedConfig);
    mockApiPut.mockResolvedValue(datedConfig);

    render(<SettingsPage />);
    await screen.findByRole("button", { name: "Save Settings" });
    expect(inputFor(HOUR_LABEL).value).toBe("6");

    fireEvent.change(inputFor(HOUR_LABEL), { target: { value: "" } });
    expect(inputFor(HOUR_LABEL).value).toBe("6");

    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));
    const [, body] = mockApiPut.mock.calls[0];
    expect((body as SettingsConfig).daily_cycle_hour).toBe(6);
  });

  it("renders a NULL timezone/hour as empty boxes and transmits the null unchanged", async () => {
    mockConfigGet(wireConfig);
    mockApiPut.mockResolvedValue(wireConfig);

    render(<SettingsPage />);
    await screen.findByRole("button", { name: "Save Settings" });

    expect(inputFor(HOUR_LABEL).value).toBe("");
    expect(inputFor("Timezone").value).toBe("");

    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));
    const [, body] = mockApiPut.mock.calls[0];
    expect((body as SettingsConfig).daily_cycle_hour).toBeNull();
    expect((body as SettingsConfig).timezone).toBeNull();
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
});
