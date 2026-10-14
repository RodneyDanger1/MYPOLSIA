import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

jest.mock("next/navigation", () => ({
  usePathname: () => "/dashboard",
}));

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

import { ApiError, api } from "@/lib/api";
import SocialPage from "@/app/social/page";
import OutreachPage from "@/app/outreach/page";
import { Topbar } from "@/components/layout/Topbar";
import { absoluteStamp, relativeTime } from "@/components/dashboard/format";
import type { SocialPost } from "@/components/social/SocialPostCard";
import type { Prospect } from "@/components/outreach/ProspectRow";

/**
 * Wave U2 — coder F2's own suite: the Social page, the Outreach page and the topbar bell.
 *
 * Scope discipline: this file is the seat's scoped test (run with
 * `node node_modules/jest/bin/jest.js u2-social-outreach`). The pre-existing page tests
 * (`SocialPage.test.tsx`, `OutreachPage.test.tsx`, `ContractNullSafety.test.tsx`, the bell part of
 * `u1-shell.test.tsx`) were deliberately updated for the genuinely changed behaviour — the
 * old-versus-new log lives in the wave receipt.
 */

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

const NOTIF_KEY = "polsia.topbar.notifications.lastSeenAt";

function socialPost(overrides: Partial<SocialPost> & { id: number; content: string }): SocialPost {
  return {
    status: null,
    platform: null,
    tweet_id: null,
    scheduled_for: null,
    published_at: null,
    created_at: null,
    engagement: null,
    ...overrides,
  };
}

function prospect(overrides: Partial<Prospect> & { id: number; email: string }): Prospect {
  return {
    first_name: null,
    last_name: null,
    company: null,
    title: null,
    source: null,
    status: null,
    created_at: null,
    ...overrides,
  };
}

function cardOf(content: string): HTMLElement {
  const card = screen.getByText(content).closest('[data-testid="social-post"]');
  expect(card).not.toBeNull();
  return card as HTMLElement;
}

beforeEach(() => {
  mockApiGet.mockReset();
  window.localStorage.clear();
});

/* ---------------------------------------------------------------------------
 * SOCIAL — social.md acceptance 1-6
 * ------------------------------------------------------------------------- */

describe("Social page — timestamp honesty (Q-25, acceptance 1)", () => {
  it("labels published/scheduled/created and renders — for an all-null row; never an empty cell, never Invalid Date", async () => {
    const publishedIso = "2026-10-11T14:30:00Z";
    const scheduledIso = "2026-10-12T09:15:00Z";
    const createdIso = "2026-10-10T08:00:00Z";
    mockApiGet.mockResolvedValue([
      socialPost({ id: 1, content: "fixture published", status: "published", published_at: publishedIso, created_at: createdIso }),
      socialPost({ id: 2, content: "fixture scheduled", status: "draft", scheduled_for: scheduledIso, created_at: createdIso }),
      socialPost({ id: 3, content: "fixture created-only", created_at: createdIso }),
      socialPost({ id: 4, content: "fixture no-dates" }),
    ]);

    render(<SocialPage />);
    await screen.findByText("fixture no-dates");

    expect(within(cardOf("fixture published")).getByTestId("post-timestamp")).toHaveTextContent(
      `Published ${absoluteStamp(publishedIso)}`,
    );
    expect(within(cardOf("fixture scheduled")).getByTestId("post-timestamp")).toHaveTextContent(
      `Scheduled ${absoluteStamp(scheduledIso)}`,
    );
    expect(within(cardOf("fixture created-only")).getByTestId("post-timestamp")).toHaveTextContent(
      `Created ${absoluteStamp(createdIso)}`,
    );
    // All three timestamps null: the cell is NOT deleted — it carries the em-dash placeholder.
    expect(within(cardOf("fixture no-dates")).getByTestId("post-timestamp")).toHaveTextContent("—");

    for (const card of screen.getAllByTestId("social-post")) {
      expect(card.textContent ?? "").not.toMatch(/Invalid Date/);
      expect((card.querySelector('[data-testid="post-timestamp"]')?.textContent ?? "").trim()).not.toBe("");
    }
  });
});

