import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { NOT_MEASURED, absoluteStamp } from "@/components/dashboard/format";
import AdsPage from "@/app/ads/page";

/**
 * Wave U4 — coder A's own suite for the rebuilt Ads page (audit Q-17: `app/ads/page.tsx` had NO
 * test file at all).
 *
 * Scope discipline: run with `node node_modules/jest/bin/jest.js u4-ads` (sibling lanes hold files
 * mid-edit; the full suite is not this lane's to run).
 *
 * What is pinned here, all against REAL wire shapes (`AdCampaignResponse`,
 * `backend/app/schemas/social.py`; `GET /ads/campaigns`, `backend/app/api/v1/ads.py`):
 *  - the three disjoint states: aria-busy skeleton while reading (the string "—" appears nowhere
 *    in flight), a named failure that REPLACES the rows, and the honest empty ONLY on a settled
 *    read that returned nothing;
 *  - the money law via the shared finance family: null/NaN/strings are the not-measured marker,
 *    a measured 0 is `$0.00`, `1234.5` is `$1,234.50` (the Q-13 collapse cannot come back);
 *  - the nullable status vocabulary: null renders the `—` marker (never a fabricated word), the
 *    writer-asserted values render themselves, an unknown non-null word renders raw (no masquerade);
 *  - the Q-12 cap disclosure at >= 50 rows and its absence below;
 *  - every filter control re-issues the REAL query params (proven through `api.get` call args);
 *  - hostile bodies: a null body, a non-list body, missing fields — each named, never a crash.
 */

/** The typed mock handle; the real `api.get` is generic, this suite answers per path. */
const mockApiGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;

const LIMIT = 50;

/** One REAL wire row (`AdCampaignResponse` shape), fields overridable for the hostile cases. */
function campaign(id: number, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id,
    platform: "meta",
    external_id: null,
    name: `campaign ${id}`,
    goal: null,
    status: "active",
    daily_budget_usd: 12.5,
    total_spent_usd: 3.25,
    created_at: "2026-10-13T18:21:39Z",
    ...overrides,
  };
}

type Wire = {
  /** Answer for the unfiltered read; presence-decided, so an explicit `null` body stays null. */
  campaigns?: unknown;
  /** Answer for a read that carries `platform=meta`. */
  byPlatform?: unknown;
  /** Answer for a read that carries `status=`. */
  byStatus?: unknown;
};

function wire(reads: Wire = {}): void {
  mockApiGet.mockImplementation((path: string) => {
    if (!path.startsWith("/ads/campaigns")) {
      return Promise.reject(new Error(`unexpected path in test: ${path}`));
    }
    if (path.includes("platform=")) {
      return Promise.resolve("byPlatform" in reads ? reads.byPlatform : []);
    }
    if (path.includes("status=")) {
      return Promise.resolve("byStatus" in reads ? reads.byStatus : []);
    }
    return Promise.resolve("campaigns" in reads ? reads.campaigns : []);
  });
}

function lastPath(): string {
  const call = mockApiGet.mock.calls.at(-1);
  if (!call) throw new Error("no read was issued at all");
  return String(call[0]);
}

function bodyText(): string {
  return document.body.textContent ?? "";
}

function rows(): HTMLElement[] {
  return screen.queryAllByTestId("campaign-row");
}

beforeEach(() => {
  mockApiGet.mockReset();
});

/* ---------------------------------------------------------------------------
 * Three states — loading (Q-17 / Q-06 class)
 * ------------------------------------------------------------------------- */

describe("Ads — the loading state is a skeleton, never an empty page", () => {
  it("a never-resolving read renders the aria-busy skeleton and NO not-measured marker anywhere", () => {
    mockApiGet.mockImplementation(() => new Promise<never>(() => undefined));

    render(<AdsPage />);

    expect(screen.getByTestId("ads-loading")).toHaveAttribute("aria-busy", "true");
    expect(screen.getAllByTestId("ads-skeleton-row")).toHaveLength(3);
    expect(rows()).toHaveLength(0);
    // "not read yet" is not "nothing measured" — the em dash must not stand in for the read.
    expect(bodyText()).not.toContain(NOT_MEASURED);
    expect(bodyText()).toContain("Loading…");
    // No count is claimed before a read settles.
    expect(screen.queryByTestId("ads-count")).toBeNull();
  });

  it("a resolved payload replaces every skeleton with real rows and the measured count", async () => {
    wire({ campaigns: [campaign(2), campaign(1, { platform: "google" })] });

    render(<AdsPage />);

    await screen.findByTestId("ads-count");
    expect(screen.queryByTestId("ads-loading")).toBeNull();
    expect(rows()).toHaveLength(2);
    expect(screen.getByTestId("ads-count")).toHaveTextContent("2 campaigns");
    // The read really asked the real route, with the explicit bounded limit.
    expect(lastPath()).toBe(`/ads/campaigns?limit=${LIMIT}`);
  });
});

