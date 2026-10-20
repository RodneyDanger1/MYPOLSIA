/**
 * WAVE V29 / lane L6 — the `/sites` health badge.
 *
 * RED leg (raw output in `polsia-work/build/ui-reverse/wave-v29/l6-trust/red/T6.3-badge.txt`):
 * written BEFORE `src/components/sites/SiteHealthBadge.tsx` existed, so this suite failed on the
 * missing module — the honest RED state for a new surface.
 *
 * What is pinned:
 *  - a LOCAL row renders `LOCAL` and makes NO network call (the badge is for a real custom domain
 *    only; the plan is explicit that local sites never hit the trust API);
 *  - a row with a `custom_domain` asks `GET /trust/domain?host=…` exactly ONCE and shows the
 *    certificate's real remaining days (`TLS <n>d`), with the expiry in `title`;
 *  - `Status: 3` (NXDOMAIN) is `DNS FAIL`, a failed TLS leg is `CHECK FAILED`, and a refused /
 *    malformed read is `CHECK FAILED` too — never a fabricated `TLS 0d`;
 *  - no badge at all for an empty host (nothing to check, nothing claimed).
 *
 * The badge paints `CHECKING…` before the read lands, so every claim below is AWAITED — asserting
 * on the first paint would race the promise and prove nothing.
 */
import { render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { SiteHealthBadge } from "@/components/sites/SiteHealthBadge";
import { SiteList } from "@/components/sites/SiteList";
import type { SiteSummary } from "@/components/sites/types";

jest.setTimeout(15_000);

const mockGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;

const LOCAL: SiteSummary = {
  slug: "local-site",
  title: "Local site",
  url: "/sites/local-site/",
  created_at: "2026-10-19T12:00:00+00:00",
  bytes: 4321,
};

const CUSTOM: SiteSummary = {
  slug: "custom-site",
  title: "Custom site",
  url: "/sites/custom-site/",
  created_at: "2026-10-19T12:00:00+00:00",
  bytes: 4321,
  custom_domain: "example.com",
};

function health(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    host: "example.com",
    dns: { a: ["93.184.216.34"], aaaa: [], status: 0, reason: null },
    tls: {
      ok: true,
      not_after: "2026-12-25T23:59:59+00:00",
      days_left: 66,
      reason: null,
    },
    reason: null,
    checked_at: "2026-10-20T12:00:00+00:00",
    cached: false,
    ...overrides,
  };
}

/** Wait for the badge to SETTLE on `text` (the first paint is the pending state). */
async function settledBadge(text: string | RegExp, scope?: HTMLElement): Promise<HTMLElement> {
  const query = scope ? within(scope) : screen;
  const badge = await query.findByTestId("site-health-badge");
  await waitFor(() => expect(badge).toHaveTextContent(text));
  return badge;
}

function renderList(rows: SiteSummary[]) {
  return render(
    <SiteList rows={rows} selectedSlug={null} onSelect={jest.fn()} onCopy={jest.fn()} />,
  );
}

beforeEach(() => {
  mockGet.mockReset();
});

// ── 1. the badge in isolation ────────────────────────────────────────────────────────────────
describe("SiteHealthBadge — one domain, three honest states", () => {
  it("renders nothing and asks nothing when the host is empty", () => {
    const { container } = render(<SiteHealthBadge host={null} />);

    expect(container).toBeEmptyDOMElement();
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("shows the certificate's remaining days and carries the expiry in the title", async () => {
    mockGet.mockResolvedValue(health());
    render(<SiteHealthBadge host="example.com" />);

    const badge = await settledBadge("TLS 66d");
    expect(badge.getAttribute("title")).toContain("2026-12-25");
    expect(badge).toHaveAttribute("data-state", "tls");
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith("/trust/domain?host=example.com");
  });

  it("says DNS FAIL for a Status 3 (NXDOMAIN) answer — never a TLS claim", async () => {
    mockGet.mockResolvedValue(
      health({
        dns: { a: [], aaaa: [], status: 3, reason: "NXDOMAIN" },
        tls: { ok: false, not_after: null, days_left: null, reason: "no address to connect to" },
        reason: "example.com does not resolve (NXDOMAIN)",
      }),
    );
    render(<SiteHealthBadge host="gone.example.com" />);

    const badge = await settledBadge("DNS FAIL");
    expect(badge).not.toHaveTextContent(/TLS/);
    expect(badge.getAttribute("title")).toContain("NXDOMAIN");
    expect(badge).toHaveAttribute("data-state", "dns");
  });

  it("says CHECK FAILED when the certificate leg failed", async () => {
    mockGet.mockResolvedValue(
      health({
        tls: {
          ok: false,
          not_after: null,
          days_left: null,
          reason: "certificate verification failed: certificate has expired",
        },
        reason: "certificate verification failed: certificate has expired",
      }),
    );
    render(<SiteHealthBadge host="expired.example.com" />);

    const badge = await settledBadge("CHECK FAILED");
    expect(badge.getAttribute("title")).toContain("certificate has expired");
    expect(badge).toHaveAttribute("data-state", "failed");
  });

  it("names a refused read instead of inventing a healthy badge", async () => {
    mockGet.mockRejectedValue(
      new ApiError(502, "the DNS resolver answered HTTP 503", "E_UPSTREAM", "retry shortly"),
    );
    render(<SiteHealthBadge host="example.com" />);

    const badge = await settledBadge("CHECK FAILED");
    expect(badge.getAttribute("title")).toContain("E_UPSTREAM");
    expect(badge.getAttribute("title")).toContain("HTTP 503");
  });

  it("treats a body that is not a health result as a broken read", async () => {
    mockGet.mockResolvedValue({ archived: true });
    render(<SiteHealthBadge host="example.com" />);

    const badge = await settledBadge("CHECK FAILED");
    expect(badge.getAttribute("title")).toContain("no health result");
  });
});

// ── 2. the list mount ───────────────────────────────────────────────────────────────────────
describe("SiteList — local rows never call the trust API", () => {
  it("renders LOCAL for a site with no custom domain and makes no request", async () => {
    mockGet.mockResolvedValue(health());
    renderList([LOCAL]);

    const row = await screen.findByTestId("site-row-local-site");
    expect(within(row).getByText("LOCAL")).toBeInTheDocument();
    expect(mockGet).not.toHaveBeenCalled();
    expect(within(row).queryByTestId("site-health-badge")).not.toBeInTheDocument();
  });

  it("mounts one badge for the custom-domain row and checks exactly that host", async () => {
    mockGet.mockResolvedValue(health());
    renderList([LOCAL, CUSTOM]);

    const custom = await screen.findByTestId("site-row-custom-site");
    const local = screen.getByTestId("site-row-local-site");

    const badge = await settledBadge("TLS 66d", custom);
    expect(within(local).getByText("LOCAL")).toBeInTheDocument();
    expect(within(local).queryByTestId("site-health-badge")).not.toBeInTheDocument();

    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith("/trust/domain?host=example.com");
    expect(badge.getAttribute("title")).toContain("2026-12-25");
  });

  it("url-encodes the host and never sends a scheme or a path", async () => {
    mockGet.mockResolvedValue(health({ host: "example.com" }));
    renderList([{ ...CUSTOM, custom_domain: "Example.COM" }]);

    await settledBadge("TLS 66d");

    expect(mockGet).toHaveBeenCalledWith("/trust/domain?host=Example.COM");
    expect(mockGet.mock.calls[0][0]).not.toContain("://");
    expect(mockGet.mock.calls[0][0]).not.toContain("/sites/");
  });
});
