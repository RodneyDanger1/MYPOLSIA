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
 *   POST /approvals {action_fingerprint} -> {token, action_fingerprint, expires_at, ttl_s}
 *   POST /sites/{slug}/deploy {dry_run:false, approval_token} -> the submitted deploy
 *
 * UIB-02: a LIVE Render deploy is `EXTERNAL_IRREVERSIBLE` in `policy_firewall`, so the backend
 * refuses it with `E_POLICY_DENIED` / `require_approval` until a single-use token for the exact
 * fingerprint is supplied — and that refusal carries the fingerprint the SERVER computed.  The
 * tests below pin the two-step the page now performs (Confirm -> refusal -> mint -> repeat with the
 * token), the named refusals approval cannot resolve (sandbox / no approval channel / a mint that
 * answers no token or another fingerprint), and that the minted token never reaches the document.
 *
 * Pinned here, all against the operator's real path through the UI:
 *  - a configured site shows its honest status badge ("Never deployed") and a working Deploy
 *    button; a site whose deploy state could not be read says so instead of claiming "never";
 *  - Deploy runs the DRY RUN first (`{dry_run: true}`) and shows the exact planned call — method,
 *    URL, body — with the credential redacted; nothing is called live until Confirm;
 *  - Confirm posts `{dry_run: false}` and the badge reports the Render deploy id, not a bare
 *    "deployed" the server did not prove;
 *  - a `require_approval` refusal arms "Approve & deploy" with the server's fingerprint, and that
 *    control mints for exactly that fingerprint and REPEATS the live deploy with the returned
 *    token — the token itself never appears in the rendered document;
 *  - a sandbox block / a missing approval channel / a bad mint body render as named refusals with
 *    NO approval control (never a dead button promising a path that does not exist);
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

/** The fingerprint `deploy_service.deploy_payload` + `policy_firewall.fingerprint` yield for this
 *  site — the SERVER computes it and puts it in the refusal; the browser only reads it. */
const DEPLOY_FINGERPRINT = "9f2c4ab1d7e35608c1a4b9ff";

/** A bearer credential the test mints; it must NEVER appear in the rendered document. */
const MINTED_TOKEN = "1760000000.0123456789abcdef0123456789abcdef";

const MINT = {
  token: MINTED_TOKEN,
  action_fingerprint: DEPLOY_FINGERPRINT,
  expires_at: 1_760_000_000,
  ttl_s: 3600,
};

/** The real `E_POLICY_DENIED` / `require_approval` refusal from
 *  `deploy_service._authorise_live_deploy`, envelope and all (`detail` = HTTPException shape). */
function approvalRefusal(fingerprint = DEPLOY_FINGERPRINT): ApiError {
  return new ApiError(
    403,
    "deploy_site: the live Render deploy of a site is refused by policy: irreversible action",
    "E_POLICY_DENIED",
    `mint a single-use approval token for fingerprint ${fingerprint} (POST /api/v1/approvals) ` +
      'and repeat this deploy with {"approval_token": "<token>"}',
    {
      detail: {
        code: "E_POLICY_DENIED",
        message:
          "deploy_site: the live Render deploy of a site is refused by policy: irreversible action",
        how_to_fix: `mint a single-use approval token for fingerprint ${fingerprint}`,
        context: {
          decision: "require_approval",
          policy_code: "MERIDIAN-E115",
          fingerprint,
          channel: "render",
        },
      },
    },
  );
}

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