describe("Social page — status chip lookup (Q-25, acceptance 2)", () => {
  it("published / draft / unknown-value / null each render their own treatment; unknown carries its raw word", async () => {
    mockApiGet.mockResolvedValue([
      socialPost({ id: 1, content: "fixture status published", status: "published" }),
      socialPost({ id: 2, content: "fixture status draft", status: "draft" }),
      socialPost({ id: 3, content: "fixture status scheduled", status: "scheduled" }),
      socialPost({ id: 4, content: "fixture status null", status: null }),
    ]);

    render(<SocialPage />);
    await screen.findByText("fixture status null");

    const chips = [
      within(cardOf("fixture status published")).getByTestId("post-status"),
      within(cardOf("fixture status draft")).getByTestId("post-status"),
      within(cardOf("fixture status scheduled")).getByTestId("post-status"),
      within(cardOf("fixture status null")).getByTestId("post-status"),
    ];

    expect(chips[0]).toHaveTextContent("published");
    expect(chips[0]).toHaveAttribute("data-status-tone", "published");
    expect(chips[1]).toHaveTextContent("draft");
    expect(chips[1]).toHaveAttribute("data-status-tone", "draft");
    // Unknown-to-today writer: neutral chip with the RAW word — never recast into a known status.
    expect(chips[2]).toHaveTextContent("scheduled");
    expect(chips[2]).toHaveAttribute("data-status-tone", "other");
    expect(chips[3]).toHaveTextContent("unknown");
    expect(chips[3]).toHaveAttribute("data-status-tone", "unmeasured");

    // Each treatment is distinct: four different tone markers, four different class recipes, and
    // the unknown/null chips share no colour with either named status.
    const tones = chips.map((chip) => chip.getAttribute("data-status-tone"));
    expect(new Set(tones).size).toBe(4);
    const classes = chips.map((chip) => chip.className);
    expect(new Set(classes).size).toBe(4);
    for (const chip of [chips[2], chips[3]]) expect(chip.className).toMatch(/text-ink-muted/);
    expect(chips[0].className).not.toMatch(/ink-muted/);
    expect(chips[1].className).not.toMatch(/ink-muted/);
  });
});

describe("Social page — truncation marker (Q-12, acceptance 3)", () => {
  it("renders the marker for a full 50-row read and none for a shorter one", async () => {
    const fifty = Array.from({ length: 50 }, (_, i) =>
      socialPost({ id: i + 1, content: `fixture full-page row ${i + 1}` }),
    );
    mockApiGet.mockResolvedValueOnce(fifty);
    const { unmount } = render(<SocialPage />);
    await screen.findByText("fixture full-page row 50");
    expect(screen.getByTestId("social-truncation")).toHaveTextContent(/showing the first 50/i);
    unmount();

    mockApiGet.mockResolvedValueOnce(
      Array.from({ length: 7 }, (_, i) => socialPost({ id: 100 + i, content: `fixture short row ${i + 1}` })),
    );
    render(<SocialPage />);
    await screen.findByText("fixture short row 7");
    expect(screen.queryByTestId("social-truncation")).toBeNull();
    expect(screen.queryByText(/showing the first/i)).toBeNull();
  });
});

describe("Social page — no phantom actions (GAP-NO-ENDPOINT, acceptance 4)", () => {
  it("has exactly one button (REFRESH); no TWEET/DELETE/CONNECT control; one fetch, then one more per refresh", async () => {
    const user = userEvent.setup();
    mockApiGet.mockResolvedValue([socialPost({ id: 1, content: "fixture actionable", status: "published" })]);

    render(<SocialPage />);
    await screen.findByText("fixture actionable");

    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveTextContent("Refresh");
    expect(screen.queryByRole("button", { name: /tweet|delete|connect/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /tweet|delete|connect/i })).toBeNull();
    expect(mockApiGet).toHaveBeenCalledTimes(1);
    expect(mockApiGet).toHaveBeenCalledWith("/social/posts?limit=50");

    await user.click(buttons[0]);
    expect(mockApiGet).toHaveBeenCalledTimes(2);
    // The refresh re-renders from the NEW payload (a page reload never happens in jsdom anyway —
    // what is pinned is that the SAME component instance re-read and re-painted).
    mockApiGet.mockResolvedValue([socialPost({ id: 2, content: "fixture after refresh" })]);
    await user.click(screen.getByTestId("social-refresh"));
    await screen.findByText("fixture after refresh");
    expect(mockApiGet).toHaveBeenCalledTimes(3);
  });
});

