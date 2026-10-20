/**
 * V29 follow-up C — the sites row's domain verdict.
 *
 * Proven defect: the row branches on the RAW `custom_domain` (`site.custom_domain ? … : LOCAL`),
 * while the badge it mounts branches on the TRIMMED host (`host.trim()`). A whitespace-only domain —
 * a string the sites list really serves, because `site_service._summary_row` keeps any non-empty
 * string and only the create/regenerate validators reject whitespace (`meta.json` is documented as
 * human-editable) — is truthy for the row and empty for the badge, so the row renders NEITHER the
 * live verdict NOR the honest `LOCAL` chip: a domain row with no verdict at all, and no request made
 * that could ever settle it. The one place the two branches may disagree is the value both of them
 * read, so the row must normalise once and branch on what the badge will actually use.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { render, screen, within } from "@testing-library/react";

import { SiteList } from "@/components/sites/SiteList";
import type { SiteSummary } from "@/components/sites/types";
import { api } from "@/lib/api";

const mockGet = api.get as jest.Mock;

function row(overrides: Partial<SiteSummary> = {}): SiteSummary {
  return {
    slug: "site-one",
    title: "Site one",
    url: "/sites/site-one/",
    created_at: "2026-10-19T10:00:00Z",
    bytes: 2048,
    ...overrides,
  };
}

function renderList(rows: SiteSummary[]) {
  return render(
    <SiteList rows={rows} selectedSlug={null} onSelect={() => {}} onCopy={() => {}} />,
  );
}

const HEALTH = {
  host: "example.com",
  dns: { status: 0, reason: null },
  tls: { ok: true, days_left: 66, not_after: "2026-12-25T00:00:00Z", reason: null },
  reason: null,
};

beforeEach(() => {
  mockGet.mockReset();
  mockGet.mockResolvedValue(HEALTH);
});

describe("a site row's domain verdict", () => {
  it("renders LOCAL for a whitespace-only domain instead of no verdict at all", () => {
    renderList([row({ custom_domain: "   " })]);

    const siteRow = screen.getByTestId("site-row-site-one");
    expect(within(siteRow).getByTestId("site-local-site-one")).toBeInTheDocument();
    expect(within(siteRow).queryByTestId("site-health-badge")).toBeNull();
    // A blank host has nothing to check: the row must not send the trust read for it.
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("still checks a real domain exactly once (control)", async () => {
    renderList([row({ custom_domain: "example.com" })]);

    const badge = await screen.findByTestId("site-health-badge");
    expect(badge).toHaveAttribute("data-host", "example.com");
    expect(mockGet).toHaveBeenCalledWith("/trust/domain?host=example.com");
    expect(screen.queryByTestId("site-local-site-one")).toBeNull();
  });
});
