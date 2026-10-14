import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * Wave V8b / seat l4 — the rail Website card (`WebsiteCard`), suite `u8b-website`.
 *
 * The card makes exactly TWO reads, both mocked at the module boundary (`@/lib/api`): the config
 * read (`/config` — the settings page's own read) and the ONE mount-time health read (`/health`).
 * The REAL `ApiError` class is kept via `jest.requireActual`, so the non-ok leg exercises the api
 * client's actual error shape — the server's status carried on the thrown error, not a string
 * convention invented here.
 *
 * `next/navigation` is mocked with a `push` spy because the chips' "no navigation" claim must be
 * falsifiable: the `next/link` mock routes clicks to that same spy, so a chip that ever became a
 * Link (or called the router) would light it up.
 */
const mockPush = jest.fn();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

jest.mock("next/link", () => ({
  __esModule: true,
  default: function MockLink(
    props: { href: string; children: React.ReactNode } & Record<string, unknown>,
  ) {
    const { href, children, ...rest } = props;
    return (
      <a
        href={href}
        {...(rest as Record<string, string>)}
        onClick={(event) => {
          event.preventDefault();
          mockPush(href);
        }}
      >
        {children}
      </a>
    );
  },
}));

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { WebsiteCard } from "@/components/dashboard/WebsiteCard";
import { ApiError, api } from "@/lib/api";

const mockGet = api.get as jest.Mock;

const SITE_URL = "https://northline.example";
const NO_SITE_LINE = "No website configured — set one in Settings.";

/** Both reads this card makes, answered per path; an unlisted path rejects LOUDLY so a widget
 *  that starts reading something new cannot pass this suite silently. An `Error` value becomes a
 *  rejection (the api client rejects a non-ok answer with an `ApiError`); any other value resolves
 *  (a never-settling Promise value stays pending, which is how the pre-resolution state is built). */
function answerWith(overrides: Record<string, unknown> = {}) {
  const paths: Record<string, unknown> = {
    "/config": { website_url: SITE_URL },
    "/health": { status: "ok" },
    ...overrides,
  };
  mockGet.mockImplementation((path: string) => {
    const key = Object.keys(paths).find((candidate) => path.startsWith(candidate));
    if (key === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    const value = paths[key];
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

beforeEach(() => {
  mockGet.mockReset();
  mockPush.mockReset();
});

describe("WebsiteCard — row 1, the configured site", () => {
  it("renders the configured URL as an external link with the exact href, target and rel, plus an EDIT link to /settings", async () => {
    answerWith();
    render(<WebsiteCard />);

    const siteLink = await screen.findByRole("link", { name: SITE_URL });
    expect(siteLink).toHaveAttribute("href", SITE_URL);
    expect(siteLink).toHaveAttribute("target", "_blank");
    expect(siteLink).toHaveAttribute("rel", "noopener noreferrer");

    const edit = screen.getByRole("link", { name: "EDIT" });
    expect(edit).toHaveAttribute("href", "/settings");
  });

  it("states 'No website configured' with the EDIT link when website_url is null — and never invents a default domain", async () => {
    answerWith({ "/config": { website_url: null } });
    const { container } = render(<WebsiteCard />);

    expect(await screen.findByText(NO_SITE_LINE)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "EDIT" })).toHaveAttribute("href", "/settings");
    // No URL link element at all — a fabricated default domain would appear here.
    expect(container.querySelectorAll('a[target="_blank"]')).toHaveLength(0);
  });

  it("treats an empty string like no configuration", async () => {
    answerWith({ "/config": { website_url: "" } });
    render(<WebsiteCard />);

    expect(await screen.findByText(NO_SITE_LINE)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /https?:\/\// })).toBeNull();
  });

  it("names a failed config read instead of inventing a site (the states.tsx error idiom)", async () => {
    answerWith({ "/config": new Error("E_HTTP_503: config unavailable") });
    render(<WebsiteCard />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the website configuration: E_HTTP_503: config unavailable",
    );
    expect(screen.queryByText(NO_SITE_LINE)).toBeNull();
  });
});

describe("WebsiteCard — row 2, the measured health line", () => {
  it("reports a successful read as 'App healthy (200 OK, N ms) — checked TIME' with the client-measured integer ms", async () => {
    answerWith();
    render(<WebsiteCard />);

    expect(
      await screen.findByText(/^App healthy \(200 OK, \d+ ms\) — checked \d{1,2}:\d{2} [AP]M$/),
    ).toBeInTheDocument();
  });

  it("reports a non-ok answer with its real status — and says 'healthy' NOWHERE", async () => {
    answerWith({ "/health": new ApiError(503, "service unavailable", "E_HTTP_503") });
    render(<WebsiteCard />);

    expect(
      await screen.findByText(/^Health check answered 503 — checked \d{1,2}:\d{2} [AP]M$/),
    ).toBeInTheDocument();
    expect((document.body.textContent ?? "").toLowerCase()).not.toContain("healthy");
  });

  it("renders a failed read in the alert presentation, naming the real message", async () => {
    answerWith({ "/health": new Error("E_NETWORK: fetch failed") });
    render(<WebsiteCard />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      /^App health check failed — E_NETWORK: fetch failed — checked \d{1,2}:\d{2} [AP]M$/,
    );
    // The same presentation PanelError uses (states.tsx): the red alert treatment.
    expect(alert).toHaveClass("text-alert");
  });

  it("shows the dash placeholder before the read resolves — synchronously, and with no fake number", () => {
    mockGet.mockImplementation(() => new Promise(() => {}));
    render(<WebsiteCard />);

    // Asserted on the SYNCHRONOUS first render — nothing resolved yet.
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByText(/App healthy/)).toBeNull();
    expect(screen.queryByText(/Health check/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("WebsiteCard — the management chips refuse by name", () => {
  it("MANAGE DOMAIN then VERSIONS reveal their named refusals in ONE shared note; nothing is called and nothing navigates", async () => {
    answerWith();
    const user = userEvent.setup();
    render(<WebsiteCard />);

    // Both reads settled — the card is stable before the chips are exercised.
    await screen.findByText(/^App healthy/);
    const readsBefore = mockGet.mock.calls.length;

    const domain = screen.getByRole("button", { name: "MANAGE DOMAIN" });
    const versions = screen.getByRole("button", { name: "VERSIONS" });
    // Real buttons, not links — a link here would make "no navigation" false by construction.
    expect(screen.queryByRole("link", { name: "MANAGE DOMAIN" })).toBeNull();
    expect(screen.queryByRole("link", { name: "VERSIONS" })).toBeNull();
    expect(domain).toHaveAttribute("aria-expanded", "false");
    expect(versions).toHaveAttribute("aria-expanded", "false");

    await user.click(domain);
    const note = screen.getByRole("note");
    expect(note.textContent).toBe(
      "Refused by name — this deployment ships no domain-management backend; nothing was called.",
    );
    // Capability absence, not a fault: the neutral note voice, never the red alert treatment.
    expect(note).not.toHaveClass("text-alert");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(domain).toHaveAttribute("aria-expanded", "true");
    expect(versions).toHaveAttribute("aria-expanded", "false");
    // Keyboard-discoverable: the revealed refusal takes focus.
    expect(note).toHaveFocus();

    await user.click(versions);
    expect(screen.getByRole("note").textContent).toBe(
      "Refused by name — this deployment ships no site-versioning backend; nothing was called.",
    );
    expect(domain).toHaveAttribute("aria-expanded", "false");
    expect(versions).toHaveAttribute("aria-expanded", "true");
    // ONE shared slot: the first refusal is replaced, not stacked.
    expect(screen.getAllByRole("note")).toHaveLength(1);
    expect(screen.queryByText(/no domain-management backend/)).toBeNull();

    // ZERO network from chip clicks, and nothing navigated (the push spy never fired).
    expect(mockGet.mock.calls.length).toBe(readsBefore);
    expect(mockPush).not.toHaveBeenCalled();
  });
});