// ── 3b. the approval leg of a LIVE deploy (UIB-02) ──────────────────────────────────────────
describe("/sites — the approval leg of a LIVE deploy", () => {
  /** Drive the real page to the state the backend produces for every live deploy: the dry run,
   *  then Confirm refused with `require_approval` and the server's fingerprint. */
  async function toApprovalGate() {
    const user = userEvent.setup();
    const row = await renderPage();
    mockPost.mockResolvedValueOnce(DRY).mockRejectedValueOnce(approvalRefusal());
    await user.click(within(row).getByTestId("site-deploy-button-landing-page"));
    await within(row).findByTestId("site-deploy-preview-landing-page");
    await user.click(within(row).getByRole("button", { name: /confirm deploy landing page/i }));
    const gate = await within(row).findByTestId("site-deploy-approval-landing-page");
    return { user, row, gate };
  }

  it("asks the server first: Confirm sends no token and the refusal arms the gate with the server's fingerprint", async () => {
    const { row, gate } = await toApprovalGate();

    expect(mockPost).toHaveBeenNthCalledWith(1, "/sites/landing-page/deploy", { dry_run: true });
    expect(mockPost).toHaveBeenNthCalledWith(2, "/sites/landing-page/deploy", { dry_run: false });
    // The first live attempt carries NO approval token — nothing is minted before the server names
    // the fingerprint, and no build could have been queued (the refusal is the policy gate).
    expect(JSON.stringify(mockPost.mock.calls[1]?.[1] ?? {})).not.toContain("approval_token");

    expect(gate).toHaveTextContent("E_POLICY_DENIED");
    expect(gate).toHaveTextContent("nothing was sent");
    expect(
      within(row).getByTestId("site-deploy-fingerprint-landing-page"),
    ).toHaveTextContent(DEPLOY_FINGERPRINT);
    // A refusal approval can resolve is NOT painted as an error: it is the approval step.
    expect(within(row).queryByTestId("site-deploy-error-landing-page")).not.toBeInTheDocument();
  });

  it("mints for exactly that fingerprint and repeats the deploy with the token — never rendering it", async () => {
    const { user, row, gate } = await toApprovalGate();
    expect(gate).toBeInTheDocument();

    mockPost.mockResolvedValueOnce(MINT).mockResolvedValueOnce(LIVE);
    await user.click(within(row).getByTestId("site-deploy-approve-landing-page"));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(4));
    expect(mockPost).toHaveBeenNthCalledWith(3, "/approvals", {
      action_fingerprint: DEPLOY_FINGERPRINT,
    });
    expect(mockPost).toHaveBeenNthCalledWith(4, "/sites/landing-page/deploy", {
      dry_run: false,
      approval_token: MINTED_TOKEN,
    });

    // The bearer credential is never rendered — not as text, not in any attribute.
    expect(document.body.innerHTML).not.toContain(MINTED_TOKEN);
    expect(document.body.textContent ?? "").not.toContain(MINTED_TOKEN);

    // The gate closed and the badge reports the server's own deploy id (never a bare "deployed").
    expect(within(row).queryByTestId("site-deploy-approval-landing-page")).not.toBeInTheDocument();
    const badge = within(row).getByTestId("site-deploy-badge-landing-page");
    expect(badge).toHaveTextContent(/submitted/i);
    expect(badge).toHaveTextContent("dep-abc123");
    const notice = within(row).getByTestId("site-deploy-notice-landing-page");
    expect(notice).toHaveTextContent(DEPLOY_FINGERPRINT);
    expect(notice).toHaveTextContent(/never displayed/i);
    expect(notice).not.toHaveTextContent(MINTED_TOKEN);
  });

  it("renders E_SANDBOX_BLOCKED as the named refusal — no approval control, nothing claimed sent", async () => {
    const user = userEvent.setup();
    const row = await renderPage();
    const sandbox = new ApiError(
      403,
      "deploy_site: the live Render deploy of a site is refused by policy: SANDBOX_MODE=true: " +
        "external action recorded as a labelled draft, nothing sent",
      "E_SANDBOX_BLOCKED",
      "run with SANDBOX_MODE=false to send real actions",
      {
        detail: {
          code: "E_SANDBOX_BLOCKED",
          message:
            "deploy_site: the live Render deploy of a site is refused by policy: SANDBOX_MODE=true: " +
            "external action recorded as a labelled draft, nothing sent",
          how_to_fix: "run with SANDBOX_MODE=false to send real actions",
          context: {
            decision: "deny",
            policy_code: "MERIDIAN-E114",
            sandbox: true,
            fingerprint: DEPLOY_FINGERPRINT,
          },
        },
      },
    );
    mockPost.mockResolvedValueOnce(DRY).mockRejectedValueOnce(sandbox);

    await user.click(within(row).getByTestId("site-deploy-button-landing-page"));
    await within(row).findByTestId("site-deploy-preview-landing-page");
    await user.click(within(row).getByRole("button", { name: /confirm deploy landing page/i }));

    const error = await within(row).findByTestId("site-deploy-error-landing-page");
    expect(error).toHaveTextContent("E_SANDBOX_BLOCKED");
    expect(error).toHaveTextContent(/nothing sent/i);
    // Approval cannot lift a sandbox block: no mint control may be offered.
    expect(within(row).queryByTestId("site-deploy-approve-landing-page")).not.toBeInTheDocument();
    expect(within(row).queryByTestId("site-deploy-approval-landing-page")).not.toBeInTheDocument();
  });

  it("renders the 'no approval channel' refusal with no mint control (mint could never verify)", async () => {
    const user = userEvent.setup();
    const row = await renderPage();
    const noChannel = new ApiError(
      403,
      "deploy_site: the live Render deploy of a site is refused by policy: approval required but " +
        "no approval channel configured",
      "E_POLICY_DENIED",
      "set APPROVAL_SECRET and restart the service",
      {
        detail: {
          code: "E_POLICY_DENIED",
          message:
            "deploy_site: the live Render deploy of a site is refused by policy: approval required " +
            "but no approval channel configured",
          how_to_fix: "set APPROVAL_SECRET and restart the service",
          context: { decision: "deny", policy_code: "MERIDIAN-E114" },
        },
      },
    );
    mockPost.mockResolvedValueOnce(DRY).mockRejectedValueOnce(noChannel);

    await user.click(within(row).getByTestId("site-deploy-button-landing-page"));
    await within(row).findByTestId("site-deploy-preview-landing-page");
    await user.click(within(row).getByRole("button", { name: /confirm deploy landing page/i }));

    const error = await within(row).findByTestId("site-deploy-error-landing-page");
    expect(error).toHaveTextContent("E_POLICY_DENIED");
    expect(error).toHaveTextContent(/no approval channel configured/i);
    expect(error).toHaveTextContent("APPROVAL_SECRET");
    expect(within(row).queryByTestId("site-deploy-approve-landing-page")).not.toBeInTheDocument();
  });

  it("mint refused by name (APPROVAL_SECRET unset) is rendered and the deploy is NOT repeated", async () => {
    const { user, row } = await toApprovalGate();
    mockPost.mockRejectedValueOnce(
      new ApiError(
        503,
        "the approvals channel is not configured: APPROVAL_SECRET is unset, so no token could " +
          "ever verify",
        "E_CONFIG",
        "set APPROVAL_SECRET in .env and restart the service",
      ),
    );

    await user.click(within(row).getByTestId("site-deploy-approve-landing-page"));

    const error = await within(row).findByTestId("site-deploy-error-landing-page");
    expect(error).toHaveTextContent("E_CONFIG");
    expect(error).toHaveTextContent("APPROVAL_SECRET");
    // Only the three calls so far: dry, refused live, refused mint. No token-bearing deploy.
    expect(mockPost).toHaveBeenCalledTimes(3);
    expect(JSON.stringify(mockPost.mock.calls.map((call) => call[1] ?? {}))).not.toContain(
      "approval_token",
    );
  });

  it("a mint for another fingerprint is a named broken answer — the deploy is not sent on it", async () => {
    const { user, row } = await toApprovalGate();
    mockPost.mockResolvedValueOnce({ ...MINT, action_fingerprint: "some-other-fingerprint-000" });

    await user.click(within(row).getByTestId("site-deploy-approve-landing-page"));

    const error = await within(row).findByTestId("site-deploy-error-landing-page");
    expect(error).toHaveTextContent(/instead of the fingerprint/i);
    expect(error).toHaveTextContent("nothing was sent to Render");
    expect(mockPost).toHaveBeenCalledTimes(3);
    expect(JSON.stringify(mockPost.mock.calls.map((call) => call[1] ?? {}))).not.toContain(
      MINTED_TOKEN,
    );
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
          gates: {},
          notices: {},
          errors: {},
          onDryRun,
          onDeploy: jest.fn(),
          onApprove: jest.fn(),
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
