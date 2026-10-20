/**
 * V26 / L2 — Settings → Integrations (RED-first).
 *
 * Written BEFORE `components/settings/IntegrationsSection.tsx` and
 * `components/integrations/*` exist, so on the base tree this file fails at import
 * ("Cannot find module '@/components/settings/IntegrationsSection'") — the honest RED state
 * for a section that is absent.
 *
 * WHAT THIS PINS:
 *  - the section renders in Settings and fires NO request on mount (the shipped law:
 *    `u11-settings-sections.test.tsx:267` pins that opening Settings issues exactly ["/config"]);
 *  - "Check status" reads `GET /integrations` and renders every provider with a
 *    Configured/Missing badge and the server's masked hint — a token is never rendered;
 *  - saving sends the value to `PUT /integrations/{provider}` and clears the draft box; the
 *    token value never appears anywhere in the DOM afterwards;
 *  - a failed test-connection renders the server's NAMED refusal (`E_AUTH` …), never "connected";
 *  - clearing (two-step confirm) calls `DELETE /integrations/{provider}` and flips the badge;
 *  - a read-only provider (`stripe`) renders no inputs and no Save/Clear — status only.
 *
 * The ONLY fake is the api seam (`@/lib/api` mocked); every assertion runs against the real
 * component tree rendered by React Testing Library.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ApiError, api } from "@/lib/api";
import SettingsPage, { type SettingsConfig } from "@/app/settings/page";
import { IntegrationsSection } from "@/components/settings/IntegrationsSection";
import type { ProviderList, ProviderStatus } from "@/components/integrations/types";

const mockGet = api.get as jest.Mock;
const mockPut = api.put as jest.Mock;
const mockPost = api.post as jest.Mock;
const mockDelete = api.delete as jest.Mock;

const FAKE_GITHUB = "fake-github-token-v26-do-not-use";

function provider(overrides: Partial<ProviderStatus> & Pick<ProviderStatus, "provider" | "label">): ProviderStatus {
  return {
    configured: false,
    source: "none",
    read_only: false,
    testable: false,
    secrets: [],
    ...overrides,
  };
}

const MISSING: ProviderList = {
  providers: [
    provider({
      provider: "github",
      label: "GitHub",
      testable: true,
      secrets: [{ name: "GITHUB_TOKEN", configured: false, source: "none", masked: "…" }],
    }),
    provider({
      provider: "render",
      label: "Render",
      testable: true,
      secrets: [
        { name: "RENDER_API_KEY", configured: false, source: "none", masked: "…" },
        { name: "RENDER_SERVICE_ID", configured: false, source: "none", masked: "…" },
      ],
    }),
    provider({
      provider: "sendgrid",
      label: "SendGrid",
      secrets: [{ name: "SENDGRID_API_KEY", configured: false, source: "none", masked: "…" }],
    }),
    provider({
      provider: "stripe",
      label: "Stripe",
      read_only: true,
      secrets: [
        { name: "STRIPE_SECRET_KEY", configured: false, source: "none", masked: "…" },
        { name: "STRIPE_WEBHOOK_SECRET", configured: false, source: "none", masked: "…" },
      ],
    }),
    provider({
      provider: "meta_ads",
      label: "Meta Ads",
      read_only: true,
      secrets: [{ name: "META_ADS_ACCESS_TOKEN", configured: false, source: "none", masked: "…" }],
    }),
    provider({
      provider: "google_ads",
      label: "Google Ads",
      read_only: true,
      secrets: [{ name: "GOOGLE_ADS_DEVELOPER_TOKEN", configured: false, source: "none", masked: "…" }],
    }),
  ],
};

const GITHUB_CONFIGURED: ProviderStatus = provider({
  provider: "github",
  label: "GitHub",
  configured: true,
  source: "stored",
  testable: true,
  secrets: [{ name: "GITHUB_TOKEN", configured: true, source: "stored", masked: "ghp…6789" }],
});

const GITHUB_MISSING: ProviderStatus = MISSING.providers[0];

const baseConfig: SettingsConfig = {
  name: "Polsia",
  mission: null,
  vision: null,
  description: null,
  target_market: null,
  value_prop: null,
  website_url: "https://placeholder.example",
  industry: null,
  product_type: null,
  timezone: null,
  daily_cycle_hour: null,
  night_task_enabled: false,
  night_task_hour: null,
  night_task_limit: 3,
  notification_email: "owner@example.com",
  notify_on_completed: false,
  notify_on_failed: false,
  notify_on_awaiting_approval: false,
  notify_on_run_requeued: false,
  dispatch_paused: false,
  sender_legal_name: "",
  sender_postal_address: "",
};

async function loadSection(): Promise<void> {
  render(<IntegrationsSection />);
  fireEvent.click(screen.getByTestId("settings-integrations-load"));
  await screen.findByTestId("integration-card-github");
}

beforeEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
  mockPost.mockReset();
  mockDelete.mockReset();
});

describe("v26 integrations — mount honesty", () => {
  it("renders the panel and fires NO request until asked", () => {
    render(<IntegrationsSection />);
    expect(screen.getByRole("heading", { name: "Integrations" })).toBeInTheDocument();
    expect(mockGet).not.toHaveBeenCalled();
    expect(mockPut).not.toHaveBeenCalled();
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockDelete).not.toHaveBeenCalled();
    expect(screen.getByTestId("settings-integrations-load")).toBeInTheDocument();
  });

  it("is composed into Settings without adding a fifth tab or an extra mount read", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/config") return Promise.resolve(baseConfig);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    render(<SettingsPage />);
    await screen.findByRole("tab", { name: "Company Profile" });
    expect(screen.getByRole("heading", { name: "Integrations" })).toBeInTheDocument();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Company Profile",
      "Scheduler",
      "Emails",
      "DeepSeek Key",
    ]);
    expect(mockGet.mock.calls.map((call) => call[0])).toEqual(["/config"]);
  });
});

describe("v26 integrations — status, save, test, clear", () => {
  it("reads masked status for all six providers and never a value", async () => {
    mockGet.mockResolvedValue(MISSING);
    await loadSection();

    expect(mockGet).toHaveBeenCalledWith("/integrations");
    for (const entry of MISSING.providers) {
      expect(screen.getByTestId(`integration-card-${entry.provider}`)).toBeInTheDocument();
    }
    const github = screen.getByTestId("integration-card-github");
    expect(within(github).getByTestId("integration-badge-github")).toHaveTextContent("Missing");
    expect(within(github).getByText("…")).toBeInTheDocument();
    expect(within(github).getByText("GITHUB_TOKEN")).toBeInTheDocument();
  });

  it("saves a token, flips the badge to Configured and never renders the value", async () => {
    mockGet.mockResolvedValue(MISSING);
    mockPut.mockResolvedValue(GITHUB_CONFIGURED);
    await loadSection();

    const github = screen.getByTestId("integration-card-github");
    fireEvent.change(within(github).getByLabelText("GITHUB_TOKEN"), {
      target: { value: FAKE_GITHUB },
    });
    fireEvent.click(within(github).getByRole("button", { name: "Save" }));

    await screen.findByText("Configured");
    expect(mockPut).toHaveBeenCalledWith("/integrations/github", {
      values: { GITHUB_TOKEN: FAKE_GITHUB },
    });
    expect(screen.queryByText(FAKE_GITHUB)).toBeNull();
    expect(document.body.textContent).not.toContain(FAKE_GITHUB);
    expect(within(github).getByText("ghp…6789")).toBeInTheDocument();
    expect((within(github).getByLabelText("GITHUB_TOKEN") as HTMLInputElement).value).toBe("");
  });

  it("renders the server's NAMED refusal when test-connection fails", async () => {
    mockGet.mockResolvedValue({
      providers: MISSING.providers.map((entry) =>
        entry.provider === "github" ? GITHUB_CONFIGURED : entry,
      ),
    });
    mockPost.mockRejectedValue(
      new ApiError(
        401,
        "github: the credential was refused (HTTP 401)",
        "E_AUTH",
        "re-enter a valid github credential",
      ),
    );
    await loadSection();

    const github = screen.getByTestId("integration-card-github");
    fireEvent.click(within(github).getByRole("button", { name: "Test connection" }));

    const result = await screen.findByTestId("integration-test-result-github");
    expect(result).toHaveTextContent("E_AUTH");
    expect(result).toHaveTextContent("401");
    expect(result).toHaveTextContent(/refused/i);
    expect(mockPost).toHaveBeenCalledWith("/integrations/github/test");
    expect(screen.queryByText(/connected/i)).toBeNull();
  });

  it("clears through a two-step confirm and flips the badge back to Missing", async () => {
    mockGet.mockResolvedValue({
      providers: MISSING.providers.map((entry) =>
        entry.provider === "github" ? GITHUB_CONFIGURED : entry,
      ),
    });
    mockDelete.mockResolvedValue(GITHUB_MISSING);
    await loadSection();

    const github = screen.getByTestId("integration-card-github");
    fireEvent.click(within(github).getByRole("button", { name: "Clear stored" }));
    fireEvent.click(within(github).getByRole("button", { name: "Confirm clear" }));

    await waitFor(() =>
      expect(within(github).getByTestId("integration-badge-github")).toHaveTextContent("Missing"),
    );
    expect(mockDelete).toHaveBeenCalledWith("/integrations/github");
  });

  it("offers no inputs and no Save/Clear on a read-only provider", async () => {
    mockGet.mockResolvedValue(MISSING);
    await loadSection();

    const stripe = screen.getByTestId("integration-card-stripe");
    expect(within(stripe).getByText("Read-only")).toBeInTheDocument();
    expect(within(stripe).queryByLabelText("STRIPE_SECRET_KEY")).toBeNull();
    expect(within(stripe).queryByLabelText("STRIPE_WEBHOOK_SECRET")).toBeNull();
    expect(within(stripe).queryByRole("button", { name: "Save" })).toBeNull();
    expect(within(stripe).queryByRole("button", { name: "Clear stored" })).toBeNull();
  });
});