describe("Social page — engagement laundering fix (Q-22, acceptance 5)", () => {
  it("renders an object counter as the em dash (never [object Object]), strings/numbers verbatim, null engagement as the line", async () => {
    mockApiGet.mockResolvedValue([
      socialPost({
        id: 1,
        content: "fixture counters",
        engagement: { likes: 11, reposts: 2, source: "twitter", retweets: null, meta: { nested: true } },
      }),
      socialPost({ id: 2, content: "fixture no counters", engagement: null }),
    ]);

    render(<SocialPage />);
    await screen.findByText("fixture counters");

    const withCounters = cardOf("fixture counters");
    expect(withCounters).toHaveTextContent("likes: 11");
    expect(withCounters).toHaveTextContent("reposts: 2");
    expect(withCounters).toHaveTextContent("source: twitter");
    expect(withCounters).toHaveTextContent("retweets: —");
    expect(withCounters).toHaveTextContent("meta: —");
    expect(withCounters.textContent ?? "").not.toMatch(/\[object Object\]/);
    // The real payload is not hidden: it rides the counter's title attribute.
    expect(within(withCounters).getByTitle('{"nested":true}')).toBeInTheDocument();

    expect(cardOf("fixture no counters")).toHaveTextContent("engagement: —");
  });
});

describe("Social page — three states (acceptance 6)", () => {
  it("renders a skeleton with aria-busy while the read is in flight, and NO empty copy", () => {
    mockApiGet.mockReturnValue(new Promise<never>(() => {}));

    render(<SocialPage />);

    expect(screen.getByTestId("social-loading")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
    expect(screen.queryByText(/No posts yet/i)).toBeNull();
  });

  it("names the failure through ApiError.display and renders no skeleton and no empty copy", async () => {
    mockApiGet.mockRejectedValue(new ApiError(500, "backing store unreachable", "E_HTTP_500", "retry once the API is up"));

    render(<SocialPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_HTTP_500: backing store unreachable — retry once the API is up");
    expect(screen.queryByTestId("social-loading")).toBeNull();
    expect(screen.queryByText(/No posts yet/i)).toBeNull();
  });

  it("renders the producer-naming empty state only after a true empty 200", async () => {
    mockApiGet.mockResolvedValue([]);

    render(<SocialPage />);

    const empty = await screen.findByText(/No posts yet\./);
    expect(empty).toHaveTextContent(/social media agent/);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

/* ---------------------------------------------------------------------------
 * OUTREACH — outreach.md acceptance 1-6
 * ------------------------------------------------------------------------- */

describe("Outreach page — loading state exists (Q-08, acceptance 1)", () => {
  it("shows a real skeleton with aria-busy while the read is in flight — no empty copy, no list", () => {
    mockApiGet.mockReturnValue(new Promise<never>(() => {}));

    render(<OutreachPage />);

    expect(screen.getByTestId("outreach-loading")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText(/No prospects yet/i)).toBeNull();
    expect(screen.queryByTestId("outreach-list")).toBeNull();
  });

  it("names the failure through ApiError.display and renders no skeleton", async () => {
    mockApiGet.mockRejectedValue(new ApiError(401, "invalid api key", "E_AUTH", "set NEXT_PUBLIC_API_KEY"));

    render(<OutreachPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_AUTH: invalid api key — set NEXT_PUBLIC_API_KEY");
    expect(screen.queryByTestId("outreach-loading")).toBeNull();
    expect(screen.queryByText(/No prospects yet/i)).toBeNull();
  });

  it("renders the producer-naming empty copy after a true empty 200", async () => {
    mockApiGet.mockResolvedValue([]);

    render(<OutreachPage />);

    const empty = await screen.findByText(/No prospects yet\./);
    expect(empty).toHaveTextContent(/outreach agent/);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByTestId("outreach-loading")).toBeNull();
  });
});

describe("Outreach page — count honesty (Q-12, acceptance 2)", () => {
  it("marks a full 100-row read with 'showing the first 100' and a 4-row read with a plain count", async () => {
    mockApiGet.mockResolvedValueOnce(
      Array.from({ length: 100 }, (_, i) => prospect({ id: i + 1, email: `fixture-full-${i + 1}@example.test` })),
    );
    const { unmount } = render(<OutreachPage />);
    await screen.findByText("fixture-full-100@example.test");
    expect(screen.getByTestId("outreach-count")).toHaveTextContent("100 prospects");
    expect(screen.getByTestId("outreach-count")).toHaveTextContent(/showing the first 100/i);
    unmount();

    mockApiGet.mockResolvedValueOnce(
      Array.from({ length: 4 }, (_, i) => prospect({ id: 200 + i, email: `fixture-short-${i + 1}@example.test` })),
    );
    render(<OutreachPage />);
    await screen.findByText("fixture-short-4@example.test");
    expect(screen.getByTestId("outreach-count")).toHaveTextContent("4 prospects");
    expect(screen.queryByText(/showing the first/i)).toBeNull();
  });

  it("never asserts a count before the read measured one", () => {
    mockApiGet.mockReturnValue(new Promise<never>(() => {}));
    render(<OutreachPage />);
    expect(screen.queryByTestId("outreach-count")).toBeNull();
  });
});

describe("Outreach page — null-safe rows (acceptance 3)", () => {
  it("renders — / date unknown placeholders in every cell — no empty cell, no Invalid Date, no null string", async () => {
    mockApiGet.mockResolvedValue([
      prospect({ id: 1, email: "missing-details@example.test" }),
      prospect({
        id: 2,
        email: "recorded-details@example.test",
        first_name: "Fixture",
        last_name: "Row",
        company: "E2E Fixtures Co",
        title: "Head of Ops",
        status: "contacted",
        created_at: new Date(Date.now() - 3 * 86_400_000).toISOString(),
      }),
    ]);

    render(<OutreachPage />);
    await screen.findByText("missing-details@example.test");

    const nullRow = screen.getByText("missing-details@example.test").closest('[data-testid="prospect-row"]');
    expect(nullRow).not.toBeNull();
    expect(nullRow).toHaveTextContent("— · —");
    expect(within(nullRow as HTMLElement).getByTestId("prospect-age")).toHaveTextContent("date unknown");
    expect(nullRow?.textContent ?? "").not.toMatch(/Invalid Date/);
    expect(nullRow?.textContent ?? "").not.toMatch(/\bnull\b/);

    const fullRow = screen.getByText("recorded-details@example.test").closest('[data-testid="prospect-row"]');
    expect(fullRow).not.toBeNull();
    expect(fullRow).toHaveTextContent("Fixture Row · E2E Fixtures Co");
    // Job title is a real served field — it rides the row line's title attribute.
    expect(within(fullRow as HTMLElement).getByTitle("Head of Ops")).toBeInTheDocument();
    // Relative age in the live vocabulary, exact stamp on hover.
    expect((fullRow as HTMLElement).textContent ?? "").not.toMatch(/date unknown/);
    const ageNode = within(fullRow as HTMLElement).getByTestId("prospect-age");
    expect(ageNode.textContent).toMatch(/ago$/);
    expect(ageNode.getAttribute("title")).toContain("·");
  });
});

describe("Outreach page — status chips (acceptance 4)", () => {
  it("the five known values each render their own treatment; unknown is neutral with its raw word; null is the dash", async () => {
    const statuses = ["new", "contacted", "replied", "converted", "unsubscribed", "qualified", null];
    mockApiGet.mockResolvedValue(
      statuses.map((status, i) => prospect({ id: i + 1, email: `fixture-status-${i}@example.test`, status })),
    );

    render(<OutreachPage />);
    await screen.findByText(`fixture-status-${statuses.length - 1}@example.test`);

    const chips = statuses.map((_, i) => {
      const row = screen.getByText(`fixture-status-${i}@example.test`).closest('[data-testid="prospect-row"]');
      return within(row as HTMLElement).getByTestId("prospect-status");
    });

    for (let i = 0; i < 5; i += 1) {
      expect(chips[i]).toHaveTextContent(statuses[i] as string);
      expect(chips[i]).toHaveAttribute("data-status-tone", statuses[i] as string);
    }
    const namedClasses = chips.slice(0, 5).map((chip) => chip.className);
    expect(new Set(namedClasses).size).toBe(5);

    expect(chips[5]).toHaveTextContent("qualified");
    expect(chips[5]).toHaveAttribute("data-status-tone", "other");
    expect(chips[6]).toHaveTextContent("—");
    expect(chips[6]).toHaveAttribute("data-status-tone", "unmeasured");
    // The gray treatments share no colour with any named status.
    for (const chip of [chips[5], chips[6]]) expect(chip.className).toMatch(/text-ink-muted/);
    for (const chip of chips.slice(0, 5)) expect(chip.className).not.toMatch(/ink-muted/);
  });
});

describe("Outreach page — no phantom automations (GAP-NO-ENDPOINT, acceptance 5)", () => {
  it("renders no FREQUENCY / START OUTREACH / START AUTO-SUPPORT control and no SENT/CREDIT counter text", async () => {
    mockApiGet.mockResolvedValue([prospect({ id: 1, email: "fixture@example.test", status: "new" })]);

    const { container } = render(<OutreachPage />);
    await screen.findByText("fixture@example.test");

    const text = container.textContent ?? "";
    expect(text).not.toMatch(/FREQUENCY/i);
    expect(text).not.toMatch(/START OUTREACH/i);
    expect(text).not.toMatch(/START AUTO-SUPPORT/i);
    expect(text).not.toMatch(/\bSENT\b/);
    expect(text).not.toMatch(/CREDIT/i);
    expect(screen.queryByRole("button", { name: /outreach|support|start/i })).toBeNull();

    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveTextContent("Refresh");
    expect(mockApiGet).toHaveBeenCalledTimes(1);
    expect(mockApiGet).toHaveBeenCalledWith("/outreach/prospects?limit=100");
  });
});

describe("Outreach page — refresh reselects (acceptance 6)", () => {
  it("issues exactly one more read and re-renders from the new payload", async () => {
    const user = userEvent.setup();
    mockApiGet.mockResolvedValueOnce([prospect({ id: 1, email: "before-refresh@example.test" })]);

    render(<OutreachPage />);
    await screen.findByText("before-refresh@example.test");
    expect(mockApiGet).toHaveBeenCalledTimes(1);

    mockApiGet.mockResolvedValueOnce([prospect({ id: 2, email: "after-refresh@example.test" })]);
    await user.click(screen.getByTestId("outreach-refresh"));

    await screen.findByText("after-refresh@example.test");
    expect(mockApiGet).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("before-refresh@example.test")).toBeNull();
  });
});

/* ---------------------------------------------------------------------------
 * BELL — frozen contract (items/count, three states, honest count semantics)
 * ------------------------------------------------------------------------- */

const notif = (id: string, title: string, kind: string, taskId: number, createdAt: string) => ({
  id,
  kind,
  title,
  task_id: taskId,
  created_at: createdAt,
});

async function openBell(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByTestId("topbar-notifications"));
}

describe("Bell — verified count + items (frozen contract)", () => {
  it("shows the endpoint's count on the badge and lists title + relative time + task links", async () => {
    const user = userEvent.setup();
    const now = Date.now();
    const recent = new Date(now - 5 * 60_000).toISOString();
    const older = new Date(now - 3 * 3_600_000).toISOString();
    mockApiGet.mockResolvedValue({
      items: [
        notif("task:7:task_completed", "Set Up MorrowLatch Owner Workspaces", "task_completed", 7, recent),
        notif("task:9:task_failed", "Compare Five Competitors", "task_failed", 9, older),
      ],
      count: 2,
    });

    render(<Topbar />);

    const badge = await screen.findByTestId("notifications-badge");
    expect(badge).toHaveTextContent("2");
    expect(screen.getByTestId("topbar-notifications")).toHaveAccessibleName("Notifications, 2 in the last 48 hours");

    await openBell(user);

    const list = await screen.findByTestId("notifications-list");
    expect(within(list).getByText("Set Up MorrowLatch Owner Workspaces")).toBeInTheDocument();
    expect(within(list).getByText("Compare Five Competitors")).toBeInTheDocument();
    expect(within(list).getByText(relativeTime(recent))).toBeInTheDocument();
    expect(within(list).getByText(relativeTime(older))).toBeInTheDocument();
    expect(within(list).getByText("Completed")).toBeInTheDocument();
    expect(within(list).getByText("Failed")).toBeInTheDocument();

    const links = within(list).getAllByRole("link", { name: "View task" });
    expect(links.map((link) => link.getAttribute("href"))).toEqual(["/tasks?task=7", "/tasks?task=9"]);
  });

  it("renders the three contract kinds with their own labels and keeps the served order", async () => {
    const user = userEvent.setup();
    const now = new Date().toISOString();
    mockApiGet.mockResolvedValue({
      items: [
        notif("a", "Newest completed", "task_completed", 1, now),
        notif("b", "Middle awaiting", "awaiting_approval", 2, now),
        notif("c", "Oldest failed", "task_failed", 3, now),
      ],
      count: 3,
    });

    render(<Topbar />);
    await screen.findByTestId("notifications-badge");
    await openBell(user);

    const list = await screen.findByTestId("notifications-list");
    const items = within(list).getAllByRole("listitem");
    expect(items.map((item) => item.getAttribute("data-kind"))).toEqual([
      "task_completed",
      "awaiting_approval",
      "task_failed",
    ]);
    expect(within(list).getByText("Awaiting approval")).toBeInTheDocument();
  });
});

describe("Bell — three states, never a fake count", () => {
  it("shows the live loading voice while the read is in flight (and no badge)", async () => {
    const user = userEvent.setup();
    mockApiGet.mockReturnValue(new Promise<never>(() => {}));

    render(<Topbar />);
    expect(screen.queryByTestId("notifications-badge")).toBeNull();

    await openBell(user);

    expect(screen.getByText("Loading notifications…")).toBeInTheDocument();
    expect(screen.getByTestId("notifications-loading")).toBeInTheDocument();
    expect(screen.queryByText(/^\d+$/)).toBeNull();
  });

  it("renders the named error state on failure — dotless dash badge, no digit, a real RETRY", async () => {
    const user = userEvent.setup();
    mockApiGet.mockRejectedValue(new ApiError(404, "Not Found", "E_HTTP_404"));

    render(<Topbar />);

    const badge = await screen.findByTestId("notifications-badge");
    expect(badge).toHaveTextContent("—");
    expect(badge.textContent ?? "").not.toMatch(/\d/);
    expect(screen.getByTestId("topbar-notifications")).toHaveAccessibleName("Notifications, unavailable");

    await openBell(user);

    const alert = await screen.findByTestId("notifications-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("E_HTTP_404: Not Found");
    expect(screen.queryByTestId("notifications-list")).toBeNull();
    // Never a fabricated zero anywhere.
    expect(screen.queryByText("0")).toBeNull();

    // RETRY is wired to the same real read.
    const callsBefore = mockApiGet.mock.calls.length;
    mockApiGet.mockResolvedValue({ items: [], count: 0 });
    await user.click(screen.getByTestId("notifications-retry"));
    expect(mockApiGet.mock.calls.length).toBe(callsBefore + 1);
    expect(await screen.findByTestId("notifications-empty")).toBeInTheDocument();
  });

  it("renders the honest empty state on a true empty 200 — and no badge at all", async () => {
    const user = userEvent.setup();
    mockApiGet.mockResolvedValue({ items: [], count: 0 });

    render(<Topbar />);
    await openBell(user);

    expect(await screen.findByTestId("notifications-empty")).toHaveTextContent(
      "No notifications in the last 48 hours.",
    );
    expect(screen.queryByTestId("notifications-badge")).toBeNull();
    expect(screen.getByTestId("topbar-notifications")).toHaveAccessibleName("Notifications");
  });

  it("refuses a malformed payload with a named error instead of a partial list or an invented count", async () => {
    const user = userEvent.setup();
    mockApiGet.mockResolvedValue({ items: [{ id: "x" }], count: 9 });

    render(<Topbar />);

    const badge = await screen.findByTestId("notifications-badge");
    expect(badge).toHaveTextContent("—");

    await openBell(user);
    const alert = await screen.findByTestId("notifications-error");
    expect(alert.textContent).toMatch(/count \(9\) did not match its items \(1\)/);
  });
});

describe("Bell — localStorage last-seen drives the NEW marker (activity-feed pattern)", () => {
  it("marks only items newer than the stored last visit and re-books the visit", async () => {
    const user = userEvent.setup();
    const now = Date.now();
    window.localStorage.setItem(NOTIF_KEY, String(now - 3_600_000));
    const fresh = new Date(now - 10 * 60_000).toISOString();
    const seen = new Date(now - 2 * 3_600_000).toISOString();
    mockApiGet.mockResolvedValue({
      items: [
        notif("fresh", "Fresh since last visit", "task_completed", 1, fresh),
        notif("seen", "Already seen last visit", "task_failed", 2, seen),
      ],
      count: 2,
    });

    render(<Topbar />);
    await screen.findByTestId("notifications-badge");
    await openBell(user);

    const list = await screen.findByTestId("notifications-list");
    const items = within(list).getAllByRole("listitem");
    expect(within(items[0]).getByTestId("notification-new")).toBeInTheDocument();
    expect(within(items[1]).queryByTestId("notification-new")).toBeNull();
    // The badge carries the NEW mark while unread items exist.
    expect(screen.getByTestId("notifications-badge")).toHaveClass("chip-alert");
    // The visit was booked: the stored timestamp moved past the old one.
    const stored = Number(window.localStorage.getItem(NOTIF_KEY));
    expect(stored).toBeGreaterThan(now - 3_600_000);
  });

  it("shows no NEW marker on a first visit (no stored last-seen) rather than inventing one", async () => {
    const user = userEvent.setup();
    mockApiGet.mockResolvedValue({
      items: [notif("a", "First-ever item", "task_completed", 1, new Date().toISOString())],
      count: 1,
    });

    render(<Topbar />);
    await screen.findByTestId("notifications-badge");
    await openBell(user);

    const list = await screen.findByTestId("notifications-list");
    expect(within(list).queryByTestId("notification-new")).toBeNull();
    expect(screen.getByTestId("notifications-badge")).not.toHaveClass("chip-alert");
  });
});

describe("Bell — placement in the topbar", () => {
  it("keeps the bell's trigger identity and leaves the EDIT control untouched", async () => {
    const user = userEvent.setup();
    mockApiGet.mockResolvedValue({ items: [], count: 0 });

    render(<Topbar />);

    const bell = screen.getByTestId("topbar-notifications");
    expect(bell).toHaveAttribute("aria-expanded", "false");
    await openBell(user);
    expect(bell).toHaveAttribute("aria-expanded", "true");

    await user.keyboard("{Escape}");
    expect(bell).toHaveAttribute("aria-expanded", "false");
    expect(bell).toHaveFocus();

    await user.click(screen.getByTestId("topbar-edit"));
    expect(screen.getByText(/no API stores a dashboard layout/i)).toBeInTheDocument();
  });
});