/* ---------------------------------------------------------------------------
 * Three states — failure replaced by its name
 * ------------------------------------------------------------------------- */

describe("Ads — a failed read is named and the value rows are replaced", () => {
  it("an ApiError shows its display (code + message + fix hint) and ZERO campaign rows", async () => {
    mockApiGet.mockRejectedValue(
      new ApiError(503, "backing store refused", "E_REDIS", "check the redis service"),
    );

    render(<AdsPage />);

    const alert = await screen.findByText(/Could not load the campaign list/);
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("E_REDIS: backing store refused — check the redis service");
    expect(rows()).toHaveLength(0);
    expect(screen.queryByTestId("ads-loading")).toBeNull();
    // No dash row beneath the banner (Q-09 class).
    expect(screen.queryAllByText(NOT_MEASURED)).toHaveLength(0);
  });

  it("a non-ApiError failure still shows its own words, never a generic apology", async () => {
    mockApiGet.mockRejectedValue(new Error("socket closed"));

    render(<AdsPage />);

    const alert = await screen.findByText(/Could not load the campaign list/);
    expect(alert).toHaveTextContent("socket closed");
  });

  it("a 200 with a NULL body is a named failure, never an endless skeleton (hostile body)", async () => {
    wire({ campaigns: null });

    render(<AdsPage />);

    const alert = await screen.findByText(/Could not load the campaign list/);
    expect(alert).toHaveTextContent("the server returned an empty body");
    expect(screen.queryByTestId("ads-loading")).toBeNull();
    expect(rows()).toHaveLength(0);
  });

  it("a body that is not a list is named, not rendered (hostile body)", async () => {
    wire({ campaigns: { rows: [] } });

    render(<AdsPage />);

    const alert = await screen.findByText(/Could not load the campaign list/);
    expect(alert).toHaveTextContent("the server returned no list of rows");
    expect(rows()).toHaveLength(0);
  });
});

/* ---------------------------------------------------------------------------
 * The vocabulary — platform chip, status across the real nullable set
 * ------------------------------------------------------------------------- */

describe("Ads — the row vocabulary is the wire's, never a fabrication", () => {
  it("a row renders its real fields: name, platform chip, named status chip, created stamp, goal, external id", async () => {
    wire({
      campaigns: [
        campaign(7, {
          name: "Summer sale",
          platform: "meta",
          status: "active",
          goal: "signups",
          external_id: "cmp_123",
          created_at: "2026-10-13T18:21:39Z",
        }),
      ],
    });

    render(<AdsPage />);

    const row = (await screen.findByTestId("campaign-row")) as HTMLElement;
    expect(row).toHaveTextContent("Summer sale");
    expect(screen.getByTestId("campaign-platform")).toHaveTextContent("meta");
    const status = screen.getByTestId("campaign-status");
    expect(status).toHaveTextContent("active");
    expect(status).toHaveAttribute("data-status-tone", "active");
    expect(screen.getByTestId("campaign-created")).toHaveTextContent(
      `Created ${absoluteStamp("2026-10-13T18:21:39Z")}`,
    );
    expect(row).toHaveTextContent("signups");
    expect(row).toHaveTextContent("cmp_123");
    expect(rows()).toHaveLength(1);
  });

  it("a NULL status renders the not-measured marker, never a fabricated word", async () => {
    wire({ campaigns: [campaign(1, { status: null })] });

    render(<AdsPage />);

    const status = await screen.findByTestId("campaign-status");
    expect(status).toHaveTextContent(NOT_MEASURED);
    expect(status).toHaveAttribute("data-status-tone", "unmeasured");
    // No word stands in for the missing status — not "active", not "unknown".
    expect(status.textContent).toBe(NOT_MEASURED);
  });

  it("an unknown non-null status renders its RAW word (no masquerade, a distinct tone)", async () => {
    wire({ campaigns: [campaign(1, { status: "archived" })] });

    render(<AdsPage />);

    const status = await screen.findByTestId("campaign-status");
    expect(status).toHaveTextContent("archived");
    expect(status).toHaveAttribute("data-status-tone", "other");
  });

  it("a NULL created_at is 'date unknown' — never 'Invalid Date', never a deleted cell", async () => {
    wire({ campaigns: [campaign(1, { created_at: null })] });

    render(<AdsPage />);

    const stamp = await screen.findByTestId("campaign-created");
    expect(stamp).toHaveTextContent("date unknown");
    expect(bodyText()).not.toContain("Invalid Date");
  });

  it("a row missing platform/name entirely renders the marker, not an empty card (hostile row)", async () => {
    wire({ campaigns: [{ id: 9, status: "active", total_spent_usd: null, daily_budget_usd: null }] });

    render(<AdsPage />);

    const row = (await screen.findByTestId("campaign-row")) as HTMLElement;
    expect(screen.getByTestId("campaign-platform")).toHaveTextContent(NOT_MEASURED);
    expect(row.textContent ?? "").not.toBe("");
  });
});

