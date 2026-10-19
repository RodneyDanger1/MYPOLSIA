/**
 * WAVE V24 / lane L3 — the `/sites` builder page + the nav entry.
 *
 * RED leg (raw output in `wave-v24/l3-sites/raw/red-frontend.txt`): written BEFORE
 * `src/app/sites/page.tsx` existed, so this suite failed on the missing module — the honest RED
 * state for a new surface.
 *
 * What is pinned, all against the real wire shapes (`POST/GET /api/v1/sites`,
 * `backend/app/api/v1/sites.py`):
 *  - the three disjoint read states: aria-busy loading, a NAMED failure that replaces the rows,
 *    and the honest empty only on a settled read that returned nothing;
 *  - every row's real fields (title, slug, created stamp) plus a working Open link to
 *    `/sites/<slug>/` and a Copy URL control that writes the ABSOLUTE url;
 *  - the sandboxed iframe preview: it renders the selected site, never auto-selects, and carries a
 *    sandbox attribute (with scripts/forms/top-navigation NOT granted);
 *  - the create form sends exactly `{title, tagline}` to `POST /sites` and then re-reads the list
 *    (the server's meta.json is the one source for created_at/bytes — no client-side invention);
 *  - a refused create renders the server's own code + message in place, keeping the typed values.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { NAV_ITEMS } from "@/components/layout/nav-items";
import SitesPage from "@/app/sites/page";

// jsdom + userEvent typing is slow, and this suite runs beside sibling lanes on a shared machine:
// the DEFAULT 5s per-test budget measured flaky timeouts (raw/green-frontend.txt), not a hang.
jest.setTimeout(15_000);

const mockGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;
const mockPost = api.post as jest.MockedFunction<
  (path: string, body?: unknown) => Promise<unknown>
>;

const SITE = {
  slug: "landing-page",
  title: "Landing page",
  url: "/sites/landing-page/",
  created_at: "2026-10-19T12:00:00+00:00",
  bytes: 4321,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

// ── 1. read states ────────────────────────────────────────────────────────────────────────────
describe("/sites — the read states", () => {
  it("shows an aria-busy loading state, not an empty claim, while the read is in flight", async () => {
    const pending = deferred<unknown>();
    mockGet.mockReturnValue(pending.promise);
    render(<SitesPage />);

    const loading = screen.getByTestId("sites-loading");
    expect(loading).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByTestId("sites-empty")).not.toBeInTheDocument();

    pending.resolve([]);
    await screen.findByTestId("sites-empty");
    expect(screen.queryByTestId("sites-loading")).not.toBeInTheDocument();
  });

  it("renders the server's rows with title, slug, created stamp and a real Open link", async () => {
    mockGet.mockResolvedValue([SITE]);
    render(<SitesPage />);

    const list = await screen.findByTestId("sites-list");
    expect(within(list).getByText("Landing page")).toBeInTheDocument();
    expect(within(list).getByText("/sites/landing-page/")).toBeInTheDocument();
    expect(within(list).getByText(/2026/)).toBeInTheDocument();
    expect(within(list).getByText(/4,321 bytes/)).toBeInTheDocument();

    const open = within(list).getByRole("link", { name: /open landing page/i });
    expect(open).toHaveAttribute("href", "/sites/landing-page/");
    expect(within(list).getByRole("button", { name: /copy url for landing page/i })).toBeInTheDocument();
    // Nothing is previewed until the operator picks a row.
    expect(screen.queryByTestId("site-preview")).not.toBeInTheDocument();
  });

  it("states the honest empty only when the read succeeded with no rows", async () => {
    mockGet.mockResolvedValue([]);
    render(<SitesPage />);

    const empty = await screen.findByTestId("sites-empty");
    expect(empty).toHaveTextContent(/no sites yet/i);
    expect(screen.queryByTestId("sites-error")).not.toBeInTheDocument();
  });

  it("names a failed read and does NOT draw the empty state under it", async () => {
    mockGet.mockRejectedValue(new ApiError(503, "database unavailable", "E_DB", "check DATABASE_URL"));
    render(<SitesPage />);

    const error = await screen.findByTestId("sites-error");
    expect(error).toHaveTextContent("E_DB");
    expect(error).toHaveTextContent("database unavailable");
    expect(screen.queryByTestId("sites-empty")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sites-list")).not.toBeInTheDocument();
  });

  it("treats a non-list body as a broken read, never as an empty list", async () => {
    mockGet.mockResolvedValue({ items: [] });
    render(<SitesPage />);

    const error = await screen.findByTestId("sites-error");
    expect(error).toHaveTextContent(/no list of sites/i);
    expect(screen.queryByTestId("sites-empty")).not.toBeInTheDocument();
  });
});

// ── 2. create ────────────────────────────────────────────────────────────────────────────────
describe("/sites — the create form", () => {
  it("posts exactly {title, tagline}, then re-reads the server's list", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValueOnce([]).mockResolvedValueOnce([SITE]);
    mockPost.mockResolvedValue({ slug: SITE.slug, url: SITE.url, bytes: SITE.bytes });
    render(<SitesPage />);
    await screen.findByTestId("sites-empty");

    await user.type(screen.getByLabelText("Title"), "Landing page");
    await user.type(screen.getByLabelText("Tagline"), "A clear line");
    await user.click(screen.getByRole("button", { name: /create site/i }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost).toHaveBeenCalledWith("/sites", {
      title: "Landing page",
      tagline: "A clear line",
    });
    // The list is re-read from the disk-backed API — the created row's created_at/bytes come from
    // the server, not from the create response.  Wave V25 also reads each row's deploy state
    // (`/sites/<slug>/deploy`), so the assertion counts LIST reads by path, not every GET.
    await waitFor(() =>
      expect(mockGet.mock.calls.filter(([path]) => path === "/sites")).toHaveLength(2),
    );
    const refreshed = await screen.findByTestId("sites-list");
    expect(within(refreshed).getByText("Landing page")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("/sites");
  });

  it("sends only the fields the operator typed (blank boxes are omitted, not invented)", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue([]);
    mockPost.mockResolvedValue({ slug: SITE.slug, url: SITE.url, bytes: SITE.bytes });
    render(<SitesPage />);
    await screen.findByTestId("sites-empty");

    await user.type(screen.getByLabelText("Title"), "Only a title");
    await user.click(screen.getByRole("button", { name: /create site/i }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost).toHaveBeenCalledWith("/sites", { title: "Only a title" });
  });

  it("renders the server's refusal in place and keeps what was typed", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue([]);
    mockPost.mockRejectedValue(
      new ApiError(422, "site slug 'landing-page' already exists", "E_VALIDATION", "pick a different slug"),
    );
    render(<SitesPage />);
    await screen.findByTestId("sites-empty");

    await user.type(screen.getByLabelText("Title"), "Landing page");
    await user.click(screen.getByRole("button", { name: /create site/i }));

    const error = await screen.findByTestId("sites-create-error");
    expect(error).toHaveTextContent("E_VALIDATION");
    expect(error).toHaveTextContent("already exists");
    expect(error).toHaveTextContent("pick a different slug");
    // The typed value survives the refusal.
    expect(screen.getByLabelText("Title")).toHaveValue("Landing page");
  });
});

// ── 3. preview + copy ───────────────────────────────────────────────────────────────────────
describe("/sites — preview and copy", () => {
  it("previews the selected site in a sandboxed iframe that grants no scripts", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue([SITE]);
    render(<SitesPage />);
    const list = await screen.findByTestId("sites-list");

    await user.click(within(list).getByRole("button", { name: /preview landing page/i }));

    const frame = screen.getByTestId("site-preview");
    expect(frame.tagName).toBe("IFRAME");
    expect(frame).toHaveAttribute("src", "/sites/landing-page/");
    expect(frame).toHaveAttribute("title", "Preview of Landing page");
    const sandbox = frame.getAttribute("sandbox");
    expect(sandbox).not.toBeNull();
    expect(sandbox).not.toContain("allow-scripts");
    expect(sandbox).not.toContain("allow-top-navigation");
    expect(sandbox).not.toContain("allow-forms");
    // The full-page Open link sits beside the preview, not inside an invented modal.
    expect(screen.getByRole("link", { name: /open landing page in a new tab/i })).toHaveAttribute(
      "href",
      "/sites/landing-page/",
    );
  });

  it("copies the ABSOLUTE url and says so only after the write resolved", async () => {
    const user = userEvent.setup();
    const writeText = jest.fn().mockResolvedValue(undefined);
    // jsdom exposes `navigator.clipboard` as a getter-only property, so define it directly.
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    mockGet.mockResolvedValue([SITE]);
    render(<SitesPage />);
    const list = await screen.findByTestId("sites-list");

    await user.click(within(list).getByRole("button", { name: /copy url for landing page/i }));

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText.mock.calls[0][0]).toMatch(/\/sites\/landing-page\/$/);
    expect(await screen.findByTestId("sites-copied")).toHaveTextContent(/copied/i);
  });
});

// ── 4. the nav entry ────────────────────────────────────────────────────────────────────────
describe("nav table — the Sites route", () => {
  it("carries exactly one /sites item, labelled Sites, with a lucide icon", () => {
    const items = NAV_ITEMS.filter((item) => item.href === "/sites");
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe("Sites");
    expect(items[0].icon).toBeDefined();
  });

  it("sits in the publishing cluster, after Ads and before Finance", () => {
    const hrefs = NAV_ITEMS.map((item) => item.href);
    expect(hrefs.indexOf("/sites")).toBe(hrefs.indexOf("/ads") + 1);
    expect(hrefs[hrefs.indexOf("/sites") + 1]).toBe("/finance");
  });
});
