/**
 * V29 follow-up C — the dashboard's 15th widget under a SAVED layout.
 *
 * PROBED, NOT A DEFECT. The suspicion this file was written to test: a layout persisted before
 * `signals` existed (V12–V28 builds) could drop or misplace the card, because the store persists a
 * per-area id list and the card is new. Measured with a stored pre-signals layout — every area
 * reordered, `chat` hidden:
 *
 *   right    ['caps','social','outreach','ads','signals']   ← the saved order, card appended last
 *   left     ['website','status','auto_mode','business','phone']
 *   middle   ['media','tasks','documents']
 *
 * The card lands LAST in the right column, which is exactly its default position, and nothing is
 * dropped: `areaOrder` keeps the saved arrangement and appends every id the arrangement does not
 * mention, and `sanitizeLayout` keeps a known id from a build that did not have the card. No code
 * change was made. This suite pins the measured shape so a future edit to `areaOrder` /
 * `sanitizeLayout` / the page's widget map cannot silently reintroduce the suspicion — and so the
 * "15 widgets" total is asserted against the REGISTRY (the editor's own source), not against a
 * hand-counted literal.
 */
jest.mock("next/link", () => {
  return function MockLink({
    href,
    children,
    className,
  }: {
    href: string;
    children: React.ReactNode;
    className?: string;
  }) {
    return (
      <a href={href} className={className}>
        {children}
      </a>
    );
  };
});

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));
jest.mock("@/components/dashboard/ActivityFeed", () => ({ ActivityFeed: () => null }));

import { act, fireEvent, render, screen, within } from "@testing-library/react";

import { api } from "@/lib/api";
import DashboardPage from "@/app/dashboard/page";
import { Topbar } from "@/components/layout/Topbar";
import {
  __resetLayoutStoreForTests,
  STORAGE_KEY,
  WIDGETS,
} from "@/components/dashboard/dashboardLayout";

const mockGet = api.get as jest.Mock;

const READS: Record<string, unknown> = {
  "/agents/status": [],
  "/dashboard/summary": null,
  "/tasks": [],
  "/dashboard/reports/daily": null,
  "/finance/planning": {
    burn_line_items: [],
    capacity: null,
    margin_template: null,
    pricing_guardrails: [],
  },
  "/social/posts": [],
  "/outreach/prospects": [],
  "/ads/campaigns": [],
  "/channels/caps": { channels: [], generated_at: "2026-10-20T00:00:00Z" },
  "/signals": { items: [], sources: [], fetched_at: "2026-10-20T12:00:00Z", untrusted: true },
  "/auto-mode": { active: false, session: null },
  "/chat": { items: [], total: 0 },
  "/notifications": { items: [], count: 0 },
  "/config/deepseek-key": { configured: true, source: "stored", masked: "sk-…1234" },
  "/config": { website_url: null },
  "/health": { status: "ok" },
  "/phone/summary": {
    ok: true,
    data: { store_status: "ok", unread_total: 0, threads: [], missed_calls_today: [] },
  },
};

beforeEach(() => {
  window.localStorage.clear();
  __resetLayoutStoreForTests();
  mockGet.mockReset();
  mockGet.mockImplementation((path: string) => {
    const key = Object.keys(READS).find((candidate) => path.startsWith(candidate));
    if (key === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return Promise.resolve(READS[key]);
  });
});

function slotIds(regionLabel: string): string[] {
  const region = screen.getByLabelText(regionLabel);
  return Array.from(region.querySelectorAll("[data-widget-id]")).map(
    (element) => element.getAttribute("data-widget-id") ?? "",
  );
}

describe("a layout saved before `signals` existed", () => {
  it("keeps all 15 registry widgets, with the card in the position it ships in", async () => {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        hidden: ["chat"],
        order: {
          right: ["caps", "social", "outreach", "ads"],
          left: ["website", "status", "auto_mode", "business"],
          middle: ["media", "tasks"],
        },
      }),
    );

    await act(async () => {
      render(
        <>
          <Topbar />
          <DashboardPage />
        </>,
      );
    });

    // The saved arrangement is honoured, in every area...
    expect(slotIds("Workspace status")).toEqual([
      "website",
      "status",
      "auto_mode",
      "business",
      "phone",
    ]);
    expect(slotIds("Tasks and reports")).toEqual(["media", "tasks", "documents"]);
    // ...and the 15th widget is neither dropped nor moved: it is appended to the right column,
    // which is the position the registry gives it.
    expect(slotIds("Workspace widgets")).toEqual(["caps", "social", "outreach", "ads", "signals"]);

    // Every registry widget is mounted exactly once, and the region slots render.
    const mounted = [
      ...slotIds("Workspace status"),
      ...slotIds("Tasks and reports"),
      ...slotIds("Workspace widgets"),
      screen.getByLabelText("Activity feed").getAttribute("data-widget-id") ?? "",
      // `chat` is hidden by the stored layout — the editor can still show it again.
    ];
    expect(mounted).toHaveLength(WIDGETS.length - 1);

    fireEvent.click(screen.getByTestId("topbar-edit"));
    const editor = screen.getByTestId("dashboard-editor");
    expect(within(editor).getAllByRole("switch")).toHaveLength(WIDGETS.length);
    expect(within(editor).getByRole("switch", { name: "Show Chat rail" })).toBeInTheDocument();
    expect(within(editor).getByText("Signals")).toBeInTheDocument();
  });
});