/* ---------------------------------------------------------------------------
 * The money law (Q-13)
 * ------------------------------------------------------------------------- */

describe("Ads — money formatting: null never $0.00, no toFixed on null (Q-13)", () => {
  it("null spent/budget render the marker; a measured 0 renders $0.00 — the two never collapse", async () => {
    wire({
      campaigns: [
        campaign(1, { total_spent_usd: null, daily_budget_usd: null, status: "paused" }),
        campaign(2, { total_spent_usd: 0, daily_budget_usd: 0, status: "ended" }),
      ],
    });

    render(<AdsPage />);

    const rendered = await screen.findAllByTestId("campaign-row");
    expect(rendered[0]).toHaveTextContent(NOT_MEASURED);
    expect(rendered[0]!.textContent ?? "").not.toContain("$0.00");
    expect(rendered[1]).toHaveTextContent("$0.00");
  });

  it("dollars render with thousands separators and two decimals (the Q-13 collapse)", async () => {
    wire({ campaigns: [campaign(1, { total_spent_usd: 1234.5, daily_budget_usd: 9999 })] });

    render(<AdsPage />);

    const row = (await screen.findByTestId("campaign-row")) as HTMLElement;
    expect(row).toHaveTextContent("$1,234.50");
    expect(row).toHaveTextContent("$9,999.00");
    expect(row.textContent ?? "").not.toContain("$1234.50");
    expect(row.textContent ?? "").not.toContain("$9999");
  });

  it("hostile money (NaN, a string, Infinity) renders the marker — never $NaN", async () => {
    wire({
      campaigns: [
        campaign(1, { total_spent_usd: Number.NaN, daily_budget_usd: "12.5" as unknown as number }),
        campaign(2, { total_spent_usd: Number.POSITIVE_INFINITY }),
      ],
    });

    render(<AdsPage />);

    await screen.findAllByTestId("campaign-row");
    expect(bodyText()).not.toContain("NaN");
    expect(bodyText()).not.toContain("Infinity");
    expect(screen.getAllByText(NOT_MEASURED).length).toBeGreaterThanOrEqual(3);
  });
});

/* ---------------------------------------------------------------------------
 * Q-12 — the read is capped and says so
 * ------------------------------------------------------------------------- */

describe("Ads — Q-12 count disclosure", () => {
  it("a full page (50 rows) says the read is capped, above the rows", async () => {
    wire({
      campaigns: Array.from({ length: LIMIT }, (_, index) => campaign(LIMIT - index)),
    });

    render(<AdsPage />);

    const marker = await screen.findByTestId("ads-truncation");
    expect(marker).toHaveTextContent(
      `Showing the first ${LIMIT} — the API caps this read at ${LIMIT} per request.`,
    );
    expect(screen.getByTestId("ads-count")).toHaveTextContent(`${LIMIT} campaigns`);
  });

  it("a short list renders NO truncation marker (nothing was cut off)", async () => {
    wire({ campaigns: [campaign(1)] });

    render(<AdsPage />);

    await screen.findByTestId("ads-count");
    expect(screen.queryByTestId("ads-truncation")).toBeNull();
  });
});

/* ---------------------------------------------------------------------------
 * The honest empty (Q-26)
 * ------------------------------------------------------------------------- */

