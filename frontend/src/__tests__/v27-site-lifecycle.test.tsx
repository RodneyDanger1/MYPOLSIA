/**
 * WAVE V27 / lane L1 — the owner-managed site lifecycle on the `/sites` page.
 *
 * RED leg (raw output in `wave-v27/l1-site-lifecycle/raw/red-frontend.txt`): written BEFORE the
 * two-step Delete control, the Regenerate control and the row's Sitemap link existed, so it failed
 * on those queries — the honest RED state for a new surface.
 *
 * What is pinned, against the real wire shapes (`DELETE /api/v1/sites/{slug}`,
 * `POST /api/v1/sites/{slug}/regenerate`, `GET /api/v1/sites`):
 *  - Delete is TWO-STEP: the first click arms the row and sends NOTHING; only "Confirm delete"
 *    calls the API; Cancel disarms and sends nothing;
 *  - a refused delete/regenerate renders the server's own named refusal inline, and the row stays;
 *  - Regenerate posts to `/sites/<slug>/regenerate` and the list is re-read from the server;
 *  - the row carries a Sitemap link only for a site that has a V26 sitemap (a legacy row has none).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), delete: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import SitesPage from "@/app/sites/page";

jest.setTimeout(15_000);

const mockGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;
const mockPost = api.post as jest.MockedFunction<
  (path: string, body?: unknown) => Promise<unknown>
>;
const mockDelete = api.delete as jest.MockedFunction<(path: string) => Promise<unknown>>;

const SITE = {
  slug: "landing-page",
  title: "Landing page",
  url: "/sites/landing-page/",
  created_at: "2026-10-19T12:00:00+00:00",
  bytes: 7643,
  pages: ["index.html", "about.html", "contact.html"],
  template: "modern",
  custom_domain: "shop.example.com",
};

const LEGACY = {
  slug: "old-site",
  title: "Old site",
  url: "/sites/old-site/",
  created_at: "2026-01-01T00:00:00+00:00",
  bytes: 1200,
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockDelete.mockReset();
});

// ── 1. delete is two-step ───────────────────────────────────────────────────────────────────
describe("/sites — the delete control", () => {
  it("does NOT delete on the first click; only Confirm calls the API", async () => {
    const user = userEvent.setup();
    // Path-aware read: the first `/sites` read returns the site, the post-delete read returns none,
    // and each row's deploy-state read answers a real (unconfigured) state.
    let listReads = 0;
    mockGet.mockImplementation((path: string) => {
      if (path === "/sites") {
        listReads += 1;
        return Promise.resolve(listReads === 1 ? [SITE] : []);
      }
      return Promise.resolve({
        slug: SITE.slug,
        configured: false,
        missing: ["RENDER_API_KEY"],
        service_id: null,
        last_deploy: null,
      });
    });
    mockDelete.mockResolvedValue({ slug: SITE.slug, deleted: true });
    render(<SitesPage />);
    const list = await screen.findByTestId("sites-list");

    await user.click(within(list).getByRole("button", { name: "Delete Landing page" }));

    // Armed, not sent: the row now asks for confirmation.
    expect(
      within(list).getByRole("button", { name: "Confirm delete Landing page" }),
    ).toBeInTheDocument();
    expect(mockDelete).not.toHaveBeenCalled();

    await user.click(within(list).getByRole("button", { name: "Confirm delete Landing page" }));

    await waitFor(() =>
      expect(mockDelete).toHaveBeenCalledWith("/sites/landing-page"),
    );
    // The list is re-read from the disk-backed API after the delete, and the row is gone because
    // the server no longer lists it.
    await waitFor(() =>
      expect(mockGet.mock.calls.filter(([path]) => path === "/sites")).toHaveLength(2),
    );
    await waitFor(() =>
      expect(screen.queryByTestId("site-row-landing-page")).not.toBeInTheDocument(),
    );
    expect(await screen.findByTestId("sites-empty")).toBeInTheDocument();
    expect(await screen.findByTestId("sites-lifecycle-notice")).toHaveTextContent(/deleted/i);
  });

  it("Cancel disarms the row and sends nothing", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue([SITE]);
    render(<SitesPage />);
    const list = await screen.findByTestId("sites-list");

    await user.click(within(list).getByRole("button", { name: "Delete Landing page" }));
    await user.click(within(list).getByRole("button", { name: "Cancel delete Landing page" }));

    expect(
      within(list).queryByRole("button", { name: "Confirm delete Landing page" }),
    ).not.toBeInTheDocument();
    expect(within(list).getByRole("button", { name: "Delete Landing page" })).toBeInTheDocument();
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it("renders the server's named refusal inline and keeps the row", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue([SITE]);
    mockDelete.mockRejectedValue(
      new ApiError(
        404,
        "no site exists for slug 'landing-page'",
        "E_NOT_FOUND",
        "list the sites and use a slug it returns",
      ),
    );
    render(<SitesPage />);
    const list = await screen.findByTestId("sites-list");

    await user.click(within(list).getByRole("button", { name: "Delete Landing page" }));
    await user.click(within(list).getByRole("button", { name: "Confirm delete Landing page" }));

    const error = await screen.findByTestId("site-lifecycle-error-landing-page");
    expect(error).toHaveTextContent("E_NOT_FOUND");
    expect(error).toHaveTextContent("no site exists");
    // The refusal keeps the row (and its armed confirm) visible — the site was not reported gone,
    // and the operator can retry without re-arming.
    expect(within(list).getByText("Landing page")).toBeInTheDocument();
    expect(
      within(list).getByRole("button", { name: "Confirm delete Landing page" }),
    ).toBeInTheDocument();
  });
});

// ── 2. regenerate ───────────────────────────────────────────────────────────────────────────
describe("/sites — the regenerate control", () => {
  it("posts to the site's regenerate route and re-reads the list", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue([SITE]);
    mockPost.mockResolvedValue({
      slug: SITE.slug,
      url: SITE.url,
      bytes: 8000,
      title: SITE.title,
      created_at: SITE.created_at,
      pages: SITE.pages,
      template: SITE.template,
      custom_domain: SITE.custom_domain,
    });
    render(<SitesPage />);
    const list = await screen.findByTestId("sites-list");

    await user.click(within(list).getByRole("button", { name: "Regenerate Landing page" }));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/sites/landing-page/regenerate"),
    );
    await waitFor(() =>
      expect(mockGet.mock.calls.filter(([path]) => path === "/sites")).toHaveLength(2),
    );
    expect(await screen.findByTestId("sites-lifecycle-notice")).toHaveTextContent(/regenerated/i);
  });

  it("names a failed regenerate inline", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue([SITE]);
    mockPost.mockRejectedValue(
      new ApiError(500, "could not write the site files", "E_STORAGE", "make sure data/sites is writable"),
    );
    render(<SitesPage />);
    const list = await screen.findByTestId("sites-list");

    await user.click(within(list).getByRole("button", { name: "Regenerate Landing page" }));

    const error = await screen.findByTestId("site-lifecycle-error-landing-page");
    expect(error).toHaveTextContent("E_STORAGE");
    expect(error).toHaveTextContent("writable");
  });
});

// ── 3. the sitemap link ─────────────────────────────────────────────────────────────────────
describe("/sites — the sitemap link", () => {
  it("links the served sitemap for a site that has one", async () => {
    mockGet.mockResolvedValue([SITE]);
    render(<SitesPage />);
    const list = await screen.findByTestId("sites-list");

    const sitemap = within(list).getByRole("link", { name: "Open sitemap for Landing page" });
    expect(sitemap).toHaveAttribute("href", "/sites/landing-page/sitemap.xml");
  });

  it("shows no sitemap link for a legacy site that has none", async () => {
    mockGet.mockResolvedValue([LEGACY]);
    render(<SitesPage />);
    const list = await screen.findByTestId("sites-list");

    expect(
      within(list).queryByRole("link", { name: "Open sitemap for Old site" }),
    ).not.toBeInTheDocument();
  });
});
