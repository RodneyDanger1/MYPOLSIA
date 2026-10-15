import { fireEvent, render, screen, waitFor } from "@testing-library/react";

/**
 * WAVE V11 / W4 — the Settings completion surfaces: sender identity in Settings → Company (B13),
 * the Data export + guarded wipe (F3), the read-only Pricing snapshot, and the Site package.
 *
 * Wire truth these tests pin (read from the backend before writing them):
 *  - GET/PUT /config serves/accepts `sender_legal_name` + `sender_postal_address`
 *    (schemas/company.py:59-60,126-127) and PUT copies ONLY the provided keys
 *    (`exclude_unset`) — the page must send exactly what changed;
 *  - GET /data/export -> application/zip (data.py:286) — raw fetch + blob download (lib/api.ts
 *    is JSON-only by construction);
 *  - DELETE /data?dry_run=true -> would_delete/total_rows/kept/action_fingerprint; the wipe is
 *    DELETE /data?confirm_token=<token> and the token is minted via POST /approvals
 *    {action_fingerprint} (data.py:416, approvals.py:103) — single-use, refusals by name;
 *  - GET /pricing -> {query,count,items,catalog_as_of,staleness_days,stale} (pricing.py:25) with
 *    nullable rows; `stale: null` is "age unknown", never "fresh";
 *  - POST /site/package -> {package_dir, files, slug} (site.py:20) — server-side PATHS (no fake
 *    progress). V15/W2 (4-07, deliberate update): the GET download route ships too
 *    (site.py:29-45, `Content-Disposition: attachment; filename="site-<slug>.zip"`) and the
 *    section now wires the REAL blob download — the old "no HTTP download" copy was false.
 *
 * Shipped pins this file must respect: u3-settings-memory / u7-settings pin the tab list (exactly
 * four tabs) and "no control besides the config save and the DeepSeek section fires a request";
 * u7/pages tests query `role="alert"` SINGULARLY. So the new sections are click-only (nothing on
 * mount) and every assertion here uses the section's own testids.
 */

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import SettingsPage, { type SettingsConfig } from "@/app/settings/page";

const mockGet = api.get as jest.Mock;
const mockPut = api.put as jest.Mock;
const mockPost = api.post as jest.Mock;
const mockDelete = api.delete as jest.Mock;

const FETCH_MOCK = jest.fn();

