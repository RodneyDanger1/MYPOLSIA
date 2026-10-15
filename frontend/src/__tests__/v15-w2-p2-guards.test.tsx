/**
 * WAVE V15 / seat W2 — the two PEEK-merge pins (UI-PEEK §P2-01, §P2-03), RED-first.
 *
 * P2-01: `WebsiteCard` rendered `config.website_url` VERBATIM as an anchor href
 * (`WebsiteCard.tsx:144-153` at b17d7b0, value sourced at `:130`) — the same U2-04-class sink the
 * V14 wave closed for planner claims, but here the value is owner-typed/free-form config. React 18
 * does not block `javascript:` URLs, so a hostile value would execute on click in the app origin
 * (which holds the owner's API key). The V14 receipt's "stale cite" dismissal of this file was
 * wrong; P1 counter-verified the sink live. Fix = the same `safeHref()` discipline as U2-04:
 * anchor ONLY for absolute http(s); every other value renders as plain text.
 *
 * P2-03: the documents overlay's counter printed `list.total ?? 0` — a fabricated 0 whenever the
 * server sent no `total`, e.g. "1 OF 0". Fix = render the counter only when the server actually
 * sent the number; an absent total prints nothing.
 *
 * RED leg (raw in wave-v15/w2/raw/): pre-fix, the javascript:/data: pins find a live hostile
 * anchor and the total-less pin finds the fabricated "1 OF 0" — both fail before the fix.
 */
import { render, screen, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

jest.mock("next/link", () => ({
  __esModule: true,
  default: function MockLink(props: { href: string; children: React.ReactNode } & Record<string, unknown>) {
    const { href, children, ...rest } = props;
    return (
      <a href={href} {...(rest as Record<string, string>)}>
        {children}
      </a>
    );
  },
}));

import { api } from "@/lib/api";
import { WebsiteCard } from "@/components/dashboard/WebsiteCard";
import { DocumentsOverlay } from "@/components/dashboard/DocumentsOverlay";

const mockGet = api.get as jest.Mock;

function answerCardWith(websiteUrl: unknown) {
  mockGet.mockImplementation((path: string) => {
    if (path === "/config") return Promise.resolve({ website_url: websiteUrl });
    if (path === "/health") return Promise.resolve({ status: "ok" });
    return Promise.reject(new Error(`unexpected read: ${path}`));
  });
}

beforeEach(() => {
  mockGet.mockReset();
});

describe("P2-01 — WebsiteCard never renders a javascript:/data: href", () => {
  it.each([
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    " javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///C:/Windows/System32/calc.exe",
    "not-a-url",
  ])("renders %s as plain text — no anchor, raw value still shown", async (hostile) => {
    answerCardWith(hostile);
    const { container } = render(<WebsiteCard />);

    // The stored value is still reported verbatim — hiding it would be its own lie. (Normalized
    // the way Testing Library normalizes DOM text: a leading-space value trims.)
    expect(await screen.findByText(hostile.trim())).toBeInTheDocument();
    // No new-tab anchor, and no anchor whose href carries the hostile scheme.
    expect(container.querySelectorAll('a[target="_blank"]')).toHaveLength(0);
    expect(container.querySelectorAll("a")).toHaveLength(1); // only the EDIT link remains
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
    expect(container.querySelector('a[href^="data:"]')).toBeNull();
    expect(container.querySelector('a[href^="vbscript:"]')).toBeNull();
  });

  it("keeps a real http(s) site link working exactly as before (target, rel, exact href)", async () => {
    answerCardWith("https://northline.example");
    render(<WebsiteCard />);

    const siteLink = await screen.findByRole("link", { name: "https://northline.example" });
    expect(siteLink).toHaveAttribute("href", "https://northline.example");
    expect(siteLink).toHaveAttribute("target", "_blank");
    expect(siteLink).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("a null website_url is still the honest no-site line", async () => {
    answerCardWith(null);
    render(<WebsiteCard />);

    expect(await screen.findByText("No website configured — set one in Settings.")).toBeInTheDocument();
  });
});

describe("P2-03 — DocumentsOverlay never prints a fabricated 0", () => {
  const row = {
    id: 1,
    report_date: "2026-10-14",
    morning_plan: null,
    evening_summary: null,
    tasks_planned: 2,
    tasks_completed: 1,
    tasks_failed: 0,
    updated_at: null,
  };

  it("a read WITHOUT a total renders NO counter — never '1 OF 0'", async () => {
    mockGet.mockResolvedValue({ items: [row] });
    render(<DocumentsOverlay open onClose={() => {}} />);

    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("2026-10-14");

    // The counter line is simply absent: nothing real to state, nothing stated.
    expect(within(dialog).queryByText(/\d+ OF \d+/)).toBeNull();
    expect(within(dialog).queryByText(/OF 0/)).toBeNull();
    // The rows themselves still render — the read answered.
    expect(within(dialog).getByText("2026-10-14")).toBeInTheDocument();
  });

  it("a server-sent total still renders the counter (regression)", async () => {
    mockGet.mockResolvedValue({ items: [row, { ...row, id: 2, report_date: "2026-10-13" }], total: 6 });
    render(<DocumentsOverlay open onClose={() => {}} />);

    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("2 OF 6")).toBeInTheDocument();
  });

  it("a total beyond the read still SAYS the read was capped (regression)", async () => {
    mockGet.mockResolvedValue({ items: [row], total: 120 });
    render(<DocumentsOverlay open onClose={() => {}} />);

    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("1 OF 120 — showing the newest 1")).toBeInTheDocument();
  });
});
