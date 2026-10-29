/**
 * WAVE 7 — the LOGIN/EMAIL call paths on screen: every state honest, no dead controls.
 *
 * Two defects this suite pins RED→GREEN (both measured live on the wave-6 rebuild, see
 * `weaver/REPORT-W7.md` §2 and `raw/w7/drive-w7-baseline.cjs`):
 *
 *  1. **Settings → Emails with no saved address.** The four `notify_on_*` switches are stored flags
 *     the notification funnel reads AFTER it resolves a recipient
 *     (`notification_service.notify_owner` → `_configured_recipient` strips
 *     `company_config.notification_email` and answers `None`). With the address blank the funnel
 *     returns before composing anything: no email leaves, and unlike the cycle-report leg nothing
 *     is recorded in the activity feed. The screen used to say "one email per completed task, sent
 *     to the address above" with no address anywhere — a switch that reads ON and can never send.
 *     The fix names the no-address state.
 *
 *  2. **Settings → Integrations, the SendGrid card.** The badge/Test read the secret store
 *     (`secret_store.get_secret`: stored → env → Settings). The SEND path does not:
 *     `integrations.send_email` reads `settings.sendgrid_api_key` / `settings.sendgrid_from_email`
 *     (env only) — a live probe on 2026-10-29 stored a fake key and watched the send still refuse
 *     `E_INTEGRATION` naming SENDGRID_API_KEY, and `SENDGRID_FROM_EMAIL` is not on the card at all.
 *     Until that seam lands (R-W7-A1, provider-call owner), the card states the send path's own
 *     requirement instead of implying a stored value makes sending work.
 *
 * The ONLY fake is the api seam (`@/lib/api`); the assertions run against the real components.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

import { fireEvent, render, screen, within } from "@testing-library/react";

import { api } from "@/lib/api";
import SettingsPage, { type SettingsConfig } from "@/app/settings/page";
import { EmailsSection } from "@/components/settings/EmailsSection";
import { IntegrationsSection } from "@/components/settings/IntegrationsSection";
import type { ProviderList, ProviderStatus } from "@/components/integrations/types";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

function config(overrides: Partial<SettingsConfig> = {}): SettingsConfig {
  return {
    name: "Cinder & Lye",
    mission: null,
    vision: null,
    description: null,
    target_market: null,
    value_prop: null,
    website_url: "https://cinder-lye.example",
    industry: null,
    product_type: null,
    timezone: null,
    daily_cycle_hour: null,
    night_task_enabled: false,
    night_task_hour: null,
    night_task_limit: 3,
    notification_email: null,
    notify_on_completed: false,
    notify_on_failed: false,
    notify_on_awaiting_approval: false,
    notify_on_run_requeued: false,
    dispatch_paused: false,
    sender_legal_name: "",
    sender_postal_address: "",
    ...overrides,
  };
}

function provider(
  overrides: Partial<ProviderStatus> & Pick<ProviderStatus, "provider" | "label">,
): ProviderStatus {
  return {
    configured: false,
    source: "none",
    read_only: false,
    testable: false,
    secrets: [],
    ...overrides,
  };
}

async function openEmailsTab(serverConfig: SettingsConfig): Promise<void> {
  mockGet.mockImplementation((requestPath: string) => {
    if (requestPath === "/config") return Promise.resolve(serverConfig);
    return Promise.reject(new Error(`unexpected GET ${requestPath}`));
  });
  render(<SettingsPage />);
  // Wait for the CONFIGURED page (its Save control only exists after GET /config answered): the
  // loading skeleton renders the same tab strip, and a tab node captured from it is replaced by
  // the loaded render — a click on a detached node does nothing.
  await screen.findByRole("button", { name: /save settings/i });
  fireEvent.click(screen.getByRole("tab", { name: "Emails" }));
  await screen.findByRole("tabpanel");
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

// ── 1. Settings → Emails: the no-address state ───────────────────────────────────────────────

describe("W7 — Settings → Emails names the no-address state", () => {
  it("says no address is saved (and what that means) when notification_email is blank", async () => {
    await openEmailsTab(config({ notification_email: null }));

    const note = await screen.findByTestId("settings-emails-no-address");
    expect(note.textContent).toMatch(/No notification address is saved/);
    // The measured consequence, not a scare: nothing is sent, nothing is recorded.
    expect(note.textContent).toMatch(/no email is sent/i);
    expect(note.textContent).toMatch(/no activity row records the skip/i);
    // The four switches are still there (the state is stated; the controls are not removed).
    expect(screen.getByLabelText("Notify on completed tasks")).toBeInTheDocument();
    expect(screen.getByLabelText("Notify on failed tasks")).toBeInTheDocument();
    expect(screen.getByLabelText("Notify on awaiting approval")).toBeInTheDocument();
    expect(screen.getByLabelText("Notify on requeued runs")).toBeInTheDocument();
  });

  it("a saved address removes the note (nothing is claimed about a configured install)", async () => {
    await openEmailsTab(config({ notification_email: "owner@cinder-lye.example" }));

    // Wait for the section's OWN read (the cycle-report switch renders only after it answers), so
    // the absence assertion is about a configured address, not about "not read yet".
    await screen.findByLabelText("Email me each cycle report");
    expect(screen.queryByTestId("settings-emails-no-address")).toBeNull();
  });

  it("a whitespace-only address is the server's own 'not configured' — the note stays", async () => {
    await openEmailsTab(config({ notification_email: "   " }));

    expect(await screen.findByTestId("settings-emails-no-address")).toBeInTheDocument();
  });

  it("the section alone (unit): blank vs saved address, no other read required", () => {
    const values = {
      notify_on_completed: false,
      notify_on_failed: false,
      notify_on_awaiting_approval: false,
      notify_on_run_requeued: false,
    };
    mockGet.mockResolvedValue({ cycle_report_email: false });
    const { rerender } = render(
      <EmailsSection values={values} onChange={() => undefined} notificationEmail="" />,
    );
    expect(screen.getByTestId("settings-emails-no-address")).toBeInTheDocument();

    rerender(
      <EmailsSection
        values={values}
        onChange={() => undefined}
        notificationEmail="owner@cinder-lye.example"
      />,
    );
    expect(screen.queryByTestId("settings-emails-no-address")).toBeNull();
  });
});

// ── 2. Settings → Integrations: the SendGrid card names the SEND path ────────────────────────

describe("W7 — the SendGrid card states the send path's requirement", () => {
  const providers: ProviderList = {
    providers: [
      provider({
        provider: "sendgrid",
        label: "SendGrid",
        secrets: [{ name: "SENDGRID_API_KEY", configured: false, source: "none", masked: "…" }],
      }),
      provider({
        provider: "stripe",
        label: "Stripe",
        configured: true,
        source: "env",
        read_only: true,
        secrets: [{ name: "STRIPE_SECRET_KEY", configured: true, source: "env", masked: "sk_…1234" }],
      }),
    ],
  };

  async function load(): Promise<void> {
    mockGet.mockResolvedValue(providers);
    render(<IntegrationsSection />);
    fireEvent.click(screen.getByTestId("settings-integrations-load"));
    await screen.findByTestId("integration-card-sendgrid");
  }

  it("names the required pair and says the send path resolves the same store", async () => {
    await load();

    const note = within(screen.getByTestId("integration-card-sendgrid")).getByTestId(
      "integration-send-path-sendgrid",
    );
    expect(note.textContent).toMatch(/draft-only/i);
    expect(note.textContent).toMatch(/SENDGRID_API_KEY/);
    // The variable the card does NOT carry — the half a "Configured" badge cannot know about.
    expect(note.textContent).toMatch(/SENDGRID_FROM_EMAIL/);
    // W7/F-W7-01 FIXED: the adapter now reads the secret store; the honest remaining gap is
    // SENDGRID_FROM_EMAIL, which the card does not carry. OLD copy ("a value saved here does not
    // reach the send path") is no longer true and was re-pinned deliberately.
    expect(note.textContent).toMatch(/resolves the same store/);
    expect(note.textContent).toMatch(/refused by name \(E_INTEGRATION/);
    // F-47 FIXED: both SendGrid values are storable card secrets now; the note names the pair.
    expect(note.textContent).toMatch(/requires both\s+SENDGRID_API_KEY and SENDGRID_FROM_EMAIL/);
  });

  it("is scoped to SendGrid — no other provider card carries an email note", async () => {
    await load();

    expect(
      within(screen.getByTestId("integration-card-stripe")).queryByTestId(
        "integration-send-path-sendgrid",
      ),
    ).toBeNull();
    expect(screen.getAllByTestId("integration-send-path-sendgrid")).toHaveLength(1);
  });

  it("the Test refusal is still rendered by name (the honest path that already worked)", async () => {
    mockGet.mockResolvedValue(providers);
    mockPost.mockRejectedValue(new Error("E_CONFIG: sendgrid: SENDGRID_API_KEY is not configured"));
    render(<IntegrationsSection />);
    fireEvent.click(screen.getByTestId("settings-integrations-load"));
    const card = await screen.findByTestId("integration-card-sendgrid");
    fireEvent.click(within(card).getByRole("button", { name: /test connection/i }));

    const result = await screen.findByTestId("integration-test-result-sendgrid");
    expect(result.textContent).toMatch(/Test refused/);
    expect(result.textContent).toMatch(/SENDGRID_API_KEY is not configured/);
    expect(result.textContent).not.toMatch(/Connected/);
  });
});