const baseConfig: SettingsConfig = {
  name: "Polsia",
  mission: null,
  vision: null,
  description: null,
  target_market: null,
  value_prop: null,
  // The seeded placeholder case: displayed exactly as stored, per the brief.
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

type PricingRow = {
  service: string;
  price_low: number | null;
  price_high: number | null;
  model: string | null;
  hours: { low: number | null; high: number | null } | null;
  source_url: string | null;
  fetch_type: string | null;
  date: string | null;
  source_label: string | null;
  taxable_hint: string | null;
};
type PricingCatalog = {
  query: string | null;
  count: number;
  items: PricingRow[];
  catalog_as_of: string | null;
  staleness_days: number | null;
  stale: boolean | null;
};

/** Real row values copied from backend/app/data/pricing_catalog.json (first row + one hostile). */
const PRICING: PricingCatalog = {
  query: null,
  count: 2,
  items: [
    {
      service: "Local SEO — single location",
      price_low: 500.0,
      price_high: 1200.0,
      model: "retainer",
      hours: { low: 15, high: 25 },
      source_url: "https://arc4.com/resources/local-seo-pricing/",
      fetch_type: "F",
      date: "2026-10-14",
      source_label: "https://arc4.com/resources/local-seo-pricing/ (F)",
      taxable_hint: "service",
    },
    {
      service: "Mystery row (no stated price)",
      price_low: null,
      price_high: null,
      model: null,
      hours: null,
      source_url: null,
      fetch_type: null,
      date: "2026-10-14",
      source_label: null,
      taxable_hint: null,
    },
  ],
  catalog_as_of: "2026-10-14",
  staleness_days: 1,
  stale: false,
};

const DRY_RUN = {
  ok: true,
  dry_run: true,
  action_fingerprint: "polsia-data-wipe:v1:0123456789ab",
  would_delete: { tasks: 3, chat_messages: 2, memory_entries: 1 },
  total_rows: 6,
  kept: ["company_config", "deepseek_key_store"],
  how_to_execute:
    'mint the confirm token with POST /api/v1/approvals {"action_fingerprint": "polsia-data-wipe:v1:0123456789ab"}',
};

const TOKEN = "1799999999.deadbeef";

const MINT = {
  token: TOKEN,
  action_fingerprint: "polsia-data-wipe:v1:0123456789ab",
  expires_at: 1799999999,
  ttl_s: 3600,
};

function serveReads(config: SettingsConfig = baseConfig, pricing: PricingCatalog | null = null) {
  mockGet.mockImplementation((requestPath: string) => {
    if (requestPath === "/config") return Promise.resolve(config);
    if (requestPath === "/config/deepseek-key") {
      return Promise.resolve({ configured: false, source: "none" as const, masked: "" });
    }
    if (requestPath === "/pricing?limit=50" && pricing !== null) return Promise.resolve(pricing);
    return Promise.reject(new Error(`unexpected GET ${requestPath}`));
  });
}

async function renderSettings(config: SettingsConfig = baseConfig, pricing: PricingCatalog | null = null) {
  serveReads(config, pricing);
  render(<SettingsPage />);
  await screen.findByRole("tab", { name: "Company Profile" });
}

/** Walk the REAL wipe sequence to the confirm strip: dry run → mint → arm the wipe. */
async function armWipe() {
  mockDelete.mockResolvedValueOnce(DRY_RUN);
  fireEvent.click(screen.getByTestId("settings-data-dryrun"));
  await screen.findByText(/would remove 6 rows/);

  mockPost.mockResolvedValueOnce(MINT);
  fireEvent.click(screen.getByTestId("settings-data-mint"));
  await screen.findByTestId("settings-data-minted");

  fireEvent.click(screen.getByTestId("settings-data-wipe"));
  await screen.findByTestId("settings-data-confirm");
}

let anchorClick: jest.SpyInstance;

beforeEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
  mockPost.mockReset();
  mockDelete.mockReset();
  FETCH_MOCK.mockReset();
  global.fetch = FETCH_MOCK as unknown as typeof fetch;
  anchorClick = jest
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(() => undefined);
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    writable: true,
    value: jest.fn(() => "blob:data-export"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    writable: true,
    value: jest.fn(),
  });
});

afterEach(() => {
  anchorClick.mockRestore();
});

// -------------------------------------------------------------------------------------------
// B13 — the sender identity exists in Settings → Company and saves through the REAL PUT /config
// -------------------------------------------------------------------------------------------

