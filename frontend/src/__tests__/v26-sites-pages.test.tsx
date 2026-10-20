/**
 * WAVE V26 / lane L5 — Sites 2.0 on the `/sites` page.
 *
 * RED leg (raw output in `wave-v26/l5-sites/raw/red-frontend.txt`): written BEFORE the template
 * picker, the page list and the custom-domain field existed, so it failed on those queries — the
 * honest RED state for a new surface.
 *
 * What is pinned, against the real wire shapes (`POST/GET /api/v1/sites`):
 *  - the picker offers exactly the three named presets and sends `template` when one is chosen
 *    (the default, classic, is omitted so the server's own default applies);
 *  - the page list renders a real link per generated page (`/sites/<slug>/`, `about.html`,
 *    `contact.html`) plus the sitemap, and a V24 row (no `pages`) shows only its index page —
 *    no link is fabricated for a page that site does not have;
 *  - the custom-domain field is labelled "recorded, not verified", an invalid hostname
 *    (`http://x/y`) renders the inline validation message and NEVER posts, and a valid bare
 *    hostname travels in the create body.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn() } };
});

import { api } from "@/lib/api";
import SitesPage from "@/app/sites/page";

jest.setTimeout(15_000);

const mockGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;
const mockPost = api.post as jest.MockedFunction<
  (path: string, body?: unknown) => Promise<unknown>
>;

const V26_SITE = {
  slug: "landing-page",
  title: "Landing page",
  url: "/sites/landing-page/",
  created_at: "2026-10-19T12:00:00+00:00",
  bytes: 7643,
  pages: ["index.html", "about.html", "contact.html"],
  template: "modern",
  custom_domain: "shop.example.com",
};

const LEGACY_SITE = {
  slug: "old-site",
  title: "Old site",
  url: "/sites/old-site/",
  created_at: "2026-01-01T00:00:00+00:00",
  bytes: 1200,
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

// ── 1. the template picker ───────────────────────────────────────────────────────────────────
describe("/sites — the template picker", () => {
  it("offers exactly the three named presets", async () => {
    mockGet.mockResolvedValue([]);
    render(<SitesPage />);
    await screen.findByTestId("sites-empty");

    for (const label of ["Classic", "Modern", "Bold"]) {
      expect(screen.getByRole("radio", { name: label })).toBeInTheDocument();
    }
    expect(screen.getAllByRole("radio")).toHaveLength(3);
    // Classic is the preset the form starts on (the server's default).
    expect(screen.getByRole("radio", { name: "Classic" })).toBeChecked();
  });

  it("sends the chosen template and leaves the default to the server", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValueOnce([]).mockResolvedValueOnce([V26_SITE]);
    mockPost.mockResolvedValue({
      slug: V26_SITE.slug,
      url: V26_SITE.url,
      bytes: V26_SITE.bytes,
    });
    render(<SitesPage />);
    await screen.findByTestId("sites-empty");

    await user.type(screen.getByLabelText("Title"), "Landing page");
    await user.click(screen.getByRole("radio", { name: "Modern" }));
    await user.click(screen.getByRole("button", { name: /create site/i }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost).toHaveBeenCalledWith("/sites", {
      title: "Landing page",
      template: "modern",
    });
  });
});

// ── 2. the page list ────────────────────────────────────────────────────────────────────────
describe("/sites — the page list", () => {
  it("links each generated page and the sitemap for a V26 site", async () => {
    mockGet.mockResolvedValue([V26_SITE]);
    render(<SitesPage />);
    const list = await screen.findByTestId("sites-list");
    const pages = within(list).getByTestId("site-pages-landing-page");

    expect(within(pages).getByRole("link", { name: "index.html" })).toHaveAttribute(
      "href",
      "/sites/landing-page/",
    );
    expect(within(pages).getByRole("link", { name: "about.html" })).toHaveAttribute(
      "href",
      "/sites/landing-page/about.html",
    );
    expect(within(pages).getByRole("link", { name: "contact.html" })).toHaveAttribute(
      "href",
      "/sites/landing-page/contact.html",
    );
    expect(within(pages).getByRole("link", { name: "sitemap.xml" })).toHaveAttribute(
      "href",
      "/sites/landing-page/sitemap.xml",
    );
    // The recorded domain is shown with the honest wording, never as "verified".
    expect(within(list).getByText(/shop\.example\.com/)).toBeInTheDocument();
    expect(within(list).getByText(/recorded, not verified/i)).toBeInTheDocument();
  });

  it("shows only the index page for a V24 row, with no fabricated links", async () => {
    mockGet.mockResolvedValue([LEGACY_SITE]);
    render(<SitesPage />);
    const list = await screen.findByTestId("sites-list");
    const pages = within(list).getByTestId("site-pages-old-site");

    expect(within(pages).getAllByRole("link")).toHaveLength(1);
    expect(within(pages).getByRole("link", { name: "index.html" })).toHaveAttribute(
      "href",
      "/sites/old-site/",
    );
    expect(within(pages).queryByRole("link", { name: "sitemap.xml" })).not.toBeInTheDocument();
  });
});

// ── 3. the custom-domain field ──────────────────────────────────────────────────────────────
describe("/sites — the custom domain", () => {
  it("labels the field as recorded, not verified", async () => {
    mockGet.mockResolvedValue([]);
    render(<SitesPage />);
    await screen.findByTestId("sites-empty");

    expect(screen.getByLabelText(/custom domain/i)).toBeInTheDocument();
    expect(screen.getByText(/recorded, not verified/i)).toBeInTheDocument();
  });

  it("refuses a scheme/path value inline and never posts it", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue([]);
    render(<SitesPage />);
    await screen.findByTestId("sites-empty");

    await user.type(screen.getByLabelText("Title"), "Bad domain");
    await user.type(screen.getByLabelText(/custom domain/i), "http://x/y");
    await user.click(screen.getByRole("button", { name: /create site/i }));

    const error = await screen.findByTestId("sites-domain-error");
    expect(error).toHaveTextContent(/bare hostname/i);
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("sends a valid bare hostname in the create body", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValueOnce([]).mockResolvedValueOnce([V26_SITE]);
    mockPost.mockResolvedValue({
      slug: V26_SITE.slug,
      url: V26_SITE.url,
      bytes: V26_SITE.bytes,
    });
    render(<SitesPage />);
    await screen.findByTestId("sites-empty");

    await user.type(screen.getByLabelText("Title"), "Landing page");
    await user.type(screen.getByLabelText(/custom domain/i), "shop.example.com");
    await user.click(screen.getByRole("button", { name: /create site/i }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost).toHaveBeenCalledWith("/sites", {
      title: "Landing page",
      custom_domain: "shop.example.com",
    });
    expect(screen.queryByTestId("sites-domain-error")).not.toBeInTheDocument();
  });
});
