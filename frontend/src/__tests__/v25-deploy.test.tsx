/**
 * WAVE V25 / lane L3 — the Deploy control on the `/sites` page (Render adapter).
 *
 * RED leg: written BEFORE the deploy-state reads, the `SiteDeployControl` component and the page
 * wiring existed, so this suite fails on missing behaviour — the honest RED state for a new
 * surface.  Raw output: `wave-v25/l3-deploy/raw/red-frontend.txt`.
 *
 * The wire shapes are the real ones (`backend/app/api/v1/sites.py`,
 * `backend/app/services/deploy_service.py`):
 *   GET  /sites/{slug}/deploy -> {slug, configured, missing[], service_id, last_deploy|null}
 *   POST /sites/{slug}/deploy {dry_run} -> {slug, dry_run, configured, state, status, deploy_id,
 *                                         url, at, message, plan{method,url,headers,body}}
 *
 * Pinned here, all against the operator's real path through the UI:
 *  - a configured site shows its honest status badge ("Never deployed") and a working Deploy
 *    button; a site whose deploy state could not be read says so instead of claiming "never";
 *  - Deploy runs the DRY RUN first (`{dry_run: true}`) and shows the exact planned call — method,
 *    URL, body — with the credential redacted; nothing is called live until Confirm;
 *  - Confirm posts `{dry_run: false}` and the badge reports the Render deploy id, not a bare
 *    "deployed" the server did not prove;
 *  - the control is disabled + aria-busy while a call is in flight;
 *  - with `configured: false` there is NO dead button — the row names exactly what is missing;
 *  - a failed deploy shows its stored reason; a refused call shows the server's code + fix hint.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { SiteList } from "@/components/sites/SiteList";
import type { DeployResponse, DeployStateResponse, SiteSummary } from "@/components/sites/types";
import SitesPage from "@/app/sites/page";

jest.setTimeout(15_000);

const mockGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;
const mockPost = api.post as jest.MockedFunction<
  (path: string, body?: unknown) => Promise<unknown>
>;

const SITE: SiteSummary = {
  slug: "landing-page",
  title: "Landing page",
  url: "/sites/landing-page/",
  created_at: "2026-10-19T12:00:00+00:00",
  bytes: 4321,
};

const STATE: DeployStateResponse = {
  slug: "landing-page",
  configured: true,
  missing: [],
  service_id: "srv-v25test",
  last_deploy: null,
};

const DRY: DeployResponse = {
  slug: "landing-page",
  dry_run: true,
  configured: true,
  state: "dry_run",
  status: null,
  deploy_id: null,
  url: null,
  at: null,
  message: "dry run: the deploy call below was NOT sent",
  plan: {
    method: "POST",
    url: "https://api.render.com/v1/services/srv-v25test/deploys",
    headers: {
      authorization: "Bearer …",
      accept: "application/json",
      "content-type": "application/json",
    },
    body: { clearCache: "do_not_clear" },
  },
};

const LIVE: DeployResponse = {
  slug: "landing-page",
  dry_run: false,
  configured: true,
  state: "submitted",
  status: "created",
  deploy_id: "dep-abc123",
  url: "https://shop.onrender.com",
  at: "2026-10-19T15:00:00+00:00",
  message: "Render accepted the deploy: dep-abc123 (status created)",
  plan: DRY.plan,
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

/** Route the two real GETs: the list, and the per-site deploy state. */
function routeReads(list: unknown, state: unknown = STATE) {
  mockGet.mockImplementation((path: string) =>
    path.endsWith("/deploy") ? Promise.resolve(state) : Promise.resolve(list),
  );
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

async function renderPage(list: unknown = [SITE], state: unknown = STATE) {
  routeReads(list, state);
  render(<SitesPage />);
  await screen.findByTestId("sites-list");
  const row = screen.getByTestId("site-row-landing-page");
  // The row's deploy state is its own request; wait for it to settle so the assertions below are
  // about the settled surface (and so a click cannot race the state read).
  await waitFor(() =>
    expect(within(row).getByTestId("site-deploy-badge-landing-page")).not.toHaveTextContent(
      /reading deploy state/i,
    ),
  );
  return row;
}

// ── 1. badges ────────────────────────────────────────────────────────────────────────────────
describe("/sites — deploy status badges", () => {
  it("says 'Never deployed' for a configured site with no deploy record", async () => {
    const row = await renderPage();
    // The deploy-state read is its own request: the badge may say "Reading…" for a tick, and is
    // never read as "never deployed" before it settles.
    await waitFor(() =>
      expect(within(row).getByTestId("site-deploy-badge-landing-page")).toHaveTextContent(
        /never deployed/i,
      ),
    );
  });

  it("names a failed deploy with the server's stored reason", async () => {
    const failed: DeployStateResponse = {
      ...STATE,
      last_deploy: {
        at: "2026-10-19T15:00:00+00:00",
        state: "failed",
        status: null,
        deploy_id: null,
        url: null,
        error: { code: "E_UPSTREAM", message: "HTTP 503 from POST .../deploys: render is down" },
      },
    };
    const row = await renderPage([SITE], failed);
    await waitFor(() =>
      expect(within(row).getByTestId("site-deploy-badge-landing-page")).toHaveTextContent(
        /failed/i,
      ),
    );
    const badge = within(row).getByTestId("site-deploy-badge-landing-page");
    expect(badge).toHaveTextContent("E_UPSTREAM");
    expect(badge).toHaveTextContent("503");
  });

  it("names an unreadable deploy state instead of claiming 'never deployed'", async () => {
    const row = await renderPage([SITE], { items: [] });
    await waitFor(() =>
      expect(within(row).getByTestId("site-deploy-badge-landing-page")).toHaveTextContent(
        /state unread/i,
      ),
    );
    const badge = within(row).getByTestId("site-deploy-badge-landing-page");
    expect(badge).not.toHaveTextContent(/never deployed/i);
  });
});

// ── 2. dry run first ────────────────────────────────────────────────────────────────────────
describe("/sites — the two-step deploy", () => {
  it("previews the exact planned call and sends nothing live", async () => {
    const user = userEvent.setup();
    const row = await renderPage();
    mockPost.mockResolvedValue(DRY);

    await user.click(within(row).getByTestId("site-deploy-button-landing-page"));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost).toHaveBeenCalledWith("/sites/landing-page/deploy", { dry_run: true });

    const preview = await within(row).findByTestId("site-deploy-preview-landing-page");
    expect(preview).toHaveTextContent("POST");
    expect(preview).toHaveTextContent("https://api.render.com/v1/services/srv-v25test/deploys");
    expect(preview).toHaveTextContent("clearCache");
    expect(preview).toHaveTextContent(/bearer …/i); // the credential is redacted
    expect(preview).toHaveTextContent(/not sent/i);
    expect(within(row).getByTestId("site-deploy-badge-landing-page")).toHaveTextContent(
      /dry-run ok/i,
    );
    // Only the dry run has happened.
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it("sends the live deploy only on Confirm and reports the Render deploy id", async () => {
    const user = userEvent.setup();
    const row = await renderPage();
    mockPost.mockResolvedValueOnce(DRY).mockResolvedValueOnce(LIVE);

    await user.click(within(row).getByTestId("site-deploy-button-landing-page"));
    await within(row).findByTestId("site-deploy-preview-landing-page");
    await user.click(within(row).getByRole("button", { name: /confirm deploy landing page/i }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
    expect(mockPost).toHaveBeenLastCalledWith("/sites/landing-page/deploy", { dry_run: false });

    const badge = within(row).getByTestId("site-deploy-badge-landing-page");
    expect(badge).toHaveTextContent(/submitted/i);
    expect(badge).toHaveTextContent("dep-abc123");
    expect(badge).toHaveTextContent(/created/i);
  });

  it("disables the control and sets aria-busy while a call is in flight", async () => {
    const user = userEvent.setup();
    const row = await renderPage();
    const pending = deferred<unknown>();
    mockPost.mockReturnValue(pending.promise);

    const button = within(row).getByTestId("site-deploy-button-landing-page");
    await user.click(button);

    await waitFor(() => expect(button).toBeDisabled());
    expect(button).toHaveAttribute("aria-busy", "true");

    await act(async () => {
      pending.resolve(DRY);
    });
    await within(row).findByTestId("site-deploy-preview-landing-page");
    expect(within(row).getByTestId("site-deploy-button-landing-page")).not.toBeDisabled();
  });
});

// ── 3. refusals ─────────────────────────────────────────────────────────────────────────────
describe("/sites — deploy refusals", () => {
  it("shows the server's named refusal and keeps the row's previous badge", async () => {
    const user = userEvent.setup();
    const row = await renderPage();
    mockPost.mockRejectedValue(
      new ApiError(503, "no RENDER_API_KEY is configured", "E_CONFIG", "set RENDER_API_KEY"),
    );

    await user.click(within(row).getByTestId("site-deploy-button-landing-page"));

    const error = await within(row).findByTestId("site-deploy-error-landing-page");
    expect(error).toHaveTextContent("E_CONFIG");
    expect(error).toHaveTextContent("set RENDER_API_KEY");
    expect(within(row).queryByTestId("site-deploy-preview-landing-page")).not.toBeInTheDocument();
    expect(within(row).getByTestId("site-deploy-badge-landing-page")).toHaveTextContent(
      /never deployed/i,
    );
  });

  it("renders NO deploy button when Render is not configured — and names what is missing", async () => {
    const unconfigured: DeployStateResponse = {
      ...STATE,
      configured: false,
      missing: ["RENDER_API_KEY", "RENDER_SERVICE_ID"],
    };
    const row = await renderPage([SITE], unconfigured);

    expect(within(row).queryByTestId("site-deploy-button-landing-page")).not.toBeInTheDocument();
    const badge = within(row).getByTestId("site-deploy-badge-landing-page");
    expect(badge).toHaveTextContent(/not configured/i);
    expect(badge).toHaveTextContent("RENDER_API_KEY");
    expect(badge).toHaveTextContent("RENDER_SERVICE_ID");
  });
});

// ── 4. the component contract in isolation ──────────────────────────────────────────────────
describe("SiteDeployControl — the row's deploy surface", () => {
  it("keeps the other row controls intact and adds no dead button", async () => {
    routeReads([SITE]);
    const onDryRun = jest.fn();
    render(
      <SiteList
        rows={[SITE]}
        selectedSlug={null}
        onSelect={jest.fn()}
        onCopy={jest.fn()}
        deploy={{
          states: { "landing-page": STATE },
          stateErrors: {},
          busySlug: null,
          busyKind: null,
          previews: {},
          errors: {},
          onDryRun,
          onDeploy: jest.fn(),
          onCancel: jest.fn(),
        }}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("site-deploy-button-landing-page")).toBeEnabled(),
    );
    expect(screen.getByRole("link", { name: /open landing page/i })).toBeInTheDocument();
  });
});