describe("u11 settings — sender identity (B13)", () => {
  it("renders the wire values as-is and PUTs exactly the changed sender fields to /config", async () => {
    await renderSettings();
    // The seeded placeholder is shown verbatim — the UI does not invent or rewrite it.
    expect((screen.getByLabelText("Website URL") as HTMLInputElement).value).toBe(
      "https://placeholder.example",
    );
    const legal = screen.getByLabelText("Sender Legal Name") as HTMLInputElement;
    const postal = screen.getByLabelText("Sender Postal Address") as HTMLInputElement;
    expect(legal.value).toBe("");
    expect(postal.value).toBe("");

    fireEvent.change(legal, { target: { value: "Polsia LLC" } });
    fireEvent.change(postal, { target: { value: "123 Main St, Minneapolis, MN 55401" } });

    mockPut.mockResolvedValue({
      ...baseConfig,
      sender_legal_name: "Polsia LLC",
      sender_postal_address: "123 Main St, Minneapolis, MN 55401",
    });
    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    const [path, body] = mockPut.mock.calls[0];
    expect(path).toBe("/config");
    // Only the two changed keys leave — website_url (unchanged) is not rewritten.
    expect(body).toEqual({
      sender_legal_name: "Polsia LLC",
      sender_postal_address: "123 Main St, Minneapolis, MN 55401",
    });
    expect(await screen.findByText("Settings saved successfully")).toBeInTheDocument();
  });

  it("a refused sender-identity save renders the server's own refusal", async () => {
    await renderSettings();
    fireEvent.change(screen.getByLabelText("Sender Legal Name"), { target: { value: "X" } });
    mockPut.mockRejectedValue(
      new ApiError(503, "company configuration has not been seeded yet", "E_CONFIG", "run the seed step"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_CONFIG");
    expect(alert).toHaveTextContent("run the seed step");
    expect(screen.queryByText("Settings saved successfully")).toBeNull();
  });
});

// -------------------------------------------------------------------------------------------
// Mount honesty — none of the new sections fires a request until asked (shipped ACC-6 pin)
// -------------------------------------------------------------------------------------------

describe("u11 settings — nothing auto-runs", () => {
  it("mount issues no DELETE/POST, no export fetch and no pricing read", async () => {
    await renderSettings(baseConfig, PRICING);
    expect(mockGet.mock.calls.map((call) => call[0])).toEqual(["/config"]);
    expect(mockDelete).not.toHaveBeenCalled();
    expect(mockPost).not.toHaveBeenCalled();
    expect(FETCH_MOCK).not.toHaveBeenCalled();
    expect(screen.queryByTestId("settings-data-wiped")).toBeNull();
  });
});

// -------------------------------------------------------------------------------------------
// Data — export (GET /data/export, REAL zip response) + the guarded wipe (the REAL flow)
// -------------------------------------------------------------------------------------------

describe("u11 settings — data export & wipe", () => {
  it("the export downloads the REAL ZIP response (GET /api/v1/data/export)", async () => {
    await renderSettings();
    FETCH_MOCK.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      headers: { get: (name: string) => (name === "X-Polsia-Row-Count" ? "42" : null) },
      blob: () => Promise.resolve(new Blob(["zip-bytes"], { type: "application/zip" })),
      text: () => Promise.resolve(""),
    });

    fireEvent.click(screen.getByTestId("settings-data-export"));

    await waitFor(() => expect(FETCH_MOCK).toHaveBeenCalledTimes(1));
    const [url] = FETCH_MOCK.mock.calls[0] as [string];
    expect(url).toMatch(/\/api\/v1\/data\/export$/);

    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("settings-data-export-done")).toHaveTextContent(
      "Exported 42 rows to polsia-data-export.zip.",
    );
  });

  it("a refused export renders the named refusal — never a dead button", async () => {
    await renderSettings();
    FETCH_MOCK.mockResolvedValue({
      ok: false,
      status: 503,
      statusText: "Service Unavailable",
      headers: { get: () => null },
      text: () =>
        Promise.resolve(
          JSON.stringify({
            ok: false,
            error: { code: "E_DB", message: "database unavailable", how_to_fix: "check postgres" },
          }),
        ),
      blob: () => Promise.resolve(new Blob([])),
    });

    fireEvent.click(screen.getByTestId("settings-data-export"));

    const alert = await screen.findByTestId("settings-data-export-error");
    expect(alert).toHaveTextContent("E_DB");
    expect(alert).toHaveTextContent("check postgres");
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it("the wipe follows the backend's exact order: dry run → mint → explicit confirm", async () => {
    await renderSettings();

    mockDelete.mockResolvedValueOnce(DRY_RUN);
    fireEvent.click(screen.getByTestId("settings-data-dryrun"));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/data?dry_run=true"));

    expect(await screen.findByText(/would remove 6 rows/)).toBeInTheDocument();
    expect(screen.getByText("tasks")).toBeInTheDocument();
    expect(screen.getByText("chat_messages")).toBeInTheDocument();
    expect(screen.getByText(/company_config, deepseek_key_store/)).toBeInTheDocument();
    // The fingerprint appears twice by design: its own line AND inside how_to_execute.
    expect(screen.getAllByText(/polsia-data-wipe:v1:0123456789ab/).length).toBeGreaterThanOrEqual(2);
    // No token yet: the wipe control is not even offered.
    expect(screen.queryByTestId("settings-data-wipe")).toBeNull();

    mockPost.mockResolvedValueOnce(MINT);
    fireEvent.click(screen.getByTestId("settings-data-mint"));
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/approvals", {
        action_fingerprint: "polsia-data-wipe:v1:0123456789ab",
      }),
    );
    expect(await screen.findByTestId("settings-data-minted")).toHaveTextContent("single-use");

    // First click only ARMES the confirm — the DELETE (with the token) must not have fired yet.
    fireEvent.click(screen.getByTestId("settings-data-wipe"));
    expect(mockDelete).toHaveBeenCalledTimes(1); // the dry run, and nothing more
    expect(await screen.findByTestId("settings-data-confirm")).toBeInTheDocument();
    expect(screen.getByTestId("settings-data-wipe-confirm")).toBeInTheDocument();

    mockDelete.mockResolvedValueOnce({
      ok: true,
      dry_run: false,
      action_fingerprint: DRY_RUN.action_fingerprint,
      deleted: { tasks: 3, chat_messages: 2, memory_entries: 1 },
      total_rows: 6,
      kept: ["company_config", "deepseek_key_store"],
    });
    fireEvent.click(screen.getByTestId("settings-data-wipe-confirm"));

    await waitFor(() => expect(mockDelete).toHaveBeenCalledTimes(2));
    expect(mockDelete.mock.calls[1][0]).toBe(`/data?confirm_token=${encodeURIComponent(TOKEN)}`);
    const wiped = await screen.findByTestId("settings-data-wiped");
    expect(wiped).toHaveTextContent("6 rows deleted");
    expect(wiped).toHaveTextContent("E_POLICY_DENIED"); // the single-use note
    // Token consumed: mint is back behind a fresh dry run, and no stale token is re-offered.
    expect(screen.queryByTestId("settings-data-minted")).toBeNull();
    expect(screen.getByTestId("settings-data-mint")).toBeDisabled();
    expect(screen.queryByTestId("settings-data-wipe")).toBeNull();
  });

  it("a refused wipe is rendered by name and deletes nothing", async () => {
    await renderSettings();
    await armWipe();

    mockDelete.mockRejectedValueOnce(
      new ApiError(
        403,
        "this confirm token was already used — it authorized the wipe recorded at an earlier time",
        "E_POLICY_DENIED",
        "run DELETE /api/v1/data?dry_run=true and mint a fresh token, then retry with the new confirm_token",
      ),
    );
    fireEvent.click(screen.getByTestId("settings-data-wipe-confirm"));

    const alert = await screen.findByTestId("settings-data-wipe-error");
    expect(alert).toHaveTextContent("E_POLICY_DENIED");
    expect(alert).toHaveTextContent("mint a fresh token");
    expect(screen.queryByTestId("settings-data-wiped")).toBeNull();
  });

  it("an empty member set disables mint + wipe and says why (token NOT burned server-side)", async () => {
    await renderSettings();
    mockDelete.mockResolvedValueOnce({
      ...DRY_RUN,
      would_delete: { tasks: 0 },
      total_rows: 0,
    });
    fireEvent.click(screen.getByTestId("settings-data-dryrun"));

    expect(await screen.findByTestId("settings-data-empty")).toHaveTextContent("E_VALIDATION");
    expect(screen.getByTestId("settings-data-mint")).toBeDisabled();
    expect(screen.queryByTestId("settings-data-wipe")).toBeNull();
    fireEvent.click(screen.getByTestId("settings-data-mint"));
    expect(mockPost).not.toHaveBeenCalled();
  });
});

