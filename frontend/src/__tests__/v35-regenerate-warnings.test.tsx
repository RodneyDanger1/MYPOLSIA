/**
 * WAVE 7 / R-W6-A3 — `POST /sites/{slug}/regenerate` now serves `publishing_warnings`, and the
 * screen consumes it.
 *
 * WHY: a regenerate re-renders and re-publishes every page from the CURRENT company profile, so it
 * can introduce exactly what the create path warns about (a banned claim, a private value). The
 * wave-6 receipt recorded the decision as open; wave 7 adds the ADDITIVE response field
 * (`SiteRegeneratedResponse.publishing_warnings`, `site_service.regenerate_site`) and this suite
 * pins the consumer — because an additive field nobody renders is the A-W4-02 defect class this
 * repo keeps paying for.
 *
 * WHAT THIS PINS
 *  - a rebuild whose body carries warnings renders them with the server's own code/message/fields/
 *    fix, in the amber advisory idiom (it is not a failure: the rebuild already happened);
 *  - a response WITHOUT the key (a pre-wave-7 server, or a clean profile) renders NOTHING — absence
 *    is "declared nothing", never a clean bill invented client-side;
 *  - the success notice is unchanged, and a REFUSED regenerate renders the named failure with no
 *    warnings block (a refusal is not a publication);
 *  - a later clean regenerate CLEARS the previous block (no stale warnings describing pages that
 *    were just rewritten clean).
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), delete: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import SitesPage from "@/app/sites/page";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

const SITE = {
  slug: "landing-page",
  title: "Landing page",
  url: "/sites/landing-page/",
  created_at: "2026-10-19T12:00:00+00:00",
  bytes: 7643,
  pages: ["index.html", "about.html", "contact.html"],
  template: "modern",
  custom_domain: null,
};

const REGENERATED = {
  slug: SITE.slug,
  url: SITE.url,
  bytes: 7100,
  title: SITE.title,
  created_at: SITE.created_at,
  pages: SITE.pages,
  template: "modern",
  custom_domain: null,
};

const BANNED = {
  code: "W_BANNED_COPY_CLAIM",
  message:
    'the generated contact page publishes "Small-batch candles." — the claim term small_batch is not allowed on a public page',
  fields: ["value_prop"],
  how_to_fix: "replace the claim with the supplier-supported line (BRAND-KIT §8) or remove it before publishing",
};

function serve(opts: { regenerate: unknown }) {
  mockGet.mockImplementation((path: string) => {
    if (path === "/sites") return Promise.resolve([SITE]);
    // each row's deploy-state read answers a real (unconfigured) state
    return Promise.resolve({
      slug: SITE.slug,
      configured: false,
      missing: ["RENDER_API_KEY"],
      service_id: null,
      last_deploy: null,
    });
  });
  mockPost.mockResolvedValue(opts.regenerate);
}

async function clickRegenerate() {
  render(<SitesPage />);
  await screen.findByTestId("sites-list");
  fireEvent.click(screen.getByTestId("site-regenerate-landing-page"));
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

describe("W7 — regenerate renders the census warnings the response carries", () => {
  it("renders the code, message, fields and fix in an advisory block", async () => {
    serve({ regenerate: { ...REGENERATED, publishing_warnings: [BANNED] } });
    await clickRegenerate();

    const block = await screen.findByTestId("sites-regenerate-warnings");
    expect(block.textContent).toMatch(/landing-page/);
    const warning = within(block).getByTestId("sites-regenerate-warning-W_BANNED_COPY_CLAIM");
    expect(warning.textContent).toMatch(/PUBLISHING WARNING · W_BANNED_COPY_CLAIM/);
    expect(warning.textContent).toMatch(/small_batch/);
    expect(warning.textContent).toMatch(/published fields: value_prop/);
    expect(warning.textContent).toMatch(/BRAND-KIT §8/);
    // The rebuild SUCCEEDED — the notice is still there beside the warning (advisory, not a gate).
    expect(await screen.findByTestId("sites-lifecycle-notice")).toHaveTextContent(/regenerated/i);
  });

  it("renders nothing when the response carries no key (a pre-wave-7 server or a clean profile)", async () => {
    serve({ regenerate: { ...REGENERATED } });
    await clickRegenerate();

    expect(await screen.findByTestId("sites-lifecycle-notice")).toHaveTextContent(/regenerated/i);
    expect(screen.queryByTestId("sites-regenerate-warnings")).toBeNull();
  });

  it("renders nothing for an EMPTY list (`[]` is a measured clean answer)", async () => {
    serve({ regenerate: { ...REGENERATED, publishing_warnings: [] } });
    await clickRegenerate();

    await screen.findByTestId("sites-lifecycle-notice");
    expect(screen.queryByTestId("sites-regenerate-warnings")).toBeNull();
  });

  it("a refused regenerate names the failure and shows no warnings (a refusal is not a publication)", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/sites") return Promise.resolve([SITE]);
      return Promise.resolve({
        slug: SITE.slug,
        configured: false,
        missing: [],
        service_id: null,
        last_deploy: null,
      });
    });
    mockPost.mockRejectedValue(
      new ApiError(404, "no site with slug 'landing-page'", "E_NOT_FOUND", "check the slug"),
    );
    await clickRegenerate();

    await waitFor(() => expect(screen.queryByTestId("sites-regenerate-warnings")).toBeNull());
    expect(screen.getByTestId("site-row-landing-page")).toBeInTheDocument();
  });

  it("a later clean regenerate clears the previous warnings (no stale advisory)", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/sites") return Promise.resolve([SITE]);
      return Promise.resolve({
        slug: SITE.slug,
        configured: false,
        missing: [],
        service_id: null,
        last_deploy: null,
      });
    });
    mockPost.mockResolvedValueOnce({ ...REGENERATED, publishing_warnings: [BANNED] });
    mockPost.mockResolvedValueOnce({ ...REGENERATED, publishing_warnings: [] });

    render(<SitesPage />);
    await screen.findByTestId("sites-list");

    fireEvent.click(screen.getByTestId("site-regenerate-landing-page"));
    expect(await screen.findByTestId("sites-regenerate-warnings")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("site-regenerate-landing-page"));
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByTestId("sites-regenerate-warnings")).toBeNull());
  });
});