describe("Ads — the empty state exists ONLY on a settled read that returned nothing", () => {
  it("an empty successful read names the producer (Q-26), with a measured count of 0", async () => {
    wire({ campaigns: [] });

    render(<AdsPage />);

    expect(await screen.findByText(/No campaigns yet\./)).toBeInTheDocument();
    expect(bodyText()).toContain("Agents page");
    expect(screen.getByTestId("ads-count")).toHaveTextContent("0 campaigns");
    expect(rows()).toHaveLength(0);
    expect(screen.queryByTestId("ads-truncation")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("an empty FILTERED read says the filter matched nothing — not 'no campaigns yet'", async () => {
    wire({
      campaigns: [campaign(1)],
      byPlatform: [],
    });

    render(<AdsPage />);
    await screen.findByTestId("campaign-row");

    await userEvent.setup().click(screen.getByTestId("ads-platform-meta"));

    expect(await screen.findByText(/No campaigns match/)).toBeInTheDocument();
    expect(screen.queryByText(/No campaigns yet\./)).toBeNull();
    expect(rows()).toHaveLength(0);
  });
});

/* ---------------------------------------------------------------------------
 * Filters — every control proven against the REAL query
 * ------------------------------------------------------------------------- */

describe("Ads — filters re-issue the real query params (each control proven)", () => {
  const twoPlatforms = [campaign(2, { name: "meta run" }), campaign(1, { name: "google run", platform: "google" })];

  it("platform chips come from observed values; clicking one re-issues the read with platform=<value>", async () => {
    wire({ campaigns: twoPlatforms, byPlatform: [campaign(2, { name: "meta run" })] });

    render(<AdsPage />);
    await screen.findByTestId("ads-count");
    expect(rows()).toHaveLength(2);

    const chip = screen.getByTestId("ads-platform-meta");
    await userEvent.setup().click(chip);

    await waitFor(() => {
      expect(lastPath()).toContain("platform=meta");
    });
    expect(lastPath()).toContain(`limit=${LIMIT}`);
    // The filtered body is what rendered.
    await waitFor(() => {
      expect(rows()).toHaveLength(1);
    });
    expect(screen.getByTestId("campaign-row")).toHaveTextContent("meta run");
    expect(screen.getByTestId("ads-platform-meta")).toHaveAttribute("aria-pressed", "true");
  });

  it("the status select re-issues the read with status=<value>", async () => {
    wire({
      campaigns: twoPlatforms,
      byStatus: [campaign(3, { name: "paused run", status: "paused" })],
    });

    render(<AdsPage />);
    await screen.findByTestId("ads-count");

    await userEvent.setup().selectOptions(screen.getByTestId("ads-status-select"), "paused");

    await waitFor(() => {
      expect(lastPath()).toContain("status=paused");
    });
    await waitFor(() => {
      expect(rows()).toHaveLength(1);
    });
    expect(screen.getByTestId("ads-status-select")).toHaveValue("paused");
  });

  it("clearing a filter OMITS the param entirely and re-reads the full list", async () => {
    wire({ campaigns: twoPlatforms, byPlatform: [campaign(2, { name: "meta run" })] });

    render(<AdsPage />);
    await screen.findByTestId("ads-count");

    const user = userEvent.setup();
    await user.click(screen.getByTestId("ads-platform-meta"));
    await waitFor(() => expect(lastPath()).toContain("platform=meta"));

    await user.click(screen.getByTestId("ads-platform-all"));
    await waitFor(() => expect(lastPath()).toBe(`/ads/campaigns?limit=${LIMIT}`));
    expect(lastPath()).not.toContain("platform=");
    await waitFor(() => expect(rows()).toHaveLength(2));
  });

  it("Refresh re-issues the SAME path, filters included", async () => {
    wire({ campaigns: twoPlatforms, byPlatform: [campaign(2, { name: "meta run" })] });

    render(<AdsPage />);
    await screen.findByTestId("ads-count");

    const user = userEvent.setup();
    await user.click(screen.getByTestId("ads-platform-meta"));
    await waitFor(() => expect(lastPath()).toContain("platform=meta"));

    await user.click(screen.getByTestId("ads-refresh"));

    await waitFor(() => {
      const filteredCalls = mockApiGet.mock.calls.filter((call) =>
        String(call[0]).includes("platform=meta"),
      );
      expect(filteredCalls.length).toBeGreaterThanOrEqual(2);
    });
    expect(lastPath()).toContain("platform=meta");
    expect(lastPath()).toContain(`limit=${LIMIT}`);
  });

  it("the page carries no control that does not issue a real read (no decorative buttons)", async () => {
    wire({ campaigns: twoPlatforms });

    render(<AdsPage />);
    await screen.findByTestId("ads-count");

    // The COMPLETE button inventory: Refresh + the platform filter chips (observed values only) +
    // one real metrics expander per campaign row. Platform chip -> `platform=…`, status select ->
    // `status=…`, Refresh -> the same path; the expander -> `/ads/campaigns/{id}/metrics` only when
    // opened (proven by `v22-wiring-ads-metrics.test.tsx`). Anything else would be a control with
    // no real query behind it.
    const labels = screen.getAllByRole("button").map((button) => button.textContent);
    expect(labels).toEqual(["Refresh", "All platforms", "meta", "google", "Metrics", "Metrics"]);
  });
});