// -------------------------------------------------------------------------------------------
// Pricing — read-only snapshot, null-safe provenance
// -------------------------------------------------------------------------------------------

describe("u11 settings — pricing snapshot", () => {
  it("loads GET /pricing?limit=50 and renders the dated snapshot null-safely", async () => {
    await renderSettings(baseConfig, PRICING);
    fireEvent.click(screen.getByTestId("settings-pricing-load"));

    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/pricing?limit=50"));
    expect(await screen.findByText("Local SEO — single location")).toBeInTheDocument();
    expect(screen.getByText("$500–$1,200")).toBeInTheDocument();
    expect(screen.getByText("15–25 hrs")).toBeInTheDocument();
    expect(screen.getByText("retainer")).toBeInTheDocument();
    // The hostile row: absent price/model/hours/source stay em dashes — never fabricated zeros.
    expect(screen.getByText("Mystery row (no stated price)")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(4);
    expect(screen.queryByText("$0")).toBeNull();
    expect(await screen.findByTestId("settings-pricing-provenance")).toHaveTextContent(
      "2026-10-14 — 1 day old",
    );
    expect(screen.queryByTestId("settings-pricing-stale")).toBeNull();
  });

  it("a null as-of never reads as fresh; stale:true raises the re-verify alarm", async () => {
    await renderSettings(baseConfig, {
      ...PRICING,
      catalog_as_of: null,
      staleness_days: null,
      stale: null,
    });
    fireEvent.click(screen.getByTestId("settings-pricing-load"));

    expect(await screen.findByTestId("settings-pricing-provenance")).toHaveTextContent(
      "as-of date unknown",
    );
    expect(screen.getByText(/Staleness cannot be computed/)).toBeInTheDocument();
    expect(screen.queryByTestId("settings-pricing-stale")).toBeNull();

    // A second read with a stale verdict: the alarm appears.
    mockGet.mockImplementation((requestPath: string) => {
      if (requestPath === "/config") return Promise.resolve(baseConfig);
      if (requestPath === "/config/deepseek-key") {
        return Promise.resolve({ configured: false, source: "none" as const, masked: "" });
      }
      if (requestPath === "/pricing?limit=50") {
        return Promise.resolve({ ...PRICING, catalog_as_of: "2026-01-01", staleness_days: 287, stale: true });
      }
      return Promise.reject(new Error(`unexpected GET ${requestPath}`));
    });
    fireEvent.click(screen.getByTestId("settings-pricing-load"));
    expect(await screen.findByTestId("settings-pricing-stale")).toHaveTextContent("STALE");
  });

  it("a refused pricing read renders the server's named refusal", async () => {
    await renderSettings(baseConfig, null);
    mockGet.mockImplementation((requestPath: string) =>
      requestPath === "/pricing?limit=50"
        ? Promise.reject(
            new ApiError(
              503,
              "pricing catalog must carry exactly 50 rows, found 12",
              "E_CONFIG",
              "restore the frozen catalog file",
            ),
          )
        : Promise.reject(new Error(`unexpected GET ${requestPath}`)),
    );
    fireEvent.click(screen.getByTestId("settings-pricing-load"));

    const alert = await screen.findByTestId("settings-pricing-error");
    expect(alert).toHaveTextContent("E_CONFIG");
    expect(alert).toHaveTextContent("restore the frozen catalog file");
  });

  it("a zero-row snapshot renders the empty state — never a fabricated table", async () => {
    await renderSettings(baseConfig, { ...PRICING, count: 0, items: [] });
    fireEvent.click(screen.getByTestId("settings-pricing-load"));

    expect(await screen.findByTestId("settings-pricing-empty")).toBeInTheDocument();
    expect(screen.getByText(/0 rows served/)).toBeInTheDocument();
  });
});

// -------------------------------------------------------------------------------------------
// Site package — the REAL response (paths), no fake download, no fake progress
// -------------------------------------------------------------------------------------------

describe("u11 settings — site package", () => {
  it("builds via POST /site/package and reports the REAL response — paths, not an archive", async () => {
    await renderSettings();
    mockPost.mockResolvedValueOnce({
      package_dir: "C:\\Polsia\\backend\\data\\exports\\site-polsia",
      files: [
        "C:\\Polsia\\backend\\data\\exports\\site-polsia\\index.html",
        "C:\\Polsia\\backend\\data\\exports\\site-polsia\\HOSTING-README.md",
      ],
      slug: "polsia",
    });

    fireEvent.click(screen.getByTestId("settings-site-build"));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/site/package"));

    const result = await screen.findByTestId("settings-site-result");
    expect(result).toHaveTextContent("polsia");
    expect(result).toHaveTextContent("index.html");
    expect(result).toHaveTextContent("HOSTING-README.md");
    // V15/W2 (4-07) DELIBERATE UPDATE: the old copy claimed "no HTTP download" — false since the
    // GET route ships. The truth is asserted instead: the ZIP download control exists, its route
    // is named, and nothing is published by building or downloading.
    expect(result).not.toHaveTextContent(/no HTTP download/);
    expect(screen.getByTestId("settings-site-download")).toBeInTheDocument();
    expect(
      screen.getByText(/Download it as a ZIP \(GET \/api\/v1\/site\/package\/download\)/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/nothing is published by\s+building or downloading it/),
    ).toBeInTheDocument();
    // No fake progress affordance anywhere on this page.
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("a refused build renders E_CONFIG with the server's missing-field fix", async () => {
    await renderSettings();
    mockPost.mockRejectedValueOnce(
      new ApiError(
        503,
        "site package needs the company contact (notification_email)",
        "E_CONFIG",
        "set the missing company field(s) in settings (PUT /api/v1/config) and retry",
      ),
    );

    fireEvent.click(screen.getByTestId("settings-site-build"));

    const alert = await screen.findByTestId("settings-site-error");
    expect(alert).toHaveTextContent("E_CONFIG");
    expect(alert).toHaveTextContent("notification_email");
    expect(screen.queryByTestId("settings-site-result")).toBeNull();
  });
});
